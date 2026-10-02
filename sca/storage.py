from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .identity import component_key, identify
from .models import DependencyEvidence, ParsedManifest
from .versioning import in_range


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
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'revoked', 'rejected')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    version_range TEXT NOT NULL DEFAULT '*',
    sources_json TEXT NOT NULL DEFAULT '["*"]',
    note TEXT,
    reject_reason TEXT,
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
        self._migrate_alias_edges()

    def _migrate_alias_edges(self) -> None:
        table_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='alias_edges'"
        ).fetchone()[0]
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(alias_edges)")}
        needed = {"version_range", "sources_json", "note", "reject_reason"}
        if needed <= columns and "'rejected'" in table_sql:
            return
        self.conn.executescript(
            """
            ALTER TABLE alias_edges RENAME TO alias_edges_legacy;
            CREATE TABLE alias_edges (
                id INTEGER PRIMARY KEY,
                from_component_id INTEGER NOT NULL REFERENCES components(id),
                to_component_id INTEGER NOT NULL REFERENCES components(id),
                origin TEXT NOT NULL CHECK(origin IN ('manifest', 'manual')),
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'revoked', 'rejected')),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                version_range TEXT NOT NULL DEFAULT '*',
                sources_json TEXT NOT NULL DEFAULT '["*"]',
                note TEXT,
                reject_reason TEXT,
                UNIQUE(from_component_id, to_component_id)
            );
            INSERT INTO alias_edges(id, from_component_id, to_component_id, origin, status,
                                    created_at, decided_at, decided_by)
                SELECT id, from_component_id, to_component_id, origin, status,
                       created_at, decided_at, decided_by FROM alias_edges_legacy;
            DROP TABLE alias_edges_legacy;
            """
        )
        self.conn.commit()

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

    def _approved_edge_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alias_edges WHERE status='approved' ORDER BY id"
        ).fetchall()

    @staticmethod
    def _edge_sources(edge: sqlite3.Row) -> list[str]:
        try:
            sources = json.loads(edge["sources_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            sources = ["*"]
        return sources or ["*"]

    def _edge_applies_to_project(self, edge: sqlite3.Row, project_name: str) -> bool:
        sources = self._edge_sources(edge)
        return sources == ["*"] or project_name in sources

    def _component_versions_in_project(self, component_id: int, project_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT version FROM dependency_evidence WHERE component_id=? AND project_id=?",
            (component_id, project_id),
        ).fetchall()
        return [row[0] for row in rows if row[0]]

    def _compute_groups(
        self, edges: list[sqlite3.Row], project: tuple[int, str] | None = None
    ) -> dict[int, int]:
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

        for edge in edges:
            from_id, to_id = int(edge["from_component_id"]), int(edge["to_component_id"])
            if project is not None:
                project_id, project_name = project
                if not self._edge_applies_to_project(edge, project_name):
                    continue
                version_range = edge["version_range"] or "*"
                from_versions = self._component_versions_in_project(from_id, project_id)
                to_versions = self._component_versions_in_project(to_id, project_id)
                if not any(in_range(version, version_range) for version in from_versions):
                    continue
                if not any(in_range(version, version_range) for version in to_versions):
                    continue
            union(from_id, to_id)
        return {component_id: find(component_id) for component_id in parent}

    def alias_groups(self, project_id: int | None = None) -> dict[int, int]:
        project = None
        if project_id is not None:
            row = self.conn.execute(
                "SELECT id, name FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if row is not None:
                project = (int(row["id"]), row["name"])
        return self._compute_groups(self._approved_edge_rows(), project)

    def alias_preview(
        self,
        first_ref: str,
        second_ref: str,
        *,
        version_range: str = "*",
        sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        first = self.resolve_component(first_ref)
        second = self.resolve_component(second_ref)
        first_id, second_id = int(first["id"]), int(second["id"])
        scope_sources = sorted(sources) if sources else ["*"]
        allowed = True
        reasons: list[str] = []
        if first_id == second_id:
            allowed = False
            reasons.append("references resolve to the same component")
        if first["ecosystem"] != second["ecosystem"]:
            allowed = False
            reasons.append("components belong to different ecosystems")
        try:
            in_range("0", version_range)
        except ValueError as exc:
            allowed = False
            reasons.append(f"invalid version range: {exc}")
        candidate = {
            "from_component_id": first_id,
            "to_component_id": second_id,
            "version_range": version_range or "*",
            "sources_json": json.dumps(scope_sources),
        }
        simulated = list(self._approved_edge_rows()) + [candidate]
        project_rows = self.conn.execute(
            "SELECT DISTINCT p.id, p.name FROM projects p "
            "JOIN dependency_evidence e ON e.project_id=p.id "
            "WHERE e.component_id IN (?, ?) ORDER BY p.id",
            (first_id, second_id),
        ).fetchall()
        affected_projects = []
        member_ids: set[int] = {first_id, second_id}
        for project_row in project_rows:
            project_id, project_name = int(project_row["id"]), project_row["name"]
            in_scope = self._edge_applies_to_project(candidate, project_name)
            groups = self._compute_groups(simulated, (project_id, project_name))
            root = groups.get(first_id, first_id)
            merged = in_scope and root == groups.get(second_id, second_id)
            group_ids = {
                component_id for component_id, group_root in groups.items() if group_root == root
            } | {first_id}
            if merged:
                group_ids.add(second_id)
            member_ids |= group_ids
            placeholders = ",".join("?" for _ in group_ids)
            version_rows = self.conn.execute(
                f"SELECT DISTINCT version FROM dependency_evidence "
                f"WHERE project_id=? AND component_id IN ({placeholders})",
                (project_id, *sorted(group_ids)),
            ).fetchall()
            versions = sorted(row[0] for row in version_rows if row[0])
            would_conflict = merged and len(versions) > 1
            if would_conflict:
                reasons.append(
                    f"project {project_name} would retain a version conflict: {', '.join(versions)}"
                )
            affected_projects.append(
                {
                    "project_id": project_id,
                    "project": project_name,
                    "in_scope": in_scope,
                    "merged": merged,
                    "versions": versions,
                    "would_conflict": would_conflict,
                }
            )
        existing = self.conn.execute(
            "SELECT origin, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            self._ordered(first_id, second_id),
        ).fetchone()
        cursor = self.conn.cursor()
        members = [dict(self._component_row(cursor, component_id)) for component_id in sorted(member_ids)]
        return {
            "allowed": allowed,
            "reasons": reasons,
            "from": dict(first),
            "to": dict(second),
            "version_range": version_range or "*",
            "sources": scope_sources,
            "group_members": members,
            "affected_projects": affected_projects,
            "existing_edge": dict(existing) if existing else None,
        }

    def approve_alias(
        self,
        first_ref: str,
        second_ref: str,
        *,
        decided_by: str = "operator",
        version_range: str = "*",
        sources: Iterable[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        from_id, to_id = self._ordered(
            int(self.resolve_component(first_ref)["id"]), int(self.resolve_component(second_ref)["id"])
        )
        existing = self.conn.execute(
            "SELECT id, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            (from_id, to_id),
        ).fetchone()
        if existing is not None and existing["status"] == "pending":
            result = self.approve_edges([int(existing["id"])], decided_by=decided_by, note=note)
            preview = self.alias_preview(first_ref, second_ref)
            return {
                "preview": preview,
                "refreshed_projects": result["refreshed_projects"],
                "edge_id": int(existing["id"]),
            }
        preview = self.alias_preview(first_ref, second_ref, version_range=version_range, sources=sources)
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        scope_sources = sorted(sources) if sources else ["*"]
        now = utc_now()
        self.conn.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by, version_range, sources_json, note) "
            "VALUES(?, ?, 'manual', 'approved', ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "status='approved', decided_at=excluded.decided_at, decided_by=excluded.decided_by, "
            "note=COALESCE(excluded.note, alias_edges.note)",
            (from_id, to_id, now, now, decided_by, version_range or "*", json.dumps(scope_sources), note),
        )
        self.conn.commit()
        affected = self._refresh_affected_projects({from_id, to_id})
        return {"preview": preview, "refreshed_projects": affected}

    def propose_alias(
        self,
        first_ref: str,
        second_ref: str,
        *,
        created_by: str = "operator",
        version_range: str = "*",
        sources: Iterable[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        preview = self.alias_preview(first_ref, second_ref, version_range=version_range, sources=sources)
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        first_id, second_id = int(preview["from"]["id"]), int(preview["to"]["id"])
        from_id, to_id = self._ordered(first_id, second_id)
        scope_sources = sorted(sources) if sources else ["*"]
        now = utc_now()
        row = self.conn.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by, version_range, sources_json, note) "
            "VALUES(?, ?, 'manual', 'pending', ?, NULL, ?, ?, ?, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "origin='manual', status=CASE WHEN alias_edges.status IN ('revoked', 'rejected') THEN 'pending' ELSE alias_edges.status END, "
            "decided_by=CASE WHEN alias_edges.status IN ('revoked', 'rejected') THEN excluded.decided_by ELSE alias_edges.decided_by END, "
            "version_range=CASE WHEN alias_edges.status IN ('revoked', 'rejected') THEN excluded.version_range ELSE alias_edges.version_range END, "
            "sources_json=CASE WHEN alias_edges.status IN ('revoked', 'rejected') THEN excluded.sources_json ELSE alias_edges.sources_json END, "
            "reject_reason=CASE WHEN alias_edges.status IN ('revoked', 'rejected') THEN NULL ELSE alias_edges.reject_reason END "
            "RETURNING id, status",
            (from_id, to_id, now, created_by, version_range or "*", json.dumps(scope_sources), note),
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
        return self.revoke_edge(int(row["id"]), decided_by=decided_by)

    def revoke_edge(self, edge_id: int, *, decided_by: str = "operator") -> dict[str, Any]:
        row = self._edge_row(edge_id)
        from_id, to_id = int(row["from_component_id"]), int(row["to_component_id"])
        affected_components = self._edge_component_group(from_id) | self._edge_component_group(to_id)
        self.conn.execute(
            "UPDATE alias_edges SET status='revoked', decided_at=?, decided_by=? WHERE id=?",
            (utc_now(), decided_by, edge_id),
        )
        self.conn.commit()
        affected = self._refresh_affected_projects(affected_components, reason="alias-revoke")
        recheck = self._recheck_pending_after_revoke({from_id, to_id})
        return {
            "revoked_edge": edge_id,
            "refreshed_projects": affected,
            "affected_pending": recheck["affected_pending"],
            "remaining_preview": recheck["remaining_preview"],
        }

    def _recheck_pending_after_revoke(self, revoked_component_ids: set[int]) -> dict[str, Any]:
        pending = self.pending_edges()
        if not pending:
            return {"affected_pending": [], "remaining_preview": {"candidates": [], "conflicts": []}}
        related = self._connected_edges(revoked_component_ids)
        affected = [edge for edge in pending if edge["id"] in related]
        return {"affected_pending": affected, "remaining_preview": self.joint_preview()}

    def _connected_edges(self, seed_component_ids: set[int]) -> set[int]:
        rows = self.conn.execute(
            "SELECT id, from_component_id, to_component_id FROM alias_edges WHERE status IN ('approved', 'pending')"
        ).fetchall()
        adjacency: dict[int, list[int]] = {}
        edge_by_pair: list[tuple[int, int, int]] = []
        for row in rows:
            left, right = int(row["from_component_id"]), int(row["to_component_id"])
            adjacency.setdefault(left, []).append(right)
            adjacency.setdefault(right, []).append(left)
            edge_by_pair.append((int(row["id"]), left, right))
        reachable = set(seed_component_ids)
        frontier = list(seed_component_ids)
        while frontier:
            current = frontier.pop()
            for neighbor in adjacency.get(current, []):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    frontier.append(neighbor)
        return {
            edge_id
            for edge_id, left, right in edge_by_pair
            if left in reachable and right in reachable
        }

    def _edge_component_group(self, component_id: int) -> set[int]:
        groups = self.alias_groups()
        root = groups.get(component_id, component_id)
        return {item for item, group_root in groups.items() if group_root == root} | {root, component_id}

    def _edge_row(self, edge_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM alias_edges WHERE id=?", (edge_id,)).fetchone()
        if row is None:
            raise KeyError(f"alias edge not found: #{edge_id}")
        return row

    def edge_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        cursor = self.conn.cursor()
        first = self._component_row(cursor, int(row["from_component_id"]))
        second = self._component_row(cursor, int(row["to_component_id"]))
        return {
            "id": int(row["id"]),
            "origin": row["origin"],
            "status": row["status"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "version_range": row["version_range"],
            "sources": self._edge_sources(row),
            "note": row["note"],
            "reject_reason": row["reject_reason"],
            "from_purl": first["purl"],
            "to_purl": second["purl"],
            "from_ref": f"{first['ecosystem']}:{first['canonical_name']}",
            "to_ref": f"{second['ecosystem']}:{second['canonical_name']}",
        }

    def pending_edges(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM alias_edges WHERE status='pending' ORDER BY id"
        ).fetchall()
        return [self.edge_dict(row) for row in rows]

    def list_edges(self, status: str = "all") -> list[dict[str, Any]]:
        if status == "all":
            rows = self.conn.execute("SELECT * FROM alias_edges ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM alias_edges WHERE status=? ORDER BY id", (status,)
            ).fetchall()
        return [self.edge_dict(row) for row in rows]

    def joint_preview(self, edge_ids: list[int] | None = None) -> dict[str, Any]:
        if edge_ids is None:
            candidate_rows = self.conn.execute(
                "SELECT * FROM alias_edges WHERE status='pending' ORDER BY id"
            ).fetchall()
        else:
            candidate_rows = []
            for edge_id in edge_ids:
                row = self._edge_row(edge_id)
                if row["status"] != "pending":
                    raise ValueError(f"edge #{edge_id} is {row['status']}, not pending")
                candidate_rows.append(row)
        approved = self._approved_edge_rows()
        simulated = list(approved) + list(candidate_rows)
        candidate_component_ids = {
            int(row[key])
            for row in candidate_rows
            for key in ("from_component_id", "to_component_id")
        }
        conflicts: list[dict[str, Any]] = []
        projects_seen: list[dict[str, Any]] = []
        if candidate_component_ids:
            placeholders = ",".join("?" for _ in candidate_component_ids)
            project_rows = self.conn.execute(
                f"SELECT DISTINCT p.id, p.name FROM projects p "
                f"JOIN dependency_evidence e ON e.project_id=p.id "
                f"WHERE e.component_id IN ({placeholders}) ORDER BY p.id",
                tuple(sorted(candidate_component_ids)),
            ).fetchall()
            for project_row in project_rows:
                project_id, project_name = int(project_row["id"]), project_row["name"]
                groups = self._compute_groups(simulated, (project_id, project_name))
                evidence_rows = self.conn.execute(
                    "SELECT e.component_id, e.version, e.location_path, s.source_path, c.canonical_name "
                    "FROM dependency_evidence e "
                    "JOIN manifest_sources s ON s.id=e.source_id "
                    "JOIN components c ON c.id=e.component_id "
                    "WHERE e.project_id=?",
                    (project_id,),
                ).fetchall()
                by_root: dict[int, dict[str, Any]] = {}
                for evidence in evidence_rows:
                    component_id = int(evidence["component_id"])
                    root = groups.get(component_id, component_id)
                    bucket = by_root.setdefault(root, {"components": set(), "versions": set(), "evidence": []})
                    bucket["components"].add(component_id)
                    if evidence["version"]:
                        bucket["versions"].add(evidence["version"])
                    bucket["evidence"].append(
                        {
                            "canonical_name": evidence["canonical_name"],
                            "version": evidence["version"],
                            "source_path": evidence["source_path"],
                            "location_path": evidence["location_path"],
                        }
                    )
                project_conflict_roots = []
                for root, bucket in by_root.items():
                    if len(bucket["versions"]) <= 1 or len(bucket["components"]) <= 1:
                        continue
                    contributing = []
                    involves_proposal = False
                    for edge in simulated:
                        left = groups.get(int(edge["from_component_id"]), int(edge["from_component_id"]))
                        right = groups.get(int(edge["to_component_id"]), int(edge["to_component_id"]))
                        if left == root and right == root:
                            is_candidate = edge in candidate_rows
                            involves_proposal = involves_proposal or is_candidate
                            entry = self.edge_dict(edge)
                            entry["role"] = "proposal" if is_candidate else "approved"
                            contributing.append(entry)
                    if not involves_proposal:
                        continue
                    project_conflict_roots.append(root)
                    conflicts.append(
                        {
                            "project": project_name,
                            "project_id": project_id,
                            "versions": sorted(bucket["versions"]),
                            "components": sorted(bucket["components"]),
                            "proposals": contributing,
                            "evidence": bucket["evidence"],
                        }
                    )
                projects_seen.append(
                    {
                        "project": project_name,
                        "project_id": project_id,
                        "conflict_groups": len(project_conflict_roots),
                    }
                )
        return {
            "candidates": [self.edge_dict(row) for row in candidate_rows],
            "projects": projects_seen,
            "conflicts": conflicts,
        }

    def approve_edges(
        self, edge_ids: list[int], *, decided_by: str = "operator", note: str | None = None
    ) -> dict[str, Any]:
        rows = []
        for edge_id in edge_ids:
            row = self._edge_row(edge_id)
            if row["status"] != "pending":
                raise ValueError(f"edge #{edge_id} is {row['status']}, not pending")
            rows.append(row)
        now = utc_now()
        affected_components: set[int] = set()
        approved = []
        for row in rows:
            self.conn.execute(
                "UPDATE alias_edges SET status='approved', decided_at=?, decided_by=?, "
                "note=COALESCE(?, note) WHERE id=?",
                (now, decided_by, note, int(row["id"])),
            )
            affected_components.add(int(row["from_component_id"]))
            affected_components.add(int(row["to_component_id"]))
            approved.append(self.edge_dict(self._edge_row(int(row["id"]))))
        self.conn.commit()
        refreshed = self._refresh_affected_projects(affected_components, reason="alias-approve")
        remaining = self.pending_edges()
        remaining_preview = self.joint_preview() if remaining else {"candidates": [], "conflicts": []}
        return {
            "approved": approved,
            "refreshed_projects": refreshed,
            "remaining_pending": remaining,
            "remaining_preview": remaining_preview,
        }

    def reject_edge(self, edge_id: int, reason: str, *, decided_by: str = "operator") -> dict[str, Any]:
        row = self._edge_row(edge_id)
        if row["status"] != "pending":
            raise ValueError(f"edge #{edge_id} is {row['status']}, not pending")
        self.conn.execute(
            "UPDATE alias_edges SET status='rejected', reject_reason=?, decided_at=?, decided_by=? WHERE id=?",
            (reason, utc_now(), decided_by, edge_id),
        )
        self.conn.commit()
        return self.edge_dict(self._edge_row(edge_id))

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
        groups = self.alias_groups(project_id)
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
        groups = self.alias_groups(project["id"])
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
        return self.pending_edges()
