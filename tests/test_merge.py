from __future__ import annotations

from pathlib import Path

from sca.store import connect, evidence_for, import_manifest, report


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "demo-app"


def import_sample(connection) -> None:
    for manifest in ("pyproject.toml", "requirements.txt", "dependency-lock.json"):
        import_manifest(connection, SAMPLE / manifest)


def test_aliases_merge_without_losing_occurrences(tmp_path: Path) -> None:
    with connect(tmp_path / "results.db") as connection:
        import_sample(connection)

        flask = connection.execute(
            "SELECT id FROM components WHERE ecosystem = 'pypi' AND canonical_name = 'flask'"
        ).fetchone()
        assert flask is not None
        names = {
            row[0]
            for row in connection.execute(
                "SELECT display_name FROM component_names WHERE component_id = ?", (flask["id"],)
            )
        }
        assert names == {"Flask", "flask"}

        artifacts = {
            row[0]
            for row in connection.execute(
                "SELECT artifact_path FROM occurrences WHERE component_id = ?", (flask["id"],)
            )
        }
        assert artifacts == {"build:metadata", "dist/api-1.4.2-py3-none-any.whl"}


def test_conflicting_versions_remain_distinct_and_are_marked_conflict(tmp_path: Path) -> None:
    with connect(tmp_path / "results.db") as connection:
        import_sample(connection)

        finding = connection.execute(
            """
            SELECT f.* FROM version_findings f
            JOIN components c ON c.id = f.component_id
            WHERE c.ecosystem = 'pypi' AND c.canonical_name = 'urllib3'
            """
        ).fetchone()
        assert finding["status"] == "conflict"
        assert finding["selected_version"] is None
        assert finding["versions"] == '["1.26.18", "2.0.7"]'

        artifacts = {row["artifact_path"]: row["version"] for row in evidence_for(connection, "pypi", "urllib3")}
        assert artifacts == {
            "dist/api-1.4.2-py3-none-any.whl": "2.0.7",
            "dist/worker-1.4.2-py3-none-any.whl": "1.26.18",
        }


def test_same_display_name_across_ecosystems_is_not_merged(tmp_path: Path) -> None:
    with connect(tmp_path / "results.db") as connection:
        import_sample(connection)
        rows = connection.execute(
            "SELECT ecosystem, canonical_name FROM components WHERE canonical_name = 'left-pad' ORDER BY ecosystem"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("npm", "left-pad"), ("pypi", "left-pad")]


def test_indirect_dependency_preserves_chain(tmp_path: Path) -> None:
    with connect(tmp_path / "results.db") as connection:
        import_sample(connection)
        rows = connection.execute(
            """
            SELECT e.chain FROM dependency_edges e
            JOIN components c ON c.id = e.component_id
            WHERE c.ecosystem = 'pypi' AND c.canonical_name = 'urllib3'
            ORDER BY e.chain
            """
        ).fetchall()
        assert [row["chain"] for row in rows] == ['["botocore"]', '["requests"]']


def test_reimport_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "results.db"
    with connect(database) as connection:
        import_sample(connection)
        first_manifests = connection.execute("SELECT COUNT(*) FROM manifests").fetchone()[0]
        first_occurrences = connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
        first_findings = connection.execute("SELECT COUNT(*) FROM version_findings").fetchone()[0]

    with connect(database) as connection:
        inserted = [import_manifest(connection, SAMPLE / name)[0] for name in ("pyproject.toml", "requirements.txt", "dependency-lock.json")]
        assert inserted == [False, False, False]
        assert connection.execute("SELECT COUNT(*) FROM manifests").fetchone()[0] == first_manifests
        assert connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == first_occurrences
        assert connection.execute("SELECT COUNT(*) FROM version_findings").fetchone()[0] == first_findings


def test_report_distinguishes_resolved_and_conflicted_components(tmp_path: Path) -> None:
    with connect(tmp_path / "results.db") as connection:
        import_sample(connection)
        rows = {
            (row["ecosystem"], row["canonical_name"]): row
            for row in report(connection, "demo-app")
        }
        assert rows[("pypi", "flask")]["status"] == "resolved"
        assert rows[("pypi", "flask")]["selected_version"] == "2.3.3"
        assert rows[("pypi", "urllib3")]["status"] == "conflict"
