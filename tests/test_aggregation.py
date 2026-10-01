from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sca.cli import import_files
from sca.identity import component_key, identify
from sca.models import DependencyEvidence, DependencyType, ParsedManifest
from sca.storage import Store


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class IdentityTests(unittest.TestCase):
    def test_python_name_normalization(self) -> None:
        self.assertEqual(component_key("PyPI", "Py.YAML"), ("pypi", "py-yaml"))

    def test_scoped_npm_name(self) -> None:
        item = DependencyEvidence("npm", "@Scope/Package", "1.0.0")
        self.assertEqual(identify(item), ("npm", "@scope/package", "pkg:npm/@scope/package"))

    def test_display_name_is_not_identity(self) -> None:
        pypi = identify(DependencyEvidence("pypi", "requests", "1", display_name="requests"))
        npm = identify(DependencyEvidence("npm", "requests", "1", display_name="requests"))
        self.assertNotEqual(pypi, npm)


class ManifestAggregationTests(unittest.TestCase):
    def test_sample_manifest_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sca.sqlite3"
            results = import_files(db, sorted(SAMPLES.glob("*.json")))
            self.assertTrue(all(result["inserted_records"] > 0 for result in results))

            with Store(db) as store:
                report = store.project_report("shopstack")

        by_purl = {
            component["purl"]: component for component in report["components"]
        }
        self.assertEqual(by_purl["pkg:pypi/requests"]["status"], "unified")
        self.assertEqual(by_purl["pkg:pypi/requests"]["evidence_count"], 2)
        self.assertEqual(by_purl["pkg:pypi/urllib3"]["status"], "conflict")
        self.assertEqual(by_purl["pkg:npm/debug"]["status"], "conflict")
        self.assertEqual(by_purl["pkg:maven/com.example/platform-core"]["evidence_count"], 2)
        self.assertIn("pkg:pypi/requests", by_purl)
        self.assertIn("pkg:npm/requests", by_purl)
        self.assertIn("pkg:pypi/pyyaml", by_purl)
        self.assertIn("pkg:pypi/py-yaml", by_purl)
        self.assertEqual(by_purl["pkg:pypi/pyyaml"]["evidence_count"], 2)
        self.assertEqual(by_purl["pkg:pypi/py-yaml"]["evidence_count"], 1)

        debug_paths = {
            item["location_path"]
            for item in report["evidence"]
            if item["purl"] == "pkg:npm/debug"
        }
        self.assertEqual(
            debug_paths,
            {
                "node_modules/debug",
                "node_modules/@example/legacy-adapter/node_modules/debug",
            },
        )
        for item in report["evidence"]:
            self.assertTrue(item["source_path"])
            self.assertTrue(item["raw_record"])

    def test_reimport_is_idempotent(self) -> None:
        files = sorted(SAMPLES.glob("*.json"))
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sca.sqlite3"
            first = import_files(db, files)
            second = import_files(db, files)
        self.assertEqual(sum(result["inserted_records"] for result in first), 15)
        self.assertEqual(sum(result["inserted_records"] for result in second), 0)

    def test_same_version_from_distinct_build_paths_keeps_evidence(self) -> None:
        evidence = (
            DependencyEvidence(
                ecosystem="pypi",
                name="certifi",
                version="2024.8.30",
                dependency_type=DependencyType.DIRECT,
                location_path="build-a/python/certifi",
                raw={"source": "a"},
            ),
            DependencyEvidence(
                ecosystem="pypi",
                name="certifi",
                version="2024.8.30",
                dependency_type=DependencyType.TRANSITIVE,
                location_path="build-b/python/certifi",
                raw={"source": "b"},
            ),
        )
        with Store() as store:
            project_id = store.conn.execute(
                "INSERT INTO projects(name, version, created_at) VALUES('p', '1', 'now') RETURNING id"
            ).fetchone()[0]
            source_id = store.conn.execute(
                "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
                "VALUES(?, 'x', 'test', 'h', 'now') RETURNING id",
                (project_id,),
            ).fetchone()[0]
            cursor = store.conn.cursor()
            inserted = store._insert_evidence(cursor, project_id, source_id, evidence)
            store.conn.commit()
            self.assertEqual(inserted, 2)


class AliasWorkflowTests(unittest.TestCase):
    def _store_with_project(self, name: str = "demo") -> Store:
        store = Store()
        store.conn.execute(
            "INSERT INTO projects(name, version, created_at) VALUES(?, '1', 'now')", (name,)
        )
        return store

    def _evidence(self, ecosystem: str, name: str, version: str, location: str, aliases: tuple = ()) -> DependencyEvidence:
        return DependencyEvidence(
            ecosystem=ecosystem,
            name=name,
            version=version,
            dependency_type=DependencyType.DIRECT,
            location_path=location,
            aliases=aliases,
            raw={"name": name, "version": version, "location": location},
        )

    def _add_source(self, store: Store, content_hash: str, evidence: tuple) -> int:
        project_id = store.conn.execute("SELECT id FROM projects").fetchone()[0]
        source_id = store.conn.execute(
            "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
            "VALUES(?, ?, 'test', ?, 'now') RETURNING id",
            (project_id, content_hash, content_hash),
        ).fetchone()[0]
        cursor = store.conn.cursor()
        store._insert_evidence(cursor, project_id, source_id, evidence)
        store.conn.commit()
        store.refresh_project_components(project_id)
        return project_id

    def test_cross_ecosystem_alias_is_rejected(self) -> None:
        store = self._store_with_project()
        self._add_source(
            store,
            "h1",
            (
                self._evidence("pypi", "requests", "2.0", "build/py-requests"),
                self._evidence("npm", "requests", "2.0", "build/npm-requests"),
            ),
        )
        preview = store.alias_preview("pypi:requests", "npm:requests")
        self.assertFalse(preview["allowed"])
        self.assertTrue(any("different ecosystems" in reason for reason in preview["reasons"]))
        with self.assertRaises(ValueError):
            store.approve_alias("pypi:requests", "npm:requests")
        report = store.project_report("demo")
        purls = {component["purl"] for component in report["components"]}
        self.assertEqual(purls, {"pkg:pypi/requests", "pkg:npm/requests"})

    def test_manual_alias_requires_explicit_project_and_source(self) -> None:
        store = self._store_with_project()
        self._add_source(
            store,
            "h1",
            (
                self._evidence("maven", "com.a:lib", "1.0", "build-a/lib"),
                self._evidence("maven", "com.b:lib", "1.0", "build-b/lib"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "explicit --project"):
            store.approve_alias("maven:com.a:lib", "maven:com.b:lib", sources=("h1",))
        with self.assertRaisesRegex(ValueError, "explicit --source"):
            store.approve_alias("maven:com.a:lib", "maven:com.b:lib", project="demo")

    def test_version_conflict_survives_alias_and_revoke_restores_evidence(self) -> None:
        store = self._store_with_project()
        project_id = self._add_source(
            store,
            "h1",
            (
                self._evidence("maven", "com.example:new-core", "1.0.0", "build-a/core"),
                self._evidence("maven", "com.example:old-core", "2.0.0", "build-b/core"),
            ),
        )
        preview = store.alias_preview("maven:com.example:new-core", "maven:com.example:old-core")
        self.assertTrue(preview["allowed"])
        self.assertTrue(preview["affected_projects"][0]["would_conflict"])
        result = store.approve_alias(
            "maven:com.example:new-core",
            "maven:com.example:old-core",
            project="demo",
            sources=("h1",),
        )
        self.assertEqual(result["refreshed_projects"], [project_id])
        report = store.project_report("demo")
        self.assertEqual(len(report["components"]), 1)
        merged = report["components"][0]
        self.assertEqual(merged["status"], "conflict")
        self.assertEqual(merged["evidence_count"], 2)
        self.assertEqual(
            {member["canonical_name"] for member in merged["member_components"]},
            {"com.example:new-core", "com.example:old-core"},
        )
        locations = {item["location_path"] for item in report["evidence"]}
        self.assertEqual(locations, {"build-a/core", "build-b/core"})

        history_before_revoke = store.project_history("demo")
        self.assertTrue(any(entry["reason"] == "alias-approve" for entry in history_before_revoke))

        store.revoke_alias(
            "maven:com.example:new-core",
            "maven:com.example:old-core",
            project="demo",
            sources=("h1",),
        )
        report_after = store.project_report("demo")
        self.assertEqual(len(report_after["components"]), 2)
        self.assertEqual(
            {component["canonical_name"] for component in report_after["components"]},
            {"com.example:new-core", "com.example:old-core"},
        )
        self.assertEqual({item["location_path"] for item in report_after["evidence"]}, locations)
        edge = store.conn.execute("SELECT status FROM alias_edges").fetchone()
        self.assertEqual(edge["status"], "revoked")
        history = store.project_history("demo")
        self.assertTrue(any(entry["reason"] == "alias-revoke" for entry in history))
        approved_snapshot = next(entry for entry in history if entry["reason"] == "alias-approve")
        self.assertEqual(len(approved_snapshot["report"]["components"]), 1)
        self.assertEqual(approved_snapshot["report"]["components"][0]["status"], "conflict")

    def test_similar_names_remain_distinct_and_traceable_across_builds(self) -> None:
        store = self._store_with_project()
        self._add_source(
            store,
            "h1",
            (
                self._evidence("pypi", "PyYAML", "6.0.2", "build-a/requirements#PyYAML"),
                self._evidence("pypi", "Py.YAML", "1.0.0", "build-a/vendor/Py.YAML"),
                self._evidence("pypi", "pyyaml", "6.0.2", "build-b/python/pyyaml"),
            ),
        )
        report = store.project_report("demo")
        by_purl = {component["purl"]: component for component in report["components"]}
        self.assertEqual(by_purl["pkg:pypi/pyyaml"]["evidence_count"], 2)
        self.assertEqual(by_purl["pkg:pypi/py-yaml"]["evidence_count"], 1)
        yaml_paths = {
            item["location_path"]
            for item in report["evidence"]
            if item["logical_purl"] == "pkg:pypi/pyyaml"
        }
        self.assertEqual(yaml_paths, {"build-a/requirements#PyYAML", "build-b/python/pyyaml"})

    def test_duplicate_import_across_paths_adds_no_evidence(self) -> None:
        item = self._evidence("pypi", "certifi", "2024.8.30", "build-a/python/certifi")
        manifest = ParsedManifest(
            project="demo",
            project_version="1",
            source_path="build-a/manifest.json",
            manifest_type="test",
            evidence=(item,),
        )
        with Store() as store:
            first = store.import_manifest(manifest, b"same-content")
            second = store.import_manifest(manifest, b"same-content")
            self.assertEqual(first["inserted_records"], 1)
            self.assertEqual(second["inserted_records"], 0)
            count = store.conn.execute("SELECT COUNT(*) FROM dependency_evidence").fetchone()[0]
            self.assertEqual(count, 1)
            sources = store.conn.execute("SELECT COUNT(*) FROM manifest_sources").fetchone()[0]
            self.assertEqual(sources, 1)

    def test_propose_then_approve_manual_alias(self) -> None:
        store = self._store_with_project()
        self._add_source(
            store,
            "h1",
            (
                self._evidence("maven", "com.a:lib", "1.0", "build-a/lib"),
                self._evidence("maven", "com.b:lib", "1.0", "build-b/lib"),
            ),
        )
        proposed = store.propose_alias("maven:com.a:lib", "maven:com.b:lib", project="demo", sources=("h1",))
        self.assertEqual(proposed["status"], "pending")
        self.assertEqual(len(store.project_report("demo")["components"]), 2)
        store.approve_alias("maven:com.a:lib", "maven:com.b:lib", project="demo", sources=("h1",))
        self.assertEqual(len(store.project_report("demo")["components"]), 1)

    def test_manual_alias_scope_does_not_leak_project_version_or_source(self) -> None:
        store = self._store_with_project("first")
        first_id = self._add_source(
            store,
            "h-first",
            (
                self._evidence("maven", "com.a:lib", "1.0", "first/in/a", aliases=()),
                self._evidence("maven", "com.b:lib", "1.0", "first/in/b"),
            ),
        )
        self._add_source(
            store,
            "h-other",
            (
                self._evidence("maven", "com.a:lib", "2.0", "first/out/a"),
                self._evidence("maven", "com.b:lib", "2.0", "first/out/b"),
            ),
        )
        second_id = store.conn.execute(
            "INSERT INTO projects(name, version, created_at) VALUES('second', '1', 'now') RETURNING id"
        ).fetchone()[0]
        source_id = store.conn.execute(
            "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
            "VALUES(?, 'second.json', 'test', 'h-second', 'now') RETURNING id",
            (second_id,),
        ).fetchone()[0]
        store._insert_evidence(
            store.conn.cursor(),
            second_id,
            source_id,
            (
                self._evidence("maven", "com.a:lib", "1.0", "second/a"),
                self._evidence("maven", "com.b:lib", "1.0", "second/b"),
            ),
        )
        store.conn.commit()
        store.refresh_project_components(second_id)

        preview = store.alias_preview(
            "maven:com.a:lib",
            "maven:com.b:lib",
            project="first",
            min_version="1.0",
            max_version="1.0",
            sources=("h-first",),
        )
        self.assertTrue(preview["allowed"])
        self.assertEqual(preview["merged_groups"][0]["versions"], ["1.0"])
        self.assertEqual(
            {(item["component"]["canonical_name"], item["version"]) for item in preview["unaffected_sources"]},
            {
                ("com.a:lib", "2.0"),
                ("com.b:lib", "2.0"),
            },
        )
        store.approve_alias(
            "maven:com.a:lib",
            "maven:com.b:lib",
            project="first",
            min_version="1.0",
            max_version="1.0",
            sources=("h-first",),
        )

        first_report = store.project_report("first")
        merged = next(component for component in first_report["components"] if len(component["member_components"]) == 2)
        self.assertEqual(json.loads(merged["versions_json"]), ["1.0"])
        self.assertEqual(merged["location_paths"], ["first/in/a", "first/in/b"])
        self.assertEqual(
            {(component["canonical_name"], component["location_paths"][0]) for component in first_report["components"] if len(component["member_components"]) == 1},
            {
                ("com.a:lib", "first/out/a"),
                ("com.b:lib", "first/out/b"),
            },
        )
        second_report = store.project_report("second")
        self.assertEqual(len(second_report["components"]), 2)
        self.assertEqual(first_id, 1)
        self.assertEqual(len(store.project_history("first")), 1)

    def test_revoking_middle_indirect_edge_recomputes_groups_and_paths(self) -> None:
        store = self._store_with_project()
        self._add_source(
            store,
            "h1",
            (
                self._evidence("maven", "com.a:lib", "1.0", "path/a"),
                self._evidence("maven", "com.b:lib", "1.0", "path/b"),
                self._evidence("maven", "com.c:lib", "1.0", "path/c"),
            ),
        )
        store.approve_alias("maven:com.a:lib", "maven:com.b:lib", project="demo", sources=("h1",))
        store.approve_alias("maven:com.b:lib", "maven:com.c:lib", project="demo", sources=("h1",))
        report = store.project_report("demo")
        self.assertEqual(len(report["components"]), 1)
        self.assertEqual(
            {member["canonical_name"] for member in report["components"][0]["member_components"]},
            {"com.a:lib", "com.b:lib", "com.c:lib"},
        )

        store.revoke_alias("maven:com.a:lib", "maven:com.b:lib", sources=("h1",))
        after = store.project_report("demo")
        self.assertEqual(len(after["components"]), 2)
        by_members = {
            tuple(member["canonical_name"] for member in component["member_components"]): component
            for component in after["components"]
        }
        self.assertEqual(set(by_members), {("com.a:lib",), ("com.b:lib", "com.c:lib")})
        self.assertEqual(
            {item["location_path"] for item in after["evidence"]},
            {"path/a", "path/b", "path/c"},
        )


if __name__ == "__main__":
    unittest.main()
