from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
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
    project_versions_json TEXT,
    note TEXT,
    reject_reason TEXT,
    UNIQUE(from_component_id, to_component_id)
);

CREATE TABLE IF NOT EXISTS inheritance_plans (
    id INTEGER PRIMARY KEY,
    project_name TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    UNIQUE(project_name, to_version)
);

CREATE TABLE IF NOT EXISTS inheritance_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES inheritance_plans(id),
    source_edge_id INTEGER REFERENCES alias_edges(id),
    inherited_edge_id INTEGER REFERENCES alias_edges(id),
    classification TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'inherited', 'narrowed', 'skipped', 'new')),
    detail_json TEXT NOT NULL,
    signature_json TEXT,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT,
    UNIQUE(plan_id, source_edge_id)
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
        if "project_versions_json" not in columns:
            self.conn.execute("ALTER TABLE alias_edges ADD COLUMN project_versions_json TEXT")
            self.conn.commit()
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

    @contextmanager
    def _transaction(self):
        try:
            self.conn.execute("BEGIN")
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def import_manifest(self, manifest: ParsedManifest, content: bytes) -> dict[str, int]:
        content_hash = hashlib.sha256(content).hexdigest()
        now = utc_now()
        with self._transaction():
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
            plan = self._prepare_inheritance_review(
                cursor, project_id, manifest.project, manifest.project_version
            )
            self._refresh_project_components(cursor, project_id)
            self._insert_snapshot(cursor, project_id, reason="import")
        return {
            "project_id": project_id,
            "source_id": source_id,
            "inserted_records": inserted,
            "total_records": len(manifest.evidence),
            "inheritance_plan_id": plan,
        }

    def _insert_evidence(
        self,
        cursor: sqlite3.Cursor,
        project_id: int,
        source_id: int,
        evidence: Iterable[DependencyEvidence],
    ) -> int:
        inserted = 0
        normalized = tuple(entry.normalized() for entry in evidence)
        component_ids = {id(entry): self._component_id(cursor, item) for entry, item in zip(evidence, normalized)}
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
            self._register_manifest_aliases(cursor, item, component_id, project_id, source_id)
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
        self,
        cursor: sqlite3.Cursor,
        item: DependencyEvidence,
        source_component_id: int,
        project_id: int,
        source_id: int,
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
            edge_id = self._upsert_alias_edge(
                cursor, source_component_id, alias_component_id, origin="manifest", status="approved"
            )
            stored = cursor.execute("SELECT status FROM alias_edges WHERE id=?", (edge_id,)).fetchone()
            if edge_id is not None and stored is not None and stored["status"] == "approved":
                self._scope_edge_to_project(cursor, edge_id, project_id, source_id)

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

    def _project_version(self, project_id: int) -> str:
        row = self.conn.execute("SELECT version FROM projects WHERE id=?", (project_id,)).fetchone()
        return row["version"] if row is not None else ""

    @staticmethod
    def _edge_sources(edge: sqlite3.Row) -> list[str]:
        try:
            sources = json.loads(edge["sources_json"])
        except (KeyError, TypeError, json.JSONDecodeError):
            sources = ["*"]
        return sources or ["*"]

    def _edge_project_scope(
        self, edge: sqlite3.Row | dict, project_name: str, project_version: str | None = None
    ) -> dict[str, Any] | None:
        keys = tuple(edge.keys())
        raw = edge["project_versions_json"] if "project_versions_json" in keys else None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            entry = data.get(project_name, data.get("*"))
            if entry is None:
                entries: list[dict[str, Any]] = []
            elif isinstance(entry, list):
                entries = [
                    {
                        "versions": list(item) if isinstance(item, list) else item,
                        "range": "*",
                        "source_paths": None,
                    }
                    for item in entry
                ]
            elif isinstance(entry, dict) and isinstance(entry.get("scopes"), list):
                entries = entry["scopes"]
            elif isinstance(entry, dict):
                entries = [entry]
            else:
                entries = []
        elif isinstance(data, list):
            entries = [{"versions": list(data), "range": "*", "source_paths": None}]
        else:
            entries = []
        if project_version is not None and entries:
            entries = [item for item in entries if project_version in (item.get("versions") or [])]
        if not entries:
            return None
        if any(not isinstance(item, dict) for item in entries):
            return None
        if any(item.get("enabled") is False for item in entries):
            return {"versions": [], "range": "*", "source_paths": []}
        scope_versions = [version for item in entries for version in (item.get("versions") or [])]
        paths = {path for item in entries for path in (item.get("source_paths") or [])}
        return {
            "versions": [str(item) for item in scope_versions],
            "range": str(entries[0].get("range") or "*") if len(entries) == 1 else "*",
            "source_paths": sorted(str(item) for item in paths) if paths else None,
        }

    def _project_row(self, cursor: sqlite3.Cursor, project_id: int) -> sqlite3.Row:
        row = cursor.execute("SELECT id, name, version FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"project not found: {project_id}")
        return row

    def _scope_edge_to_project(
        self,
        cursor: sqlite3.Cursor,
        edge_id: int,
        project_id: int,
        source_id: int | None = None,
        *,
        version_range: str = "*",
    ) -> None:
        project = self._project_row(cursor, project_id)
        source_row = None
        if source_id is not None:
            source_row = cursor.execute(
                "SELECT source_path FROM manifest_sources WHERE id=?", (source_id,)
            ).fetchone()
        source_paths = [source_row["source_path"]] if source_row is not None else None
        row = cursor.execute(
            "SELECT sources_json, project_versions_json FROM alias_edges WHERE id=?", (edge_id,)
        ).fetchone()
        try:
            scopes = json.loads(row["project_versions_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            scopes = {}
        if not isinstance(scopes, dict):
            scopes = {}
        project_scopes = scopes.get(project["name"], {})
        existing_scopes = project_scopes.get("scopes", []) if isinstance(project_scopes, dict) else []
        scopes[project["name"]] = {
            "scopes": existing_scopes
            + [
                {
                    "versions": [project["version"]],
                    "range": version_range or "*",
                    "source_paths": source_paths,
                }
            ]
        }
        cursor.execute(
            "UPDATE alias_edges SET project_versions_json=? WHERE id=?",
            (json.dumps(scopes, ensure_ascii=False, sort_keys=True), edge_id),
        )

    def _pending_inheritance_item(
        self, cursor: sqlite3.Cursor, project_name: str, project_version: str, edge_id: int
    ) -> sqlite3.Row | None:
        return cursor.execute(
            """
            SELECT i.* FROM inheritance_items i
            JOIN inheritance_plans p ON p.id=i.plan_id
            WHERE p.project_name=? AND p.to_version=? AND i.source_edge_id=? AND i.status='pending'
            """,
            (project_name, project_version, edge_id),
        ).fetchone()

    def _edge_has_pending_inheritance(
        self, cursor: sqlite3.Cursor, edge_id: int, project_name: str, project_version: str
    ) -> bool:
        return self._pending_inheritance_item(cursor, project_name, project_version, edge_id) is not None

    def _previous_project_version(
        self, cursor: sqlite3.Cursor, project_name: str, current_version: str
    ) -> str | None:
        rows = cursor.execute(
            "SELECT version FROM projects WHERE name=? AND version<>?", (project_name, current_version)
        ).fetchall()
        previous_version = None
        for row in rows:
            candidate = row[0]
            if previous_version is None or compare_versions(candidate, previous_version) > 0:
                previous_version = candidate
        if previous_version is None:
            return None
        return previous_version

    def _current_component_evidence(
        self, cursor: sqlite3.Cursor, project_id: int
    ) -> dict[int, list[sqlite3.Row]]:
        rows = cursor.execute(
            """
            SELECT e.id, e.component_id, e.version, e.location_path, e.display_name,
                   s.source_path, c.ecosystem, c.canonical_name, c.purl
            FROM dependency_evidence e
            JOIN manifest_sources s ON s.id=e.source_id
            JOIN components c ON c.id=e.component_id
            WHERE e.project_id=?
            ORDER BY e.id
            """,
            (project_id,),
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["component_id"]), []).append(row)
        return grouped

    def _current_component_by_identity(
        self, current_evidence: dict[int, list[sqlite3.Row]], ecosystem: str, canonical_name: str
    ) -> sqlite3.Row | None:
        for rows in current_evidence.values():
            row = rows[0]
            if row["ecosystem"] == ecosystem and row["canonical_name"] == canonical_name:
                return row
        return None

    def _current_component_by_name(
        self, current_evidence: dict[int, list[sqlite3.Row]], canonical_name: str
    ) -> sqlite3.Row | None:
        for rows in current_evidence.values():
            if rows[0]["canonical_name"] == canonical_name:
                return rows[0]
        return None

    def _prepare_inheritance_review(
        self,
        cursor: sqlite3.Cursor,
        project_id: int,
        project_name: str,
        project_version: str,
    ) -> int | None:
        previous_version = self._previous_project_version(cursor, project_name, project_version)
        if previous_version is None:
            return None
        previous = cursor.execute(
            "SELECT id FROM projects WHERE name=? AND version=?", (project_name, previous_version)
        ).fetchone()
        old_edges = cursor.execute(
            """
            SELECT DISTINCT a.* FROM alias_edges a
            JOIN dependency_evidence e ON e.component_id IN (a.from_component_id, a.to_component_id)
            WHERE a.status='approved' AND e.project_id=?
            ORDER BY a.id
            """,
            (previous["id"],),
        ).fetchall()
        if not old_edges:
            return None
        current_evidence = self._current_component_evidence(cursor, project_id)
        plan_row = cursor.execute(
            "INSERT INTO inheritance_plans(project_name, from_version, to_version, created_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(project_name, to_version) DO UPDATE SET project_name=excluded.project_name "
            "RETURNING id",
            (project_name, previous_version, project_version, utc_now()),
        ).fetchone()
        plan_id = int(plan_row[0])
        created = 0
        for edge in old_edges:
            existing = cursor.execute(
                "SELECT id FROM inheritance_items WHERE plan_id=? AND source_edge_id=?",
                (plan_id, int(edge["id"])),
            ).fetchone()
            if existing is not None:
                continue
            endpoints = []
            for key in ("from_component_id", "to_component_id"):
                component = self._component_row(cursor, int(edge[key]))
                same_identity = self._current_component_by_identity(
                    current_evidence, component["ecosystem"], component["canonical_name"]
                )
                current = same_identity or self._current_component_by_name(
                    current_evidence, component["canonical_name"]
                )
                endpoints.append({
                    "old_component": dict(component),
                    "current_evidence": current,
                    "same_identity": same_identity is not None,
                })
            current_rows = [
                row
                for endpoint in endpoints
                if endpoint["current_evidence"] is not None
                for row in current_evidence.get(int(endpoint["current_evidence"]["component_id"]), [])
            ]
            if not current_rows:
                continue
            old_ecosystems = {endpoint["old_component"]["ecosystem"] for endpoint in endpoints}
            current_ecosystems = {row["ecosystem"] for row in current_rows}
            same_identity_count = sum(endpoint["same_identity"] for endpoint in endpoints)
            if len(old_ecosystems | current_ecosystems) > 1 or same_identity_count < 2:
                classification = "ecosystem_or_identity_changed"
            else:
                old_paths = {
                    row["source_path"]
                    for row in cursor.execute(
                        """
                        SELECT DISTINCT s.source_path FROM dependency_evidence e
                        JOIN manifest_sources s ON s.id=e.source_id
                        WHERE e.project_id=? AND e.component_id IN (?, ?)
                        """,
                        (previous["id"], int(edge["from_component_id"]), int(edge["to_component_id"])),
                    ).fetchall()
                }
                new_paths = {row["source_path"] for row in current_rows}
                classification = "source_changed" if old_paths != new_paths else "version_changed"
            detail = {
                "endpoints": [
                    {
                        "old_component": endpoint["old_component"],
                        "current_evidence": (
                            {
                                "evidence_id": int(endpoint["current_evidence"]["id"]),
                                "component_id": int(endpoint["current_evidence"]["component_id"]),
                                "ecosystem": endpoint["current_evidence"]["ecosystem"],
                                "canonical_name": endpoint["current_evidence"]["canonical_name"],
                                "purl": endpoint["current_evidence"]["purl"],
                            }
                            if endpoint["current_evidence"] is not None
                            else None
                        ),
                        "same_identity": endpoint["same_identity"],
                    }
                    for endpoint in endpoints
                ],
                "affected_evidence": [
                    {
                        "evidence_id": int(row["id"]),
                        "ecosystem": row["ecosystem"],
                        "canonical_name": row["canonical_name"],
                        "purl": row["purl"],
                        "version": row["version"],
                        "source_path": row["source_path"],
                        "location_path": row["location_path"],
                    }
                    for row in current_rows
                ],
                "old_version": previous_version,
                "new_version": project_version,
                "old_version_range": edge["version_range"],
                "old_sources": self._edge_sources(edge),
            }
            signature = stable_hash(
                {
                    "evidence": [(item["evidence_id"], item["version"], item["source_path"], item["location_path"]) for item in detail["affected_evidence"]],
                    "edge": int(edge["id"]),
                }
            )
            cursor.execute(
                """
                INSERT INTO inheritance_items(
                    plan_id, source_edge_id, inherited_edge_id, classification, status,
                    detail_json, signature_json
                ) VALUES(?, ?, NULL, ?, 'pending', ?, ?)
                """,
                (
                    plan_id,
                    int(edge["id"]),
                    classification,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    signature,
                ),
            )
            created += 1
        if created == 0 and cursor.execute(
            "SELECT COUNT(*) FROM inheritance_items WHERE plan_id=?", (plan_id,)
        ).fetchone()[0] == 0:
            cursor.execute("DELETE FROM inheritance_plans WHERE id=?", (plan_id,))
            return None
        return plan_id

    def _plan_row(self, plan_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM inheritance_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(f"inheritance plan not found: #{plan_id}")
        return row

    def inheritance_review(self, project_name: str, project_version: str) -> dict[str, Any] | None:
        plan = self.conn.execute(
            "SELECT * FROM inheritance_plans WHERE project_name=? AND to_version=?",
            (project_name, project_version),
        ).fetchone()
        if plan is None:
            return None
        items = self.conn.execute(
            "SELECT * FROM inheritance_items WHERE plan_id=? ORDER BY id", (int(plan["id"]),)
        ).fetchall()
        return {
            "plan_id": int(plan["id"]),
            "project": plan["project_name"],
            "from_version": plan["from_version"],
            "to_version": plan["to_version"],
            "finalized_at": plan["finalized_at"],
            "items": [self._inheritance_item_dict(row) for row in items],
        }

    def _inheritance_item_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        source = self.edge_dict(self._edge_row(int(row["source_edge_id"])))
        detail = json.loads(row["detail_json"])
        inherited = None
        if row["inherited_edge_id"] is not None:
            inherited = self.edge_dict(self._edge_row(int(row["inherited_edge_id"])))
        return {
            "id": int(row["id"]),
            "plan_id": int(row["plan_id"]),
            "source_edge": source,
            "inherited_edge": inherited,
            "classification": row["classification"],
            "status": row["status"],
            "detail": detail,
            "affected_evidence": detail.get("affected_evidence", []),
            "signature": row["signature_json"],
            "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "note": row["note"],
        }

    def decide_inheritance(
        self,
        plan_id: int,
        item_ids: list[int] | None = None,
        decision: str = "inherit",
        *,
        version_range: str = "*",
        decided_by: str = "operator",
        note: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"inherit", "narrowed", "skipped"}:
            raise ValueError("decision must be inherit, narrowed or skipped")
        status = "inherited" if decision == "inherit" else decision
        in_range("0", version_range)
        with self._transaction():
            plan = self._plan_row(plan_id)
            rows = self._review_item_rows(plan_id, item_ids)
            results = []
            affected_components: set[int] = set()
            project = self.conn.execute(
                "SELECT id FROM projects WHERE name=? AND version=?",
                (plan["project_name"], plan["to_version"]),
            ).fetchone()
            if project is None:
                raise KeyError(f"project not found: {plan['project_name']} {plan['to_version']}")
            for row in rows:
                if row["status"] != "pending":
                    raise ValueError(f"inheritance item #{row['id']} is {row['status']}, not pending")
                detail = json.loads(row["detail_json"])
                edge = self._edge_row(int(row["source_edge_id"]))
                if decision != "skipped":
                    if row["classification"] == "ecosystem_or_identity_changed":
                        raise ValueError(
                            f"inheritance item #{row['id']} changed ecosystem or identity; choose skipped"
                        )
                    affected_versions = {item["version"] for item in detail["affected_evidence"]}
                    if decision == "narrowed" and not all(in_range(version, version_range) for version in affected_versions):
                        raise ValueError("narrowed range excludes affected evidence in the new version")
                    source_paths = {item["source_path"] for item in detail["affected_evidence"]}
                    self._add_edge_version_scope(
                        edge,
                        plan["project_name"],
                        plan["to_version"],
                        plan["from_version"],
                        sorted(source_paths),
                        version_range=version_range,
                        enabled=True,
                    )
                    inherited_edge_id = int(edge["id"])
                    affected_components.update((int(edge["from_component_id"]), int(edge["to_component_id"])))
                else:
                    self._add_edge_version_scope(
                        edge,
                        plan["project_name"],
                        plan["to_version"],
                        plan["from_version"],
                        [],
                        version_range="*",
                        enabled=False,
                    )
                    inherited_edge_id = None
                self.conn.execute(
                    "UPDATE inheritance_items SET status=?, inherited_edge_id=?, decided_at=?, decided_by=?, note=? "
                    "WHERE id=?",
                    (status, inherited_edge_id, utc_now(), decided_by, note, int(row["id"])),
                )
                results.append(self._inheritance_item_dict(self._review_item_rows(plan_id, [int(row["id"])])[0]))
            refreshed = self._refresh_component_projects(
                self.conn, affected_components, int(project["id"]), reason="inheritance-decision"
            )
            pending_count = self.conn.execute(
                "SELECT COUNT(*) FROM inheritance_items WHERE plan_id=? AND status='pending'", (plan_id,)
            ).fetchone()[0]
            self.conn.execute(
                "UPDATE inheritance_plans SET finalized_at=? WHERE id=?",
                (utc_now() if pending_count == 0 else None, plan_id),
            )
        return {"decided": results, "refreshed_projects": refreshed, "review": self.inheritance_review(plan["project_name"], plan["to_version"])}

    def _review_item_rows(self, plan_id: int, item_ids: list[int] | None) -> list[sqlite3.Row]:
        if item_ids is None:
            return self.conn.execute(
                "SELECT * FROM inheritance_items WHERE plan_id=? AND status='pending' ORDER BY id", (plan_id,)
            ).fetchall()
        rows = []
        for item_id in item_ids:
            row = self.conn.execute(
                "SELECT * FROM inheritance_items WHERE plan_id=? AND id=?", (plan_id, item_id)
            ).fetchone()
            if row is None:
                raise KeyError(f"inheritance item not found: #{item_id}")
            rows.append(row)
        return rows

    def _add_edge_version_scope(
        self,
        edge: sqlite3.Row,
        project_name: str,
        project_version: str,
        baseline_version: str,
        source_paths: list[str],
        *,
        version_range: str,
        enabled: bool,
    ) -> None:
        try:
            scopes = json.loads(edge["project_versions_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            scopes = {}
        if not isinstance(scopes, dict):
            scopes = {}
        project_entry = scopes.get(project_name)
        if not isinstance(project_entry, dict) or not isinstance(project_entry.get("scopes"), list):
            project_entry = {"scopes": []}
        existing_versions = {
            version
            for item in project_entry["scopes"]
            for version in (item.get("versions") if isinstance(item, dict) else [])
        }
        new_entries = []
        if baseline_version not in existing_versions:
            new_entries.append(
                {"versions": [baseline_version], "range": "*", "source_paths": None, "enabled": True}
            )
        if project_version not in existing_versions:
            new_entries.append(
            {
                "versions": [project_version],
                "range": version_range or "*",
                "source_paths": source_paths,
                "enabled": enabled,
            }
            )
        project_entry["scopes"].extend(new_entries)
        scopes[project_name] = project_entry
        self.conn.execute(
            "UPDATE alias_edges SET sources_json=?, project_versions_json=? WHERE id=?",
            (edge["sources_json"], json.dumps(scopes, ensure_ascii=False, sort_keys=True), int(edge["id"])),
        )

    def reopen_inheritance_items(
        self, plan_id: int, item_ids: list[int], *, decided_by: str = "operator"
    ) -> dict[str, Any]:
        plan = self._plan_row(plan_id)
        with self._transaction():
            project = self.conn.execute(
                "SELECT id FROM projects WHERE name=? AND version=?",
                (plan["project_name"], plan["to_version"]),
            ).fetchone()
            affected: set[int] = set()
            for item_id in item_ids:
                row = self.conn.execute(
                    "SELECT * FROM inheritance_items WHERE plan_id=? AND id=?", (plan_id, item_id)
                ).fetchone()
                if row is None:
                    raise KeyError(f"inheritance item not found: #{item_id}")
                if row["status"] == "pending":
                    continue
                edge = self._edge_row(int(row["source_edge_id"]))
                self._remove_edge_version_scope(int(edge["id"]), plan["project_name"], plan["to_version"])
                affected.update((int(edge["from_component_id"]), int(edge["to_component_id"])))
                self.conn.execute(
                    "UPDATE inheritance_items SET status='pending', inherited_edge_id=NULL, decided_at=?, decided_by=?, note=NULL WHERE id=?",
                    (utc_now(), decided_by, item_id),
                )
                self.conn.execute("UPDATE inheritance_plans SET finalized_at=NULL WHERE id=?", (plan_id,))
            refreshed = self._refresh_component_projects(
                self.conn, affected, int(project["id"]), reason="inheritance-reopen"
            )
        return {"reopened": item_ids, "refreshed_projects": refreshed, "review": self.inheritance_review(plan["project_name"], plan["to_version"])}

    def _remove_edge_version_scope(self, edge_id: int, project_name: str, project_version: str) -> None:
        row = self._edge_row(edge_id)
        try:
            scopes = json.loads(row["project_versions_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            scopes = {}
        if not isinstance(scopes, dict) or project_name not in scopes:
            return
        entry = scopes[project_name]
        entries = entry.get("scopes", []) if isinstance(entry, dict) else []
        entries = [item for item in entries if project_version not in item.get("versions", [])]
        if entries:
            scopes[project_name] = {"scopes": entries}
        else:
            scopes.pop(project_name, None)
        self.conn.execute(
            "UPDATE alias_edges SET project_versions_json=? WHERE id=?",
            (json.dumps(scopes, ensure_ascii=False, sort_keys=True) if scopes else None, edge_id),
        )

    def _edge_applies_to_project(
        self, edge: sqlite3.Row | dict, project_name: str, project_version: str | None = None
    ) -> bool:
        edge_id = int(edge["id"]) if "id" in edge.keys() else None
        if edge_id is not None and project_version is not None and self._edge_has_pending_inheritance(
            self.conn.cursor(), edge_id, project_name, project_version
        ):
            return False
        sources = self._edge_sources(edge)
        if sources != ["*"] and project_name not in sources:
            return False
        scope = self._edge_project_scope(edge, project_name, project_version)
        if scope is None:
            return True
        if project_version is not None and scope["versions"] is not None and project_version not in scope["versions"]:
            return False
        return True

    def _component_versions_in_project(self, component_id: int, project_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT version FROM dependency_evidence WHERE component_id=? AND project_id=?",
            (component_id, project_id),
        ).fetchall()
        return [row[0] for row in rows if row[0]]

    def _compute_groups(
        self, edges: list[sqlite3.Row | dict], project: tuple[int, str, str] | None = None
    ) -> dict[int, int]:
        component_parent: dict[int, int] = {}

        def find(value: int) -> int:
            component_parent.setdefault(value, value)
            while component_parent[value] != value:
                component_parent[value] = component_parent[component_parent[value]]
                value = component_parent[value]
            return value

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                component_parent[max(left_root, right_root)] = min(left_root, right_root)

        for edge in edges:
            from_id, to_id = int(edge["from_component_id"]), int(edge["to_component_id"])
            if project is not None:
                project_id, project_name, project_version = project
                if not self._edge_applies_to_project(edge, project_name, project_version):
                    continue
                project_scope = self._edge_project_scope(edge, project_name, project_version) or {}
                version_range = project_scope.get("range") or edge["version_range"] or "*"
                from_versions = self._component_versions_in_project(from_id, project_id)
                to_versions = self._component_versions_in_project(to_id, project_id)
                if not any(in_range(version, version_range) for version in from_versions):
                    continue
                if not any(in_range(version, version_range) for version in to_versions):
                    continue
            union(from_id, to_id)
        return {component_id: find(component_id) for component_id in component_parent}

    def alias_groups(self, project_id: int | None = None) -> dict[int, int]:
        project = None
        if project_id is not None:
            row = self.conn.execute(
                "SELECT id, name, version FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if row is not None:
                project = (int(row["id"]), row["name"], row["version"])
        return self._compute_groups(self._approved_edge_rows(), project)

    def _evidence_rows(self, project_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT e.id AS evidence_id, e.component_id, e.version, e.location_path, s.source_path "
            "FROM dependency_evidence e JOIN manifest_sources s ON s.id=e.source_id "
            "WHERE e.project_id=? ORDER BY e.id",
            (project_id,),
        ).fetchall()

    def logical_groups(self, project_id: int) -> dict[int, int]:
        """Map every evidence row id to its logical component root for one project."""
        project = self.conn.execute("SELECT id, name, version FROM projects WHERE id=?", (project_id,)).fetchone()
        if project is None:
            return {}
        project_name, project_version = project["name"], project["version"]
        rows = self._evidence_rows(project_id)
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

        by_component: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            component_id = int(row["component_id"])
            by_component.setdefault(component_id, []).append(row)
            find(component_id)
        for edge in self._approved_edge_rows():
            if not self._edge_applies_to_project(edge, project_name, project_version):
                continue
            scope = self._edge_project_scope(edge, project_name, project_version) or {}
            version_range = scope.get("range") or edge["version_range"] or "*"
            allowed_paths = scope.get("source_paths")
            matched: list[int] = []
            for endpoint_key in ("from_component_id", "to_component_id"):
                endpoint_matches = []
                for evidence in by_component.get(int(edge[endpoint_key]), []):
                    if allowed_paths is not None and evidence["source_path"] not in allowed_paths:
                        continue
                    if evidence["version"] and not in_range(evidence["version"], version_range):
                        continue
                    endpoint_matches.append(int(evidence["evidence_id"]))
                if not endpoint_matches:
                    matched = []
                    break
                matched.extend(endpoint_matches)
            if matched:
                union(int(edge["from_component_id"]), int(edge["to_component_id"]))
        return {int(row["evidence_id"]): find(int(row["component_id"])) for row in rows}

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
            "project_versions_json": None,
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
            groups = self._compute_groups(simulated, (project_id, project_name, self._project_version(project_id)))
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
        if existing is not None and existing["status"] == "approved":
            edge = self.edge_dict(self._edge_row(int(existing["id"])))
            return {
                "preview": self.alias_preview(
                    first_ref,
                    second_ref,
                    version_range=edge["version_range"],
                    sources=edge["sources"],
                ),
                "refreshed_projects": [],
                "edge_id": int(existing["id"]),
            }
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
        with self._transaction():
            self.conn.execute(
                "INSERT INTO alias_edges(from_component_id, to_component_id, origin, status, created_at, decided_at, decided_by, version_range, sources_json, note) "
                "VALUES(?, ?, 'manual', 'approved', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(from_component_id, to_component_id) DO UPDATE SET "
                "status='approved', decided_at=excluded.decided_at, decided_by=excluded.decided_by, "
                "version_range=excluded.version_range, sources_json=excluded.sources_json, "
                "project_versions_json=NULL, note=COALESCE(excluded.note, alias_edges.note)",
                (from_id, to_id, now, now, decided_by, version_range or "*", json.dumps(scope_sources), note),
            )
            affected = self._refresh_component_projects(
                self.conn.cursor(), {from_id, to_id}, reason="alias-approve"
            )
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
        with self._transaction():
            self.conn.execute(
                "UPDATE alias_edges SET status='revoked', decided_at=?, decided_by=? WHERE id=?",
                (utc_now(), decided_by, edge_id),
            )
            affected = self._refresh_component_projects(
                self.conn.cursor(), affected_components, reason="alias-revoke"
            )
        recheck = self._recheck_pending_after_revoke({from_id, to_id})
        return {"revoked_edge": edge_id, "refreshed_projects": affected, "affected_pending": recheck["affected_pending"], "remaining_preview": recheck["remaining_preview"]}

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
                groups = self._compute_groups(simulated, (project_id, project_name, self._project_version(project_id)))
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
        affected_components: set[int] = set()
        approved = []
        now = utc_now()
        with self._transaction():
            for row in rows:
                self.conn.execute(
                    "UPDATE alias_edges SET status='approved', decided_at=?, decided_by=?, "
                    "note=COALESCE(?, note) WHERE id=?",
                    (now, decided_by, note, int(row["id"])),
                )
                affected_components.add(int(row["from_component_id"]))
                affected_components.add(int(row["to_component_id"]))
                approved.append(self.edge_dict(self._edge_row(int(row["id"]))))
            refreshed = self._refresh_component_projects(
                self.conn.cursor(), affected_components, reason="alias-approve"
            )
        remaining = self.pending_edges()
        remaining_preview = self.joint_preview() if remaining else {"candidates": [], "conflicts": []}
        return {"approved": approved, "refreshed_projects": refreshed, "remaining_pending": remaining, "remaining_preview": remaining_preview}

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

    def _refresh_component_projects(
        self,
        cursor: sqlite3.Cursor,
        component_ids: set[int],
        force_project_id: int | None = None,
        *,
        reason: str,
    ) -> list[int]:
        if not component_ids:
            project_ids = [force_project_id] if force_project_id is not None else []
            for project_id in project_ids:
                self._insert_snapshot(cursor, project_id, reason=reason)
            return project_ids
        placeholders = ",".join("?" for _ in component_ids)
        rows = cursor.execute(
            f"SELECT DISTINCT project_id FROM dependency_evidence WHERE component_id IN ({placeholders})",
            tuple(component_ids),
        ).fetchall()
        project_ids = [int(row[0]) for row in rows]
        if force_project_id is not None and force_project_id not in project_ids:
            project_ids.append(force_project_id)
        for project_id in project_ids:
            self._refresh_project_components(cursor, project_id)
            self._insert_snapshot(cursor, project_id, reason=reason)
        return project_ids

    def _refresh_affected_projects(self, component_ids: set[int], *, reason: str = "alias-approve") -> list[int]:
        with self._transaction():
            return self._refresh_component_projects(self.conn.cursor(), component_ids, reason=reason)

    def _refresh_project_components(self, cursor: sqlite3.Cursor, project_id: int | None = None) -> None:
        if project_id is not None:
            target_ids = [project_id]
            cursor.execute("DELETE FROM project_components WHERE project_id=?", (project_id,))
        else:
            target_ids = [int(row[0]) for row in cursor.execute("SELECT id FROM projects").fetchall()]
            cursor.execute("DELETE FROM project_components")
        for target_id in target_ids:
            groups = self.logical_groups(target_id)
            rows = cursor.execute(
                "SELECT id, component_id, version FROM dependency_evidence WHERE project_id=?",
                (target_id,),
            ).fetchall()
            aggregated: dict[int, dict[str, Any]] = {}
            for row in rows:
                root_id = groups[int(row["id"])]
                bucket = aggregated.setdefault(root_id, {"versions": set(), "evidence_count": 0})
                if row["version"]:
                    bucket["versions"].add(row["version"])
                bucket["evidence_count"] += 1
            for root_id, bucket in aggregated.items():
                versions = sorted(bucket["versions"])
                cursor.execute(
                    "INSERT INTO project_components(project_id, component_id, versions_json, status, evidence_count) "
                    "VALUES(?, ?, ?, ?, ?) ON CONFLICT(project_id, component_id) DO UPDATE SET "
                    "versions_json=excluded.versions_json, status=excluded.status, evidence_count=excluded.evidence_count",
                    (
                        target_id,
                        root_id,
                        json.dumps(versions, ensure_ascii=False),
                        "conflict" if len(versions) > 1 else "unified",
                        bucket["evidence_count"],
                    ),
                )

    def refresh_project_components(self, project_id: int | None = None) -> None:
        with self._transaction():
            self._refresh_project_components(self.conn.cursor(), project_id)

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
        logical_evidence_groups = self.logical_groups(project["id"])
        evidence_components = self.conn.execute(
            "SELECT id, component_id FROM dependency_evidence WHERE project_id=?", (project["id"],)
        ).fetchall()
        groups = {int(row["component_id"]): logical_evidence_groups[int(row["id"])] for row in evidence_components}
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

    def _insert_snapshot(self, cursor: sqlite3.Cursor, project_id: int, *, reason: str) -> int | None:
        project = cursor.execute(
            "SELECT name, version FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            return None
        report = self.project_report(project["name"], project["version"])
        now = utc_now()
        result = cursor.execute(
            "INSERT INTO project_snapshots(project_id, taken_at, reason, report_json) VALUES(?, ?, ?, ?)",
            (project_id, now, reason, json.dumps(report, ensure_ascii=False, sort_keys=True)),
        )
        return int(result.lastrowid)

    def snapshot_project(self, project_id: int, *, reason: str) -> int | None:
        with self._transaction():
            return self._insert_snapshot(self.conn.cursor(), project_id, reason=reason)

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
