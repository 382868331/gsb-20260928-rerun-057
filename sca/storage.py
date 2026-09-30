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

CREATE TABLE IF NOT EXISTS component_aliases (
    source_component_id INTEGER NOT NULL REFERENCES components(id),
    alias_component_id INTEGER NOT NULL REFERENCES components(id),
    PRIMARY KEY(source_component_id, alias_component_id)
);

CREATE TABLE IF NOT EXISTS manual_aliases (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    target_canonical_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'revoked')) DEFAULT 'pending',
    note TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS component_merge_map (
    alias_id INTEGER NOT NULL REFERENCES manual_aliases(id),
    source_component_id INTEGER NOT NULL REFERENCES components(id),
    target_component_id INTEGER NOT NULL REFERENCES components(id),
    PRIMARY KEY(alias_id, source_component_id, target_component_id)
);

CREATE TABLE IF NOT EXISTS alias_events (
    id INTEGER PRIMARY KEY,
    alias_id INTEGER NOT NULL REFERENCES manual_aliases(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
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
        self.consolidate_aliases()
        self.refresh_project_components(project_id)
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
            self._insert_alias_edges(cursor, item, component_id)
        return inserted

    def _component_id(self, cursor: sqlite3.Cursor, item: DependencyEvidence) -> int:
        ecosystem, canonical_name, purl = identify(item)
        cursor.execute(
            "INSERT INTO components(ecosystem, canonical_name, purl, display_name) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(ecosystem, canonical_name) DO UPDATE SET display_name=excluded.display_name RETURNING id",
            (ecosystem, canonical_name, purl, item.display_name),
        )
        return int(cursor.fetchone()[0])

    def _insert_alias_edges(
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
            first, second = sorted((source_component_id, int(row[0])))
            cursor.execute(
                "INSERT OR IGNORE INTO component_aliases(source_component_id, alias_component_id) VALUES(?, ?)",
                (first, second),
            )

    def consolidate_aliases(self) -> int:
        rows = self.conn.execute("SELECT source_component_id, alias_component_id FROM component_aliases").fetchall()
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

        for row in rows:
            union(int(row[0]), int(row[1]))
        merged = 0
        for old_id in sorted(parent):
            new_id = find(old_id)
            if new_id == old_id:
                continue
            self.conn.execute("UPDATE dependency_evidence SET component_id=? WHERE component_id=?", (new_id, old_id))
            self.conn.execute(
                "UPDATE OR IGNORE project_components SET component_id=? WHERE component_id=?",
                (new_id, old_id),
            )
            self.conn.execute("DELETE FROM project_components WHERE component_id=?", (old_id,))
            self.conn.execute("DELETE FROM component_aliases WHERE source_component_id=? OR alias_component_id=?", (old_id, old_id))
            self.conn.execute("DELETE FROM components WHERE id=?", (old_id,))
            merged += 1
        self.conn.commit()
        return merged

    def refresh_project_components(self, project_id: int | None = None) -> None:
        if project_id is not None:
            self.conn.execute("DELETE FROM project_components WHERE project_id=?", (project_id,))
            rows = self.conn.execute(
                "SELECT project_id, component_id, version FROM dependency_evidence WHERE project_id=?",
                (project_id,),
            ).fetchall()
        else:
            self.conn.execute("DELETE FROM project_components")
            rows = self.conn.execute(
                "SELECT project_id, component_id, version FROM dependency_evidence"
            ).fetchall()
        parent = self._merge_parent()
        groups: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            key = (int(row["project_id"]), self._resolve_component(int(row["component_id"]), parent))
            group = groups.setdefault(key, {"versions": set(), "count": 0})
            if row["version"]:
                group["versions"].add(row["version"])
            group["count"] += 1
        for (group_project_id, component_id), group in sorted(groups.items()):
            versions = sorted(group["versions"])
            self.conn.execute(
                "INSERT INTO project_components(project_id, component_id, versions_json, status, evidence_count) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(project_id, component_id) DO UPDATE SET "
                "versions_json=excluded.versions_json, status=excluded.status, evidence_count=excluded.evidence_count",
                (
                    group_project_id,
                    component_id,
                    json.dumps(versions, ensure_ascii=False),
                    "conflict" if len(versions) > 1 else "unified",
                    group["count"],
                ),
            )
        self.conn.commit()

    def _merge_parent(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT m.source_component_id, m.target_component_id FROM component_merge_map m "
            "JOIN manual_aliases a ON a.id = m.alias_id WHERE a.status = 'approved'"
        ).fetchall()
        parent: dict[int, int] = {}
        for row in rows:
            source_root = self._resolve_component(int(row[0]), parent)
            target_root = self._resolve_component(int(row[1]), parent)
            if source_root != target_root:
                parent[source_root] = target_root
        return parent

    @staticmethod
    def _resolve_component(component_id: int, parent: dict[int, int]) -> int:
        parent.setdefault(component_id, component_id)
        root = component_id
        while parent[root] != root:
            root = parent[root]
        while parent[component_id] != component_id:
            parent[component_id], component_id = root, parent[component_id]
        return root

    def _component_row(self, ecosystem: str, name: str) -> sqlite3.Row | None:
        ecosystem_value, canonical_name = component_key(ecosystem, name)
        return self.conn.execute(
            "SELECT * FROM components WHERE ecosystem=? AND canonical_name=?",
            (ecosystem_value, canonical_name),
        ).fetchone()

    def _alias_row(self, alias_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM manual_aliases WHERE id=?", (alias_id,)).fetchone()
        if row is None:
            raise KeyError(f"alias not found: {alias_id}")
        return row

    def _record_alias_event(self, alias_id: int, action: str, detail: Any) -> None:
        self.conn.execute(
            "INSERT INTO alias_events(alias_id, action, detail, created_at) VALUES(?, ?, ?, ?)",
            (alias_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def propose_alias(
        self,
        ecosystem: str,
        name: str,
        target_name: str,
        target_ecosystem: str | None = None,
        note: str | None = None,
    ) -> int:
        ecosystem_value, canonical_name = component_key(ecosystem, name)
        target_value, target_canonical = component_key(target_ecosystem or ecosystem, target_name)
        if ecosystem_value != target_value:
            raise ValueError(
                f"manual aliases require a single ecosystem: {ecosystem_value} vs {target_value}"
            )
        if canonical_name == target_canonical:
            raise ValueError(f"alias target already resolves to {canonical_name}")
        source = self._component_row(ecosystem_value, canonical_name)
        target = self._component_row(ecosystem_value, target_canonical)
        if source is None:
            raise KeyError(f"component not found: {ecosystem_value}/{canonical_name}")
        if target is None:
            raise KeyError(f"component not found: {ecosystem_value}/{target_canonical}")
        existing = self.conn.execute(
            "SELECT id, status FROM manual_aliases WHERE ecosystem=? AND status != 'revoked' AND "
            "((canonical_name=? AND target_canonical_name=?) OR (canonical_name=? AND target_canonical_name=?))",
            (ecosystem_value, canonical_name, target_canonical, target_canonical, canonical_name),
        ).fetchone()
        if existing is not None:
            raise ValueError(f"alias already exists: id {existing['id']} ({existing['status']})")
        cursor = self.conn.execute(
            "INSERT INTO manual_aliases(ecosystem, canonical_name, target_canonical_name, status, note, created_at) "
            "VALUES(?, ?, ?, 'pending', ?, ?)",
            (ecosystem_value, canonical_name, target_canonical, note, utc_now()),
        )
        alias_id = int(cursor.lastrowid)
        self._record_alias_event(
            alias_id,
            "proposed",
            {
                "ecosystem": ecosystem_value,
                "canonical_name": canonical_name,
                "target_canonical_name": target_canonical,
                "note": note,
            },
        )
        self.conn.commit()
        return alias_id

    def list_aliases(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM manual_aliases ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def preview_alias(self, alias_id: int) -> dict[str, Any]:
        alias = self._alias_row(alias_id)
        source = self._component_row(alias["ecosystem"], alias["canonical_name"])
        target = self._component_row(alias["ecosystem"], alias["target_canonical_name"])
        if source is None or target is None:
            raise KeyError(f"alias {alias_id} references a component that no longer exists")
        parent = self._merge_parent()
        source_root = self._resolve_component(int(source["id"]), parent)
        target_root = self._resolve_component(int(target["id"]), parent)
        already_merged = source_root == target_root
        if not already_merged:
            parent[source_root] = target_root
        merged_root = self._resolve_component(int(source["id"]), parent)
        member_ids = {
            int(row[0])
            for row in self.conn.execute("SELECT id FROM components").fetchall()
            if self._resolve_component(int(row[0]), parent) == merged_root
        }
        placeholders = ",".join("?" for _ in member_ids)
        components = self.conn.execute(
            f"SELECT * FROM components WHERE id IN ({placeholders}) ORDER BY id",
            tuple(sorted(member_ids)),
        ).fetchall()
        evidence_rows = self.conn.execute(
            f"""
            SELECT e.project_id, e.component_id, e.version, p.name AS project_name, p.version AS project_version
            FROM dependency_evidence e JOIN projects p ON p.id = e.project_id
            WHERE e.component_id IN ({placeholders})
            """,
            tuple(sorted(member_ids)),
        ).fetchall()
        impact: dict[int, dict[str, Any]] = {}
        for row in evidence_rows:
            entry = impact.setdefault(
                int(row["project_id"]),
                {
                    "project_id": int(row["project_id"]),
                    "project": row["project_name"],
                    "project_version": row["project_version"],
                    "evidence_count": 0,
                    "versions": set(),
                },
            )
            entry["evidence_count"] += 1
            if row["version"]:
                entry["versions"].add(row["version"])
        for entry in impact.values():
            entry["versions"] = sorted(entry["versions"])
            entry["resulting_status"] = "conflict" if len(entry["versions"]) > 1 else "unified"
        return {
            "alias": dict(alias),
            "already_merged": already_merged,
            "components": [dict(row) for row in components],
            "impact": sorted(impact.values(), key=lambda entry: entry["project_id"]),
        }

    def approve_alias(self, alias_id: int) -> dict[str, Any]:
        alias = self._alias_row(alias_id)
        if alias["status"] != "pending":
            raise ValueError(f"alias {alias_id} is {alias['status']}, only pending aliases can be approved")
        preview = self.preview_alias(alias_id)
        source = self._component_row(alias["ecosystem"], alias["canonical_name"])
        target = self._component_row(alias["ecosystem"], alias["target_canonical_name"])
        self.conn.execute(
            "INSERT INTO component_merge_map(alias_id, source_component_id, target_component_id) VALUES(?, ?, ?)",
            (alias_id, int(source["id"]), int(target["id"])),
        )
        self.conn.execute(
            "UPDATE manual_aliases SET status='approved', decided_at=? WHERE id=?",
            (utc_now(), alias_id),
        )
        self._record_alias_event(alias_id, "approved", preview)
        self.refresh_project_components()
        return preview

    def revoke_alias(self, alias_id: int) -> dict[str, Any]:
        alias = self._alias_row(alias_id)
        if alias["status"] != "approved":
            raise ValueError(f"alias {alias_id} is {alias['status']}, only approved aliases can be revoked")
        snapshot = self.preview_alias(alias_id)
        self.conn.execute(
            "UPDATE manual_aliases SET status='revoked', decided_at=? WHERE id=?",
            (utc_now(), alias_id),
        )
        self._record_alias_event(alias_id, "revoked", snapshot)
        self.refresh_project_components()
        return snapshot

    def alias_history(self, alias_id: int | None = None) -> list[dict[str, Any]]:
        if alias_id is None:
            rows = self.conn.execute(
                "SELECT * FROM alias_events ORDER BY id"
            ).fetchall()
        else:
            self._alias_row(alias_id)
            rows = self.conn.execute(
                "SELECT * FROM alias_events WHERE alias_id=? ORDER BY id", (alias_id,)
            ).fetchall()
        return [dict(row) for row in rows]

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
        components = self.conn.execute(
            """
            SELECT pc.*, c.ecosystem, c.canonical_name, c.purl, c.display_name
            FROM project_components pc JOIN components c ON c.id=pc.component_id
            WHERE pc.project_id=?
            ORDER BY pc.status DESC, c.ecosystem, c.canonical_name
            """,
            (project["id"],),
        ).fetchall()
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
        parent = self._merge_parent()
        component_rows = self.conn.execute(
            "SELECT id, ecosystem, canonical_name, purl, display_name FROM components"
        ).fetchall()
        component_by_id = {int(row["id"]): row for row in component_rows}
        evidence_items = []
        for row in evidence:
            item = dict(row)
            resolved_id = self._resolve_component(int(item["component_id"]), parent)
            item["original_component_id"] = item["component_id"]
            item["original_canonical_name"] = item["canonical_name"]
            item["original_purl"] = item["purl"]
            if resolved_id != int(item["component_id"]) and resolved_id in component_by_id:
                resolved = component_by_id[resolved_id]
                item["component_id"] = resolved_id
                item["canonical_name"] = resolved["canonical_name"]
                item["purl"] = resolved["purl"]
            evidence_items.append(item)
        return {
            "project": {"name": project["name"], "version": project["version"]},
            "components": [dict(row) for row in components],
            "evidence": evidence_items,
        }
