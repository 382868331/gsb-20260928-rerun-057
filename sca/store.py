from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .models import ParsedManifest
from .parsers import canonicalize_pypi_name, parse_manifest_with_project


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS manifests (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_path TEXT NOT NULL,
    format TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    purl TEXT,
    UNIQUE(ecosystem, canonical_name)
);

CREATE TABLE IF NOT EXISTS component_names (
    component_id INTEGER NOT NULL REFERENCES components(id),
    display_name TEXT NOT NULL,
    PRIMARY KEY(component_id, display_name)
);

CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY,
    component_id INTEGER NOT NULL REFERENCES components(id),
    manifest_id INTEGER NOT NULL REFERENCES manifests(id),
    artifact_path TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    dependency_type TEXT NOT NULL,
    version TEXT,
    version_spec TEXT,
    dependency_chain TEXT NOT NULL,
    raw_record TEXT NOT NULL,
    UNIQUE(manifest_id, record_index)
);

CREATE TABLE IF NOT EXISTS dependency_edges (
    id INTEGER PRIMARY KEY,
    manifest_id INTEGER NOT NULL REFERENCES manifests(id),
    component_id INTEGER NOT NULL REFERENCES components(id),
    parent_ecosystem TEXT,
    parent_name TEXT,
    chain TEXT NOT NULL,
    UNIQUE(manifest_id, component_id, parent_ecosystem, parent_name, chain)
);

CREATE TABLE IF NOT EXISTS version_findings (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    component_id INTEGER NOT NULL REFERENCES components(id),
    status TEXT NOT NULL CHECK(status IN ('resolved', 'conflict', 'unresolved')),
    selected_version TEXT,
    versions TEXT NOT NULL,
    PRIMARY KEY(project_id, component_id)
);
"""


def connect(database: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)
    return connection


def import_manifest(
    connection: sqlite3.Connection,
    manifest_or_path: ParsedManifest | str | Path,
    default_artifact: str | None = None,
    project_name: str | None = None,
    project_version: str | None = None,
) -> tuple[bool, int]:
    manifest = (
        manifest_or_path
        if isinstance(manifest_or_path, ParsedManifest)
        else parse_manifest_with_project(
            manifest_or_path, project_name, project_version, default_artifact
        )
    )
    existing = connection.execute(
        "SELECT id FROM manifests WHERE content_hash = ?", (manifest.content_hash,)
    ).fetchone()
    if existing:
        return False, int(existing["id"])

    with connection:
        project_id = connection.execute(
            "INSERT INTO projects(name, version) VALUES(?, ?) "
            "ON CONFLICT(name, version) DO UPDATE SET name=excluded.name RETURNING id",
            (manifest.project_name, manifest.project_version),
        ).fetchone()[0]
        manifest_id = connection.execute(
            "INSERT INTO manifests(project_id, source_path, format, content_hash) VALUES(?, ?, ?, ?)",
            (project_id, manifest.source_path, manifest.format, manifest.content_hash),
        ).lastrowid

        for record in manifest.records:
            component_id = connection.execute(
                "INSERT INTO components(ecosystem, canonical_name, purl) VALUES(?, ?, ?) "
                "ON CONFLICT(ecosystem, canonical_name) DO UPDATE SET purl=COALESCE(components.purl, excluded.purl) RETURNING id",
                (record.coordinate.ecosystem, record.coordinate.canonical_name, record.coordinate.purl),
            ).fetchone()[0]
            connection.execute(
                "INSERT OR IGNORE INTO component_names(component_id, display_name) VALUES(?, ?)",
                (component_id, record.display_name),
            )
            connection.execute(
                "INSERT INTO occurrences "
                "(component_id, manifest_id, artifact_path, record_index, dependency_type, version, version_spec, dependency_chain, raw_record) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (component_id, manifest_id, record.artifact_path, record.index, record.dependency_type,
                 record.version, record.version_spec,
                 json.dumps(record.dependency_chain, ensure_ascii=False),
                 json.dumps(record.raw, ensure_ascii=False, sort_keys=True)),
            )
            if record.dependency_chain:
                parent = record.dependency_chain[-1]
                parent_ecosystem = record.coordinate.ecosystem
                parent_name = (
                    canonicalize_pypi_name(parent)
                    if parent_ecosystem == "pypi"
                    else parent
                )
            else:
                parent_ecosystem = None
                parent_name = None
            connection.execute(
                "INSERT OR IGNORE INTO dependency_edges "
                "(manifest_id, component_id, parent_ecosystem, parent_name, chain) VALUES(?, ?, ?, ?, ?)",
                (manifest_id, component_id, parent_ecosystem, parent_name,
                 json.dumps(record.dependency_chain, ensure_ascii=False)),
            )
    refresh_project_findings(connection, project_id)
    return True, manifest_id


def refresh_project_findings(connection: sqlite3.Connection, project_id: int) -> None:
    rows = connection.execute(
        """
        SELECT o.component_id, o.version, o.version_spec
        FROM occurrences o
        JOIN manifests m ON m.id = o.manifest_id
        WHERE m.project_id = ?
        """,
        (project_id,),
    ).fetchall()
    grouped: dict[int, set[str]] = defaultdict(set)
    specs: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        component_id = int(row["component_id"])
        if row["version"]:
            grouped[component_id].add(str(row["version"]))
        if row["version_spec"]:
            specs[component_id].add(str(row["version_spec"]))

    with connection:
        connection.execute("DELETE FROM version_findings WHERE project_id = ?", (project_id,))
        for component_id, grouped_versions in grouped.items():
            status = "conflict" if len(grouped_versions) > 1 else "resolved"
            selected = None if status == "conflict" else next(iter(grouped_versions))
            connection.execute(
                "INSERT INTO version_findings(project_id, component_id, status, selected_version, versions) VALUES(?, ?, ?, ?, ?)",
                (project_id, component_id, status, selected, json.dumps(sorted(grouped_versions))),
            )
        for component_id, component_specs in specs.items():
            if component_id in grouped:
                continue
            connection.execute(
                "INSERT INTO version_findings(project_id, component_id, status, selected_version, versions) VALUES(?, ?, ?, ?, ?)",
                (project_id, component_id, "unresolved", None, json.dumps(sorted(component_specs))),
            )


def report(connection: sqlite3.Connection, project_name: str | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT p.name AS project_name, p.version AS project_version,
               c.ecosystem, c.canonical_name, c.purl,
               f.status, f.selected_version, f.versions,
               group_concat(DISTINCT n.display_name) AS aliases,
               replace(group_concat(DISTINCT o.artifact_path), ',', '; ') AS artifact_paths,
               group_concat(DISTINCT o.dependency_type) AS dependency_types
        FROM version_findings f
        JOIN projects p ON p.id = f.project_id
        JOIN components c ON c.id = f.component_id
        JOIN component_names n ON n.component_id = c.id
        JOIN occurrences o ON o.component_id = c.id
        JOIN manifests m ON m.id = o.manifest_id AND m.project_id = p.id
        {where}
        GROUP BY p.id, c.id, f.status, f.selected_version, f.versions
        ORDER BY p.name, c.ecosystem, c.canonical_name
    """
    params: tuple[object, ...] = ()
    where = ""
    if project_name is not None:
        where = "WHERE p.name = ?"
        params = (project_name,)
    return list(connection.execute(sql.format(where=where), params).fetchall())


def evidence_for(connection: sqlite3.Connection, ecosystem: str, canonical_name: str) -> list[sqlite3.Row]:
    return list(connection.execute(
        """
        SELECT m.source_path, o.record_index, o.artifact_path, o.dependency_type,
               o.version, o.version_spec, o.dependency_chain, o.raw_record
        FROM occurrences o
        JOIN components c ON c.id = o.component_id
        JOIN manifests m ON m.id = o.manifest_id
        WHERE c.ecosystem = ? AND c.canonical_name = ?
        ORDER BY m.source_path, o.record_index
        """,
        (ecosystem, canonical_name),
    ).fetchall())
