from __future__ import annotations

import hashlib
import json
import re
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
    group_key TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('unified', 'conflict')),
    evidence_count INTEGER NOT NULL,
    PRIMARY KEY(project_id, group_key)
);

CREATE TABLE IF NOT EXISTS manual_alias_scopes (
    id INTEGER PRIMARY KEY,
    edge_id INTEGER NOT NULL REFERENCES alias_edges(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    min_version TEXT,
    max_version TEXT,
    source_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending', 'approved', 'revoked')),
    UNIQUE(edge_id, project_id, min_version, max_version, source_key)
);

CREATE TABLE IF NOT EXISTS manual_alias_sources (
    scope_id INTEGER NOT NULL REFERENCES manual_alias_scopes(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    PRIMARY KEY(scope_id, source_path)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def version_sort_key(version: str) -> tuple[tuple[int, ...], tuple[str, ...], str]:
    numeric_parts = [int(part) for part in re.findall(r"\d+", version)]
    suffix_parts = tuple(part for part in re.findall(r"[A-Za-z]+(?:[.-][A-Za-z]+)*", version))
    return tuple(numeric_parts), suffix_parts, version


def _compare_versions(left: str, right: str) -> int:
    left_key, right_key = version_sort_key(left), version_sort_key(right)
    for index in range(max(len(left_key[0]), len(right_key[0]))):
        left_value = left_key[0][index] if index < len(left_key[0]) else 0
        right_value = right_key[0][index] if index < len(right_key[0]) else 0
        if left_value != right_value:
            return -1 if left_value < right_value else 1
    if left_key[1] != right_key[1]:
        return -1 if left_key[1] < right_key[1] else 1
    return 0


def version_in_range(version: str, minimum: str | None, maximum: str | None) -> bool:
    if minimum is not None and _compare_versions(version, minimum) < 0:
        return False
    if maximum is not None:
        return _compare_versions(version, maximum) <= 0
    return True


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _ensure_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(project_components)")
        }
        if "group_key" not in columns:
            self.conn.executescript(
                """
                ALTER TABLE project_components RENAME TO project_components_old;
                CREATE TABLE project_components (
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    component_id INTEGER NOT NULL REFERENCES components(id),
                    group_key TEXT NOT NULL,
                    versions_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('unified', 'conflict')),
                    evidence_count INTEGER NOT NULL,
                    PRIMARY KEY(project_id, group_key)
                );
                DROP TABLE project_components_old;
                """
            )
            project_ids = [row[0] for row in self.conn.execute("SELECT id FROM projects")]
            self.conn.commit()
            for project_id in project_ids:
                self.refresh_project_components(project_id)
        old_scope_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(manual_alias_scopes_old)")
        }
        scope_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(manual_alias_scopes)")
        }
        if old_scope_columns:
            self.conn.execute("ALTER TABLE manual_alias_scopes RENAME TO manual_alias_scopes_old")
            self.conn.execute("ALTER TABLE manual_alias_sources RENAME TO manual_alias_sources_old")
            self.conn.executescript(
                """
                CREATE TABLE manual_alias_scopes (
                    id INTEGER PRIMARY KEY,
                    edge_id INTEGER NOT NULL REFERENCES alias_edges(id) ON DELETE CASCADE,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    min_version TEXT,
                    max_version TEXT,
                    source_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'approved' CHECK(status IN ('pending', 'approved', 'revoked'))
                );
                CREATE TABLE manual_alias_sources (
                    scope_id INTEGER NOT NULL REFERENCES manual_alias_scopes(id) ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    PRIMARY KEY(scope_id, source_path)
                );
                INSERT INTO manual_alias_scopes(id, edge_id, project_id, min_version, max_version, source_key, status)
                SELECT id, edge_id, project_id, min_version, max_version, '', status FROM manual_alias_scopes_old;
                INSERT INTO manual_alias_sources(scope_id, source_path)
                SELECT scope_id, source_path FROM manual_alias_sources_old;
                DROP TABLE manual_alias_sources_old;
                DROP TABLE manual_alias_scopes_old;
                """
            )
        else:
            if "status" not in scope_columns:
                self.conn.execute(
                    "ALTER TABLE manual_alias_scopes ADD COLUMN status TEXT NOT NULL DEFAULT 'approved' "
                    "CHECK(status IN ('pending', 'approved', 'revoked'))"
                )
            if "source_key" not in scope_columns:
                self.conn.execute("ALTER TABLE manual_alias_scopes ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
        self.conn.commit()
        legacy_rows = self.conn.execute(
            """
            SELECT e.id AS edge_id, e.status
            FROM alias_edges e
            WHERE e.origin='manual'
              AND NOT EXISTS (SELECT 1 FROM manual_alias_scopes s WHERE s.edge_id=e.id)
            """
        ).fetchall()
        for edge in legacy_rows:
            project_rows = self.conn.execute(
                """
                SELECT project_id FROM dependency_evidence
                WHERE component_id IN (SELECT from_component_id FROM alias_edges WHERE id=?)
                   OR component_id IN (SELECT to_component_id FROM alias_edges WHERE id=?)
                GROUP BY project_id
                HAVING COUNT(DISTINCT component_id) = 2
                """,
                (int(edge["edge_id"]), int(edge["edge_id"])),
            ).fetchall()
            for project in project_rows:
                self.conn.execute(
                    "INSERT INTO manual_alias_scopes(edge_id, project_id, min_version, max_version, source_key, status) "
                    "VALUES(?, ?, NULL, NULL, '', ?)",
                    (int(edge["edge_id"]), int(project["project_id"]), edge["status"]),
                )
        self.conn.commit()

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

    def _manifest_edges(self, cursor: sqlite3.Cursor) -> set[tuple[int, int]]:
        rows = cursor.execute(
            "SELECT from_component_id, to_component_id FROM alias_edges "
            "WHERE status='approved' AND origin='manifest'"
        ).fetchall()
        return {(int(row[0]), int(row[1])) for row in rows}

    def _manual_scopes(self, cursor: sqlite3.Cursor) -> dict[int, list[dict[str, Any]]]:
        rows = cursor.execute(
            """
            SELECT sc.edge_id, e.from_component_id, e.to_component_id, sc.project_id,
                   sc.min_version, sc.max_version, sc.status,
                   COALESCE((SELECT json_group_array(source_path)
                             FROM manual_alias_sources s WHERE s.scope_id=sc.id), '[]') AS sources
            FROM manual_alias_scopes sc
            JOIN alias_edges e ON e.id=sc.edge_id
            WHERE e.status='approved' AND e.origin='manual' AND sc.status='approved'
            """
        ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["edge_id"]), []).append(
                {
                    "edge_id": int(row["edge_id"]),
                    "components": {int(row["from_component_id"]), int(row["to_component_id"])},
                    "project_id": int(row["project_id"]),
                    "min_version": row["min_version"],
                    "max_version": row["max_version"],
                    "sources": set(json.loads(row["sources"])),
                }
            )
        return result

    @staticmethod
    def _union_find(values: Iterable[int]) -> tuple[dict[int, int], Any]:
        parent = {value: value for value in values}

        def find(value: int) -> int:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        return parent, find

    @staticmethod
    def _scope_matches(row: sqlite3.Row, scope: dict[str, Any]) -> bool:
        if int(row["project_id"]) != scope["project_id"]:
            return False
        if scope["sources"] and row["source_path"] not in scope["sources"]:
            return False
        return version_in_range(row["version"], scope["min_version"], scope["max_version"])

    def _project_evidence_rows(self, cursor: sqlite3.Cursor, project_id: int) -> list[sqlite3.Row]:
        return cursor.execute(
            """
            SELECT e.id AS evidence_id, e.project_id, e.component_id, e.version,
                   e.location_path, s.source_path
            FROM dependency_evidence e
            JOIN manifest_sources s ON s.id=e.source_id
            WHERE e.project_id=?
            """,
            (project_id,),
        ).fetchall()

    def _project_group_rows(
        self,
        cursor: sqlite3.Cursor,
        project_id: int,
        *,
        extra_scopes: list[dict[str, Any]] | None = None,
        suppress_edges: set[tuple[int, int]] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._project_evidence_rows(cursor, project_id)
        if not rows:
            return []
        parent, find = self._union_find(int(row["evidence_id"]) for row in rows)

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        by_id = {int(row["evidence_id"]): row for row in rows}
        component_rows: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            component_rows.setdefault(int(row["component_id"]), []).append(row)

        manifest_components: set[int] = set()
        for left, right in self._manifest_edges(cursor):
            if suppress_edges and tuple(sorted((left, right))) in suppress_edges:
                continue
            if left in component_rows and right in component_rows:
                manifest_components.update((left, right))
                for left_row in component_rows[left]:
                    for right_row in component_rows[right]:
                        union(int(left_row["evidence_id"]), int(right_row["evidence_id"]))

        scopes_by_edge = self._manual_scopes(cursor)
        if extra_scopes:
            scopes_by_edge[-1] = extra_scopes
        active_evidence: set[int] = set()
        for edge_scopes in scopes_by_edge.values():
            for scope in edge_scopes:
                left_id, right_id = sorted(scope["components"])
                eligible = {
                    component_id: [
                        row
                        for row in component_rows.get(component_id, [])
                        if self._scope_matches(row, scope)
                    ]
                    for component_id in (left_id, right_id)
                }
                if eligible[left_id] and eligible[right_id]:
                    for left_row in eligible[left_id]:
                        for right_row in eligible[right_id]:
                            left_evidence, right_evidence = int(left_row["evidence_id"]), int(right_row["evidence_id"])
                            active_evidence.update((left_evidence, right_evidence))
                            union(left_evidence, right_evidence)

        for component_id, component_evidence in component_rows.items():
            if component_id in manifest_components:
                for row in component_evidence:
                    active_evidence.add(int(row["evidence_id"]))
                for row in component_evidence[1:]:
                    union(int(component_evidence[0]["evidence_id"]), int(row["evidence_id"]))
                continue
            inactive = [row for row in component_evidence if int(row["evidence_id"]) not in active_evidence]
            for row in inactive[1:]:
                union(int(inactive[0]["evidence_id"]), int(row["evidence_id"]))

        grouped: dict[int, dict[str, Any]] = {}
        for evidence_id in parent:
            root = find(evidence_id)
            row = by_id[evidence_id]
            bucket = grouped.setdefault(
                root,
                {
                    "evidence_ids": set(),
                    "component_ids": set(),
                    "versions": set(),
                    "source_paths": set(),
                    "location_paths": set(),
                },
            )
            bucket["evidence_ids"].add(evidence_id)
            bucket["component_ids"].add(int(row["component_id"]))
            if row["version"]:
                bucket["versions"].add(row["version"])
            bucket["source_paths"].add(row["source_path"])
            bucket["location_paths"].add(row["location_path"])

        result = []
        for root_evidence, bucket in grouped.items():
            component_ids = sorted(bucket["component_ids"])
            versions = sorted(bucket["versions"], key=version_sort_key)
            group_key = (
                f"c{component_ids[0]}"
                if len(component_ids) == 1 and root_evidence not in active_evidence
                else f"e{root_evidence}"
            )
            result.append(
                {
                    "group_key": group_key,
                    "root_component_id": component_ids[0],
                    "component_ids": component_ids,
                    "evidence_ids": sorted(bucket["evidence_ids"]),
                    "versions": versions,
                    "source_paths": sorted(bucket["source_paths"]),
                    "location_paths": sorted(bucket["location_paths"]),
                }
            )
        return sorted(result, key=lambda item: item["group_key"])

    def _resolve_project_id(self, project: str | int | None, project_version: str | None = None) -> int | None:
        if project is None:
            return None
        if isinstance(project, int):
            row = self.conn.execute("SELECT id FROM projects WHERE id=?", (project,)).fetchone()
        else:
            sql = "SELECT id FROM projects WHERE name=?"
            params: list[Any] = [project]
            if project_version is not None:
                sql += " AND version=?"
                params.append(project_version)
            sql += " ORDER BY version DESC"
            row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise KeyError(f"project not found: {project}")
        return int(row[0])

    def _project_identity(self, project_id: int) -> dict[str, str | int]:
        row = self.conn.execute("SELECT id, name, version FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"project not found: {project_id}")
        return {"id": int(row["id"]), "name": row["name"], "version": row["version"]}

    @staticmethod
    def _normalize_scope_sources(sources: Iterable[str] | None) -> tuple[str, ...]:
        if sources is None:
            return ()
        return tuple(dict.fromkeys(source for source in sources if source))

    def _resolve_preview_scope(
        self,
        first_id: int,
        second_id: int,
        project: str | int | None,
        project_version: str | None,
        min_version: str | None,
        max_version: str | None,
        sources: Iterable[str] | None,
    ) -> dict[str, Any]:
        if min_version and max_version and _compare_versions(min_version, max_version) > 0:
            raise ValueError("min_version cannot be greater than max_version")
        if project is None:
            rows = self.conn.execute(
                """
                SELECT DISTINCT project_id FROM dependency_evidence
                WHERE component_id IN (?, ?)
                GROUP BY project_id
                HAVING COUNT(DISTINCT component_id) = 2
                """,
                (first_id, second_id),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("project is required when the components occur together in multiple projects")
            project_id = int(rows[0]["project_id"]) if rows else None
        else:
            project_id = self._resolve_project_id(project, project_version)
        source_paths = self._normalize_scope_sources(sources)
        if not source_paths:
            source_paths = ()
        return {
            "edge_id": -1,
            "components": {first_id, second_id},
            "project_id": project_id,
            "min_version": min_version,
            "max_version": max_version,
            "sources": set(source_paths),
            "source_paths": source_paths,
        }

    @staticmethod
    def _require_explicit_sources(preview: dict[str, Any]) -> None:
        if not preview["scope"]["source_paths"]:
            raise ValueError("at least one explicit --source path is required")

    def alias_preview(
        self,
        first_ref: str,
        second_ref: str,
        *,
        project: str | int | None = None,
        project_version: str | None = None,
        min_version: str | None = None,
        max_version: str | None = None,
        sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
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
        scope = self._resolve_preview_scope(
            first_id,
            second_id,
            project,
            project_version,
            min_version,
                max_version,
                sources,
        )
        if scope["project_id"] is None:
            reasons.append("both components have no evidence in a common project")
        if min_version and max_version and _compare_versions(min_version, max_version) > 0:
            allowed = False
            reasons.append("min_version cannot be greater than max_version")
        cursor = self.conn.cursor()
        current_groups = self._project_group_rows(cursor, scope["project_id"]) if scope["project_id"] is not None else []
        current_by_key = {group["group_key"]: group for group in current_groups}
        proposed_groups = (
            self._project_group_rows(
                cursor,
                scope["project_id"],
                extra_scopes=[scope],
                suppress_edges={self._ordered(first_id, second_id)},
            )
            if scope["project_id"] is not None
            else []
        )
        endpoint_ids = {first_id, second_id}
        component_cache = {
            int(row["id"]): dict(row)
            for row in cursor.execute("SELECT * FROM components").fetchall()
        }

        def describe_group(group: dict[str, Any]) -> dict[str, Any]:
            return {
                "group_key": group["group_key"],
                "components": [component_cache[component_id] for component_id in group["component_ids"]],
                "versions": group["versions"],
                "source_paths": group["source_paths"],
                "location_paths": group["location_paths"],
                "would_conflict": len(group["versions"]) > 1,
            }

        merged_groups = []
        affected_projects = []
        version_disagreements = []
        for group in proposed_groups:
            described = describe_group(group)
            if group["versions"]:
                affected_projects.append(
                    {
                        "project_id": scope["project_id"],
                        "versions": group["versions"],
                        "would_conflict": len(group["versions"]) > 1,
                    }
                )
            if endpoint_ids.issubset(set(group["component_ids"])):
                merged_groups.append(described)
                if group["group_key"] not in current_by_key and len(group["versions"]) > 1:
                    version_disagreements.append(described)
                    reasons.append(
                        "scoped records retain a version conflict: " + ", ".join(group["versions"])
                    )
            if len(group["versions"]) > 1 and endpoint_ids & set(group["component_ids"]):
                if described not in version_disagreements:
                    version_disagreements.append(described)
        if scope["project_id"] is not None and not merged_groups:
            allowed = False
            reasons.append("no in-scope evidence matches both components")

        endpoint_rows = []
        if scope["project_id"] is not None:
            endpoint_rows = cursor.execute(
                """
                SELECT e.id, e.project_id, e.component_id, e.version, e.location_path, s.source_path
                FROM dependency_evidence e JOIN manifest_sources s ON s.id=e.source_id
                WHERE e.project_id=? AND e.component_id IN (?, ?)
                ORDER BY s.source_path, e.location_path
                """,
                (scope["project_id"], first_id, second_id),
            ).fetchall()
        unaffected_sources = []
        for row in endpoint_rows:
            component_id = int(row["component_id"])
            matches = self._scope_matches(row, scope)
            if matches:
                continue
            reasons_out = []
            if scope["sources"] and row["source_path"] not in scope["sources"]:
                reasons_out.append("source is outside approved source scope")
            if not version_in_range(row["version"], scope["min_version"], scope["max_version"]):
                reasons_out.append("version is outside approved range")
            unaffected_sources.append(
                {
                    "component": component_cache[component_id],
                    "version": row["version"],
                    "source_path": row["source_path"],
                    "location_path": row["location_path"],
                    "reasons": reasons_out,
                }
            )

        existing = self.conn.execute(
            "SELECT origin, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            self._ordered(first_id, second_id),
        ).fetchone()
        current_member_ids = {
            component_id
            for group in current_groups
            if endpoint_ids & set(group["component_ids"])
            for component_id in group["component_ids"]
        } | endpoint_ids
        members = [dict(self._component_row(cursor, component_id)) for component_id in sorted(current_member_ids)]
        scope_result = {
            "project": self._project_identity(scope["project_id"]) if scope["project_id"] is not None else None,
            "ecosystem": first["ecosystem"],
            "min_version": min_version,
            "max_version": max_version,
            "source_paths": scope["source_paths"],
            "match_all_sources": not bool(scope["source_paths"]),
        }
        return {
            "allowed": allowed,
            "reasons": reasons,
            "from": dict(first),
            "to": dict(second),
            "scope": scope_result,
            "group_members": members,
            "merged_groups": merged_groups,
            "affected_projects": affected_projects,
            "version_disagreements": version_disagreements,
            "unaffected_sources": unaffected_sources,
            "existing_edge": dict(existing) if existing else None,
        }

    def _save_manual_scope(
        self,
        cursor: sqlite3.Cursor,
        edge_id: int,
        preview: dict[str, Any],
        status: str,
    ) -> int:
        project_id = int(preview["scope"]["project"]["id"])
        minimum = preview["scope"]["min_version"]
        maximum = preview["scope"]["max_version"]
        sources = tuple(preview["scope"]["source_paths"])
        source_key = stable_hash(sources)
        cursor.execute(
            "INSERT INTO manual_alias_scopes(edge_id, project_id, min_version, max_version, source_key, status) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(edge_id, project_id, min_version, max_version, source_key) DO UPDATE SET "
            "status=excluded.status "
            "RETURNING id",
            (edge_id, project_id, minimum, maximum, source_key, status),
        )
        row = cursor.fetchone()
        if row is None:
            row = cursor.execute(
                "SELECT id FROM manual_alias_scopes WHERE edge_id=? AND project_id=? "
                "AND COALESCE(min_version, '')=COALESCE(?, '') AND COALESCE(max_version, '')=COALESCE(?, '')",
                (edge_id, project_id, minimum, maximum),
            ).fetchone()
        scope_id = int(row[0])
        cursor.execute("DELETE FROM manual_alias_sources WHERE scope_id=?", (scope_id,))
        if sources:
            cursor.executemany(
                "INSERT INTO manual_alias_sources(scope_id, source_path) VALUES(?, ?) ON CONFLICT DO NOTHING",
                [(scope_id, source) for source in sources],
            )
        return scope_id

    def approve_alias(
        self,
        first_ref: str,
        second_ref: str,
        *,
        decided_by: str = "operator",
        project: str | int | None = None,
        project_version: str | None = None,
        min_version: str | None = None,
        max_version: str | None = None,
        sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if project is None:
            raise ValueError("an explicit --project is required")
        preview = self.alias_preview(
            first_ref,
            second_ref,
            project=project,
            project_version=project_version,
            min_version=min_version,
            max_version=max_version,
            sources=sources,
        )
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        self._require_explicit_sources(preview)
        first_id, second_id = int(preview["from"]["id"]), int(preview["to"]["id"])
        from_id, to_id = self._ordered(first_id, second_id)
        now = utc_now()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by) "
            "VALUES(?, ?, 'manual', 'approved', ?, ?, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "origin='manual', status='approved', decided_at=excluded.decided_at, decided_by=excluded.decided_by "
            "RETURNING id",
            (from_id, to_id, now, now, decided_by),
        )
        edge_id = int(cursor.fetchone()[0])
        self._save_manual_scope(cursor, edge_id, preview, "approved")
        self.conn.commit()
        affected = self._refresh_affected_projects({first_id, second_id})
        return {"preview": preview, "refreshed_projects": affected}

    def propose_alias(
        self,
        first_ref: str,
        second_ref: str,
        *,
        created_by: str = "operator",
        project: str | int | None = None,
        project_version: str | None = None,
        min_version: str | None = None,
        max_version: str | None = None,
        sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if project is None:
            raise ValueError("an explicit --project is required")
        preview = self.alias_preview(
            first_ref,
            second_ref,
            project=project,
            project_version=project_version,
            min_version=min_version,
            max_version=max_version,
            sources=sources,
        )
        if not preview["allowed"]:
            raise ValueError("; ".join(preview["reasons"]))
        self._require_explicit_sources(preview)
        first_id, second_id = int(preview["from"]["id"]), int(preview["to"]["id"])
        from_id, to_id = self._ordered(first_id, second_id)
        now = utc_now()
        cursor = self.conn.cursor()
        row = cursor.execute(
            "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by) "
            "VALUES(?, ?, 'manual', 'pending', ?, NULL, ?) "
            "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
            "origin='manual', status=CASE WHEN alias_edges.status IN ('revoked','approved') THEN 'pending' ELSE alias_edges.status END, "
            "decided_by=CASE WHEN alias_edges.status='revoked' THEN excluded.decided_by ELSE alias_edges.decided_by END "
            "RETURNING id, status",
            (from_id, to_id, now, created_by),
        ).fetchone()
        edge_id = int(row[0])
        self._save_manual_scope(cursor, edge_id, preview, "pending")
        self.conn.commit()
        return {"edge_id": int(row[0]), "status": row[1], "preview": preview}

    def revoke_alias(
        self,
        first_ref: str,
        second_ref: str,
        *,
        decided_by: str = "operator",
        project: str | int | None = None,
        project_version: str | None = None,
        min_version: str | None = None,
        max_version: str | None = None,
        sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        first = self.resolve_component(first_ref)
        second = self.resolve_component(second_ref)
        from_id, to_id = self._ordered(int(first["id"]), int(second["id"]))
        row = self.conn.execute(
            "SELECT id, status FROM alias_edges WHERE from_component_id=? AND to_component_id=?",
            (from_id, to_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"alias edge not found: {first_ref} <-> {second_ref}")
        edge_id = int(row["id"])
        scope_rows = self.conn.execute(
            """
            SELECT sc.*, COALESCE((SELECT json_group_array(source_path)
                                  FROM manual_alias_sources s WHERE s.scope_id=sc.id), '[]') AS source_paths
            FROM manual_alias_scopes sc WHERE sc.edge_id=? AND sc.status='approved'
            """,
            (edge_id,),
        ).fetchall()
        target_project = self._resolve_project_id(project, project_version)
        wanted_sources = self._normalize_scope_sources(sources)
        if target_project is None and min_version is None and max_version is None and not wanted_sources:
            matching_scopes = list(scope_rows)
        else:
            matching_scopes = [
                scope_row
                for scope_row in scope_rows
                if (target_project is None or int(scope_row["project_id"]) == target_project)
                and scope_row["min_version"] == min_version
                and scope_row["max_version"] == max_version
                and scope_row["source_key"] == stable_hash(wanted_sources)
                and tuple(json.loads(scope_row["source_paths"])) == wanted_sources
            ]
        if target_project is not None and not matching_scopes:
            raise KeyError("no approved manual alias scope matches the supplied constraints")
        if len(matching_scopes) > 1:
            raise ValueError("revoke constraints must identify exactly one alias scope")
        scope = matching_scopes[0]
        scope_project_id = int(scope["project_id"])
        self.conn.execute(
            "UPDATE manual_alias_scopes SET status='revoked' WHERE id=?",
            (int(scope["id"]),),
        )
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM manual_alias_scopes WHERE edge_id=? AND status='approved'",
            (edge_id,),
        ).fetchone()[0]
        if remaining == 0:
            self.conn.execute(
                "UPDATE alias_edges SET status='revoked', decided_at=?, decided_by=? WHERE id=?",
                (utc_now(), decided_by, edge_id),
            )
        else:
            self.conn.execute(
                "UPDATE alias_edges SET decided_at=?, decided_by=? WHERE id=?",
                (utc_now(), decided_by, edge_id),
            )
        self.conn.commit()
        affected = self._refresh_projects({scope_project_id}, reason="alias-revoke")
        return {"revoked_edge": edge_id, "revoked_scope": int(scope["id"]), "refreshed_projects": affected}

    def _refresh_projects(self, project_ids: set[int], *, reason: str = "alias-approve") -> list[int]:
        for project_id in project_ids:
            self.refresh_project_components(project_id)
            self.snapshot_project(project_id, reason=reason)
        return sorted(project_ids)

    def _refresh_affected_projects(self, component_ids: set[int], *, reason: str = "alias-approve") -> list[int]:
        if not component_ids:
            return []
        placeholders = ",".join("?" for _ in component_ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT project_id FROM dependency_evidence WHERE component_id IN ({placeholders})",
            tuple(component_ids),
        ).fetchall()
        return self._refresh_projects({int(row[0]) for row in rows}, reason=reason)

    def refresh_project_components(self, project_id: int | None = None) -> None:
        if project_id is not None:
            self.conn.execute("DELETE FROM project_components WHERE project_id=?", (project_id,))
            project_ids = [project_id]
        else:
            self.conn.execute("DELETE FROM project_components")
            project_ids = [int(row[0]) for row in self.conn.execute("SELECT id FROM projects")]
        cursor = self.conn.cursor()
        for project_value in project_ids:
            for group in self._project_group_rows(cursor, project_value):
                self.conn.execute(
                    "INSERT INTO project_components(project_id, component_id, group_key, versions_json, status, evidence_count) "
                    "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(project_id, group_key) DO UPDATE SET "
                    "component_id=excluded.component_id, versions_json=excluded.versions_json, "
                    "status=excluded.status, evidence_count=excluded.evidence_count",
                    (
                        project_value,
                        group["root_component_id"],
                        group["group_key"],
                        json.dumps(group["versions"], ensure_ascii=False),
                        "conflict" if len(group["versions"]) > 1 else "unified",
                        len(group["evidence_ids"]),
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
        group_rows = self._project_group_rows(self.conn.cursor(), int(project["id"]))
        groups_by_key = {group["group_key"]: group for group in group_rows}
        components = self.conn.execute(
            """
            SELECT pc.project_id, pc.component_id, pc.group_key, pc.versions_json,
                   pc.status, pc.evidence_count, c.ecosystem, c.canonical_name, c.purl,
                   c.display_name
            FROM project_components pc JOIN components c ON c.id=pc.component_id
            WHERE pc.project_id=?
            ORDER BY pc.status DESC, c.ecosystem, c.canonical_name
            """,
            (project["id"],),
        ).fetchall()
        result_components = []
        for row in components:
            item = dict(row)
            group = groups_by_key.get(row["group_key"])
            member_ids = group["component_ids"] if group else [int(row["component_id"])]
            item["member_components"] = [
                dict(component_row)
                for component_row in self.conn.execute(
                    f"SELECT * FROM components WHERE id IN ({','.join('?' for _ in member_ids)}) "
                    "ORDER BY canonical_name",
                    member_ids,
                ).fetchall()
            ]
            item["source_paths"] = group["source_paths"] if group else []
            item["location_paths"] = group["location_paths"] if group else []
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
        evidence_group_by_id = {
            evidence_id: group["group_key"]
            for group in group_rows
            for evidence_id in group["evidence_ids"]
        }
        for row in evidence:
            item = dict(row)
            raw_component_id = int(item["component_id"])
            group_key = evidence_group_by_id.get(int(row["id"]))
            header = next(
                component
                for component in result_components
                if component["group_key"] == group_key
            ) if group_key else None
            root_id = int(header["component_id"]) if header else raw_component_id
            root = self.conn.execute(
                "SELECT id, ecosystem, canonical_name, purl, display_name FROM components WHERE id=?",
                (root_id,),
            ).fetchone()
            item["logical_component_id"] = root_id
            item["logical_purl"] = root["purl"]
            item["logical_canonical_name"] = root["canonical_name"]
            item["group_key"] = group_key or next(
                (
                    component["group_key"]
                    for component in result_components
                    if int(component["component_id"]) == raw_component_id
                ),
                f"c{raw_component_id}",
            )
            result_evidence.append(item)
        manual_scopes = self.manual_alias_scopes(project_id=int(project["id"]))
        return {
            "project": {"name": project["name"], "version": project["version"]},
            "components": result_components,
            "evidence": result_evidence,
            "manual_alias_scopes": manual_scopes,
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

    def manual_alias_scopes(self, project_id: int | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT sc.id AS scope_id, sc.edge_id, sc.project_id, p.name AS project_name,
                   p.version AS project_version, sc.min_version, sc.max_version,
                   e.status, e.created_at, e.decided_at, e.decided_by,
                   c1.purl AS from_purl, c2.purl AS to_purl,
                   COALESCE((SELECT json_group_array(source_path)
                             FROM manual_alias_sources s WHERE s.scope_id=sc.id), '[]') AS source_paths
            FROM manual_alias_scopes sc
            JOIN alias_edges e ON e.id=sc.edge_id
            JOIN projects p ON p.id=sc.project_id
            JOIN components c1 ON c1.id=e.from_component_id
            JOIN components c2 ON c2.id=e.to_component_id
        """
        params: tuple[Any, ...] = ()
        if project_id is not None:
            sql += " WHERE sc.project_id=?"
            params = (project_id,)
        sql += " ORDER BY sc.edge_id, sc.id"
        return [
            {
                **dict(row),
                "source_paths": json.loads(row["source_paths"]),
                "match_all_sources": not bool(json.loads(row["source_paths"])),
            }
            for row in self.conn.execute(sql, params).fetchall()
        ]
