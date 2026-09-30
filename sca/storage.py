from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .identity import component_key, identify
from .models import DependencyEvidence, ParsedManifest


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS manifest_sources (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_path TEXT NOT NULL,
    manifest_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(project_id, content_hash)
);

CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    purl TEXT NOT NULL,
    display_name TEXT NOT NULL,
    UNIQUE(ecosystem, canonical_name)
);

CREATE TABLE IF NOT EXISTS alias_edges (
    id INTEGER PRIMARY KEY,
    from_component_id INTEGER NOT NULL REFERENCES components(id),
    to_component_id INTEGER NOT NULL REFERENCES components(id),
    origin TEXT NOT NULL CHECK(origin IN ('manifest', 'manual')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'revoked')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    UNIQUE(from_component_id, to_component_id)
);

CREATE TABLE IF NOT EXISTS project_snapshots (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    taken_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_evidence (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_id INTEGER NOT NULL REFERENCES manifest_sources(id),
    component_id INTEGER NOT NULL REFERENCES components(id),
    display_name TEXT NOT NULL,
    version TEXT NOT NULL,
    dependency_type TEXT NOT NULL,
    requested_by TEXT,
    scope TEXT,
    location_path TEXT NOT NULL,
    raw_record TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    UNIQUE(source_id, record_hash)
);
CREATE TABLE IF NOT EXISTS project_components (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    component_id INTEGER NOT NULL REFERENCES components(id),
    versions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('unified', 'conflict')),
    evidence_count INTEGER NOT NULL,
    PRIMARY KEY(project_id, component_id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def import_manifest(self, manifest: ParsedManifest, content: bytes) -> dict[str, int]:
        content_hash = hashlib.sha256(content).hexdigest()
        now = utc_now()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO projects(name, version, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(name, version) DO UPDATE SET name=excluded.name RETURNING id",
            (manifest.project, manifest.project_version, now),
        )
        project_id = int(cursor.fetchone()[0])
        cursor.execute(
            "SELECT id FROM manifest_sources WHERE project_id=? AND content_hash=?",
            (project_id, content_hash),
        )
        row = cursor.fetchone()
        if row:
            source_id = int(row[0])
            inserted = 0
        else:
            cursor.execute(
                "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (project_id, manifest.source_path, manifest.manifest_type, content_hash, now),
            )
            source_id = int(cursor.lastrowid)
            inserted = self._insert_evidence(cursor, project_id, source_id, manifest.evidence)
        self.conn.commit()
        self.refresh_project_components(project_id)
        self.snapshot_project(project_id, reason="import")
        return {
            "project_id": project_id,
            "source_id": source_id,
            "inserted_records": inserted,
            "total_records": len(manifest.evidence),
        }

    def _insert_evidence(
        self,
        cursor: sqlite3.Cursor,
        project_id: int,
        source_id: int,
        evidence: Iterable[DependencyEvidence],
    ) -> int:
        inserted = 0
        component_ids = {id(entry): self._component_id(cursor, entry.normalized()) for entry in evidence}
        for item in evidence:
            component_id = component_ids[id(item)]
            record_hash = stable_hash(
                {
                    "identity": identify(item),
                    "version": item.version,
                    "display_name": item.display_name,
                    "dependency_type": str(item.dependency_type),
                    "requested_by": item.requested_by,
                    "scope": item.scope,
                    "location_path": item.location_path,
                    "raw": item.raw,
                }
            )
            cursor.execute(
                "INSERT OR IGNORE INTO dependency_evidence(" 
                "project_id, source_id, component_id, display_name, version, dependency_type, "
                "requested_by, scope, location_path, raw_record, record_hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    source_id,
                    component_id,
                    item.display_name,
                    item.version,
                    str(item.dependency_type),
                    item.requested_by,
                    item.scope,
                    item.location_path,
                    json.dumps(item.raw, ensure_ascii=False, sort_keys=True),
                    record_hash,
                ),
            )
            inserted += int(cursor.rowcount)
            self._register_manifest_aliases(cursor, item, component_id)
        return inserted

    def _component_id(self, cursor: sqlite3.Cursor, item: DependencyEvidence) -> int:
        ecosystem, canonical_name, purl = identify(item)
        cursor.execute(
            "INSERT INTO components(ecosystem, canonical_name, purl, display_name) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(ecosystem, canonical_name) DO UPDATE SET display_name=excluded.display_name RETURNING id",
            (ecosystem, canonical_name, purl, item.display_name),
        )
        return int(cursor.fetchone()[0])

    def _register_manifest_aliases(
        self, cursor: sqlite3.Cursor, item: DependencyEvidence, source_component_id: int
    ) -> None:
        for alias in item.aliases:
            if ":" not in alias:
                continue
            alias_ecosystem, remainder = alias.split(":", 1)
            ecosystem, canonical_name = component_key(alias_ecosystem, remainder)
            cursor.execute(
                "SELECT id FROM components WHERE ecosystem=? AND canonical_name=?",
                (ecosystem, canonical_name),
            )
            row = cursor.fetchone()
            if row is None:
                continue
            alias_component_id = int(row[0])
            if self._component_row(cursor, source_component_id)["ecosystem"] != ecosystem:
                continue
            self._upsert_alias_edge(
                cursor, source_component_id, alias_component_id, origin="manifest", status="approved"
            )

    @staticmethod
    def _component_row(cursor: sqlite3.Cursor, component_id: int) -> sqlite3.Row:
        row = cursor.execute(
            "SELECT id, ecosystem, canonical_name, purl, display_name FROM components WHERE id=?",
            (component_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"component not found: {component_id}")
        return row

    def resolve_component(self, ref: str) -> sqlite3.Row:
        if ":" not in ref:
            raise ValueError(f"component reference must use ecosystem:name form: {ref}")
        ecosystem, remainder = ref.split(":", 1)
        ecosystem_value, canonical_name = component_key(ecosystem, remainder)
        row = self.conn.execute(
            "SELECT id, ecosystem, canonical_name, purl, display_name FROM components "
            "WHERE ecosystem=? AND canonical_name=?",
            (ecosystem_value, canonical_name),
        ).fetchone()
        if row is None:
            raise KeyError(f"component not found: {ref}")
        return row

    @staticmethod
    def _ordered(left: int, right: int) -> tuple[int, int]:
        return (left, right) if left < right else (right, left)

    def _upsert_alias_edge(
        self,
        cursor: sqlite3.Cursor,
        first_component_id: int,
        second_component_id: int,
        *,
        origin: str,
        status: str | None = None,
    ) -> int | None:
        if first_component_id == second_component_id:
            return None
        from_id, to_id = self._ordered(first_component_id, second_component_id)
        now = utc_now()
        cursor.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by) "
            "VALUES(?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "origin=CASE WHEN alias_edges.origin='manual' THEN 'manual' ELSE excluded.origin END, "
            "status=CASE WHEN ?='manifest' AND alias_edges.origin='manifest' AND alias_edges.status='revoked' "
            "THEN 'approved' ELSE alias_edges.status END "
            "RETURNING id",
            (from_id, to_id, origin, status or "pending", now, origin),
        )
        return int(cursor.fetchone()[0])

    def _approved_edges(self, cursor: sqlite3.Cursor) -> list[tuple[int, int]]:
        rows = cursor.execute(
            "SELECT from_component_id, to_component_id FROM alias_edges WHERE status='approved'"
        ).fetchall()
        return [(int(row[0]), int(row[1])) for row in rows]

    def alias_groups(self) -> dict[int, int]:
        parent: dict[int, int] = {}

        def find(value: int) -> int:
            parent.setdefault(value, value)
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        for left, right in self._approved_edges(self.conn.cursor()):
            union(left, right)
        groups = {component_id: find(component_id) for component_id in parent}
        return groups

    def alias_preview(self, first_ref: str, second_ref: str) -> dict[str, Any]:
        first = self.resolve_component(first_ref)
        second = self.resolve_component(second_ref)
        first_id, second_id = int(first["id"]), int(second["id"])
        allowed = True
        reasons: list[str] = []
        if first_id == second_id:
            allowed = False
            reasons.append("references resolve to the same component")
        if first["ecosystem"] != second["ecosystem"]:
            allowed = False
            reasons.append("components belong to different ecosystems")
        groups = self.alias_groups()
        root = groups.get(first_id, first_id)
        group_ids = {component_id for component_id, group_root in groups.items() if group_root == root} | {first_id}
        group_ids.add(second_id)
        placeholders = ",".join("?" for _ in group_ids)
        version_rows = self.conn.execute(
            f"SELECT project_id, COUNT(DISTINCT version) AS version_count, "
            f"json_group_array(DISTINCT version) AS versions "
            f"FROM dependency_evidence WHERE component_id IN ({placeholders}) GROUP BY project_id",
            tuple(group_ids),
        ).fetchall()
        affected_projects = []
        for row in version_rows:
            versions = sorted(value for value in json.loads(row["versions"]) if value)
            would_conflict = int(row["version_count"]) > 1
            if would_conflict:
                reasons.append(
                    f"project {row['project_id']} would retain a version conflict: {', '.join(versions)}"
                )
            affected_projects.append(
                {"project_id": int(row["project_id"]), "versions": versions, "would_conflict": would_conflict}
            )
        existing = self.conn.execute(
            "SELECT origin, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            self._ordered(first_id, second_id),
        ).fetchone()
        cursor = self.conn.cursor()
        members = [dict(self._component_row(cursor, component_id)) for component_id in sorted(group_ids)]
        return {
            "allowed": allowed,
            "reasons": reasons,
            "from": dict(first),
            "to": dict(second),
            "group_members": members,
            "affected_projects": affected_projects,
            "existing_edge": dict(existing) if existing else None,
        }

    def approve_alias(self, first_ref: str, second_ref: str, *, decided_by: str = "operator") -> dict[str, Any]:
        preview = self.alias_preview(first_ref, second_ref)
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        first_id, second_id = int(preview["from"]["id"]), int(preview["to"]["id"])
        from_id, to_id = self._ordered(first_id, second_id)
        now = utc_now()
        self.conn.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by) "
            "VALUES(?, ?, 'manual', 'approved', ?, ?, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "status='approved', decided_at=excluded.decided_at, decided_by=excluded.decided_by",
            (from_id, to_id, now, now, decided_by),
        )
        self.conn.commit()
        affected = self._refresh_affected_projects({first_id, second_id})
        return {"preview": preview, "refreshed_projects": affected}

    def propose_alias(self, first_ref: str, second_ref: str, *, created_by: str = "operator") -> dict[str, Any]:
        preview = self.alias_preview(first_ref, second_ref)
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        first_id, second_id = int(preview["from"]["id"]), int(preview["to"]["id"])
        from_id, to_id = self._ordered(first_id, second_id)
        now = utc_now()
        row = self.conn.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by) "
            "VALUES(?, ?, 'manual', 'pending', ?, NULL, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "origin='manual', status=CASE WHEN alias_edges.status='revoked' THEN 'pending' ELSE alias_edges.status END, "
            "decided_by=CASE WHEN alias_edges.status='revoked' THEN excluded.decided_by ELSE alias_edges.decided_by END "
            "RETURNING id, status",
            (from_id, to_id, now, created_by),
        ).fetchone()
        self.conn.commit()
        return {"edge_id": int(row[0]), "status": row[1], "preview": preview}

    def revoke_alias(self, first_ref: str, second_ref: str, *, decided_by: str = "operator") -> dict[str, Any]:
        first = self.resolve_component(first_ref)
        second = self.resolve_component(second_ref)
        from_id, to_id = self._ordered(int(first["id"]), int(second["id"]))
        row = self.conn.execute(
            "SELECT id, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            (from_id, to_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"alias edge not found: {first_ref} <-> {second_ref}")
        affected_components = self._edge_component_group(from_id) | self._edge_component_group(to_id)
        self.conn.execute(
            "UPDATE alias_edges SET status='revoked', decided_at=?, decided_by=? WHERE id=?",
            (utc_now(), decided_by, int(row["id"])),
        )
        self.conn.commit()
        affected = self._refresh_affected_projects(affected_components, reason="alias-revoke")
        return {"revoked_edge": int(row["id"]), "refreshed_projects": affected}

    def _edge_component_group(self, component_id: int) -> set[int]:
        groups = self.alias_groups()
        root = groups.get(component_id, component_id)
        return {item for item, group_root in groups.items() if group_root == root} | {root, component_id}

    def _refresh_affected_projects(self, component_ids: set[int], *, reason: str = "alias-approve") -> list[int]:
        if not component_ids:
            return []
        placeholders = ",".join("?" for _ in component_ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT project_id FROM dependency_evidence WHERE component_id IN ({placeholders})",
            tuple(component_ids),
        ).fetchall()
        project_ids = [int(row[0]) for row in rows]
        for project_id in project_ids:
            self.refresh_project_components(project_id)
            self.snapshot_project(project_id, reason=reason)
        return project_ids

    def refresh_project_components(self, project_id: int | None = None) -> None:
        groups = self.alias_groups()
        where = "WHERE e.project_id=?" if project_id is not None else ""
        params: tuple[Any, ...] = (project_id,) if project_id is not None else ()
        if project_id is not None:
            self.conn.execute("DELETE FROM project_components WHERE project_id=?", (project_id,))
        else:
            self.conn.execute("DELETE FROM project_components")
        rows = self.conn.execute(
            f"""
            SELECT e.project_id, e.component_id, e.version
            FROM dependency_evidence e
            {where}
            """,
            params,
        ).fetchall()
        aggregated: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            raw_component_id = int(row["component_id"])
            root_id = groups.get(raw_component_id, raw_component_id)
            bucket = aggregated.setdefault(
                (int(row["project_id"]), root_id),
                {"versions": set(), "evidence_count": 0},
            )
            if row["version"]:
                bucket["versions"].add(row["version"])
            bucket["evidence_count"] += 1
        for ((project_value, root_id), bucket) in aggregated.items():
            versions = sorted(bucket["versions"])
            self.conn.execute(
                "INSERT INTO project_components(project_id, component_id, versions_json, status, evidence_count) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(project_id, component_id) DO UPDATE SET "
                "versions_json=excluded.versions_json, status=excluded.status, evidence_count=excluded.evidence_count",
                (
                    project_value,
                    root_id,
                    json.dumps(versions, ensure_ascii=False),
                    "conflict" if len(versions) > 1 else "unified",
                    bucket["evidence_count"],
                ),
            )
        self.conn.commit()

    def project_report(self, project_name: str, project_version: str | None = None) -> dict[str, Any]:
        params: list[Any] = [project_name]
        version_sql = ""
        if project_version is not None:
            version_sql = " AND p.version=?"
            params.append(project_version)
        project = self.conn.execute(
            f"SELECT id, name, version FROM projects p WHERE name=?{version_sql} ORDER BY version DESC LIMIT 1",
            params,
        ).fetchone()
        if project is None:
            raise KeyError(f"project not found: {project_name}")
        groups = self.alias_groups()
        component_rows = self.conn.execute("SELECT * FROM components").fetchall()
        members: dict[int, list[dict[str, Any]]] = {}
        for row in component_rows:
            root_id = groups.get(int(row["id"]), int(row["id"]))
            members.setdefault(root_id, []).append(dict(row))
        components = self.conn.execute(
            """
            SELECT pc.*, c.ecosystem, c.canonical_name, c.purl, c.display_name
            FROM project_components pc JOIN components c ON c.id=pc.component_id
            WHERE pc.project_id=?
            ORDER BY pc.status DESC, c.ecosystem, c.canonical_name
            """,
            (project["id"],),
        ).fetchall()
        result_components = []
        for row in components:
            item = dict(row)
            item["member_components"] = sorted(members.get(int(row["component_id"]), []), key=lambda m: m["canonical_name"])
            result_components.append(item)
        evidence = self.conn.execute(
            """
            SELECT e.*, c.ecosystem, c.canonical_name, c.purl, s.source_path, s.manifest_type
            FROM dependency_evidence e
            JOIN components c ON c.id=e.component_id
            JOIN manifest_sources s ON s.id=e.source_id
            WHERE e.project_id=?
            ORDER BY c.canonical_name, e.location_path
            """,
            (project["id"],),
        ).fetchall()
        result_evidence = []
        for row in evidence:
            item = dict(row)
            raw_component_id = int(item["component_id"])
            root_id = groups.get(raw_component_id, raw_component_id)
            root = self.conn.execute(
                "SELECT id, ecosystem, canonical_name, purl, display_name FROM components WHERE id=?",
                (root_id,),
            ).fetchone()
            item["logical_component_id"] = root_id
            item["logical_purl"] = root["purl"]
            item["logical_canonical_name"] = root["canonical_name"]
            result_evidence.append(item)
        return {
            "project": {"name": project["name"], "version": project["version"]},
            "components": result_components,
            "evidence": result_evidence,
        }

    def snapshot_project(self, project_id: int, *, reason: str) -> int | None:
        project = self.conn.execute(
            "SELECT name, version FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            return None
        report = self.project_report(project["name"], project["version"])
        now = utc_now()
        cursor = self.conn.execute(
            "INSERT INTO project_snapshots(project_id, taken_at, reason, report_json) VALUES(?, ?, ?, ?)",
            (project_id, now, reason, json.dumps(report, ensure_ascii=False, sort_keys=True)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def project_history(self, project_name: str, project_version: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [project_name]
        version_sql = ""
        if project_version is not None:
            version_sql = " AND p.version=?"
            params.append(project_version)
        rows = self.conn.execute(
            f"""
            SELECT s.id, s.project_id, s.taken_at, s.reason, s.report_json, p.name, p.version
            FROM project_snapshots s JOIN projects p ON p.id=s.project_id
            WHERE p.name=?{version_sql}
            ORDER BY s.id
            """,
            params,
        ).fetchall()
        history = []
        for row in rows:
            history.append(
                {
                    "snapshot_id": int(row["id"]),
                    "taken_at": row["taken_at"],
                    "reason": row["reason"],
                    "project": {"name": row["name"], "version": row["version"]},
                    "report": json.loads(row["report_json"]),
                }
            )
        return history

    def pending_aliases(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.id, e.origin, e.status, e.created_at,
                   c1.ecosystem AS from_ecosystem, c1.canonical_name AS from_name,
                   c2.ecosystem AS to_ecosystem, c2.canonical_name AS to_name
            FROM alias_edges e
            JOIN components c1 ON c1.id=e.from_component_id
            JOIN components c2 ON c2.id=e.to_component_id
            WHERE e.status='pending'
            ORDER BY e.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
