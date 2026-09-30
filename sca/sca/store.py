from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from .models import AliasDecision, Component, ConflictEvidence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store:
    """Append-only event log. State is always re-derivable from events,
    so save/reopen and repeated imports are safe and traceable."""

    def __init__(self, path: str):
        self.path = path
        self.events: list[dict] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self.events.append(json.loads(line))

    def _append(self, kind: str, payload: dict) -> dict:
        ev = {"id": _uid("ev"), "kind": kind, "at": _now(), **payload}
        self.events.append(ev)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    # ---- write side -------------------------------------------------

    def record_scan(self, scan_id: str, source: str, components: list[dict]) -> int:
        """Record components from a scan. Duplicate observations (same
        name/ecosystem/version/source/build_path) are skipped, so
        incremental scans and repeated imports stay idempotent."""
        known = {c.key for c in self.components()}
        added = 0
        for c in components:
            comp = Component(
                name=c["name"],
                ecosystem=c["ecosystem"],
                version=str(c["version"]),
                source=source,
                build_path=c.get("build_path", c["name"]),
                scan_id=scan_id,
            )
            if comp.key in known:
                continue
            known.add(comp.key)
            self._append("component", {"component": comp.__dict__})
            added += 1
        self._append("scan", {"scan_id": scan_id, "source": source, "added": added})
        return added

    def propose_alias(self, name_a, name_b, ecosystem, version_range, sources) -> AliasDecision:
        d = AliasDecision(
            decision_id=_uid("dec"),
            name_a=name_a,
            name_b=name_b,
            ecosystem=ecosystem,
            version_range=version_range or "*",
            sources=tuple(sources) if sources else ("*",),
            created_at=_now(),
        )
        self._append("decision", {"decision": _dec_dict(d)})
        return d

    def set_decision_status(self, decision_id: str, status: str) -> None:
        self._append("decision_status", {"decision_id": decision_id, "status": status})

    def record_conflict(self, name, ecosystem, source, versions) -> ConflictEvidence:
        ev = ConflictEvidence(
            evidence_id=_uid("cfx"),
            name=name,
            ecosystem=ecosystem,
            source=source,
            versions=tuple(sorted(set(versions))),
            created_at=_now(),
        )
        self._append("conflict", {"conflict": _conf_dict(ev)})
        return ev

    # ---- read side (re-derived from the event log) ------------------

    def components(self) -> list[Component]:
        return [Component(**e["component"]) for e in self.events if e["kind"] == "component"]

    def decisions(self) -> list[AliasDecision]:
        latest: dict[str, AliasDecision] = {}
        order: list[str] = []
        for e in self.events:
            if e["kind"] == "decision":
                d = e["decision"]
                d["sources"] = tuple(d["sources"])
                dec = AliasDecision(**d)
                latest[dec.decision_id] = dec
                order.append(dec.decision_id)
            elif e["kind"] == "decision_status":
                cur = latest[e["decision_id"]]
                latest[e["decision_id"]] = AliasDecision(
                    **{**cur.__dict__, "status": e["status"], "resolved_at": e["at"]}
                )
        return [latest[i] for i in order]

    def conflicts(self) -> list[ConflictEvidence]:
        out = []
        for e in self.events:
            if e["kind"] == "conflict":
                c = dict(e["conflict"])
                c["versions"] = tuple(c["versions"])
                out.append(ConflictEvidence(**c))
        return out


def _dec_dict(d: AliasDecision) -> dict:
    x = d.__dict__.copy()
    x["sources"] = list(x["sources"])
    return x


def _conf_dict(c: ConflictEvidence) -> dict:
    x = c.__dict__.copy()
    x["versions"] = list(x["versions"])
    return x
