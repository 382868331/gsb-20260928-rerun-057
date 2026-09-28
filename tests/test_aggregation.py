from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sca.cli import import_files
from sca.identity import component_key, identify
from sca.models import DependencyEvidence, DependencyType
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
        self.assertEqual(sum(result["inserted_records"] for result in first), 14)
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


if __name__ == "__main__":
    unittest.main()
