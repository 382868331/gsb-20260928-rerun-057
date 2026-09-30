from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sca.cli import import_files
from sca.storage import Store


def write_manifest(directory: Path, name: str, packages: dict) -> Path:
    path = directory / name
    path.write_text(
        json.dumps({"project": "shopstack", "version": "2.0.0", "packages": packages}),
        encoding="utf-8",
    )
    return path


def package(name: str, version: str, location: str, **extra) -> dict:
    info = {
        "name": name,
        "version": version,
        "dependency_type": "direct",
        "location_path": location,
    }
    info.update(extra)
    return info


class ManualAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.db = root / "sca.sqlite3"
        first = write_manifest(
            root,
            "build-a.pip.lock.json",
            {
                "PyYAML": package("PyYAML", "6.0.2", "build-a/python/PyYAML"),
                "requests": package("requests", "2.32.3", "build-a/python/requests"),
            },
        )
        second = write_manifest(
            root,
            "build-b.pip.lock.json",
            {
                "pyyaml": package("pyyaml", "6.0.1", "build-b/python/pyyaml"),
                "requests": package("Requests", "2.32.3", "build-b/python/requests"),
            },
        )
        import_files(self.db, [first, second])
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.addCleanup(self.directory.cleanup)

    def component_statuses(self) -> dict[str, str]:
        report = self.store.project_report("shopstack")
        return {row["purl"]: row["status"] for row in report["components"]}

    def test_similar_names_normalize_but_display_names_differ(self) -> None:
        self.assertEqual(self.component_statuses()["pkg:pypi/pyyaml"], "conflict")
        report = self.store.project_report("shopstack")
        display_names = {
            item["display_name"]
            for item in report["evidence"]
            if item["canonical_name"] == "pyyaml"
        }
        self.assertEqual(display_names, {"PyYAML", "pyyaml"})

    def test_dotted_and_plain_names_stay_separate(self) -> None:
        report = self.store.project_report("shopstack")
        self.assertNotIn("pkg:pypi/py-yaml", {row["purl"] for row in report["components"]})
        self.assertEqual(
            __import__("sca.identity", fromlist=["component_key"]).component_key("pypi", "Py.YAML"),
            ("pypi", "py-yaml"),
        )

    def test_cross_ecosystem_alias_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.propose_alias("pypi", "requests", "requests", target_ecosystem="npm")

    def test_alias_requires_existing_components(self) -> None:
        with self.assertRaises(KeyError):
            self.store.propose_alias("pypi", "requests", "does-not-exist")

    def test_duplicate_alias_rejected(self) -> None:
        self.store.propose_alias("pypi", "pyyaml", "requests")
        with self.assertRaises(ValueError):
            self.store.propose_alias("pypi", "requests", "pyyaml")

    def test_preview_approve_revoke_lifecycle(self) -> None:
        before = self.store.project_report("shopstack")
        before_paths = sorted(
            (item["canonical_name"], item["location_path"]) for item in before["evidence"]
        )
        alias_id = self.store.propose_alias("pypi", "pyyaml", "requests")

        preview = self.store.preview_alias(alias_id)
        self.assertFalse(preview["already_merged"])
        self.assertEqual(preview["alias"]["status"], "pending")
        impact = preview["impact"][0]
        self.assertEqual(impact["project"], "shopstack")
        self.assertEqual(impact["resulting_status"], "conflict")
        self.assertEqual(impact["versions"], ["2.32.3", "6.0.1", "6.0.2"])
        self.assertEqual(impact["evidence_count"], 4)

        pending_report = self.store.project_report("shopstack")
        self.assertIn("pkg:pypi/pyyaml", {row["purl"] for row in pending_report["components"]})

        self.store.approve_alias(alias_id)
        merged = self.store.project_report("shopstack")
        merged_components = {row["purl"]: row for row in merged["components"]}
        self.assertNotIn("pkg:pypi/pyyaml", merged_components)
        self.assertEqual(merged_components["pkg:pypi/requests"]["status"], "conflict")
        self.assertEqual(merged_components["pkg:pypi/requests"]["evidence_count"], 4)
        merged_originals = {
            item["original_canonical_name"]
            for item in merged["evidence"]
            if item["canonical_name"] == "requests"
        }
        self.assertEqual(merged_originals, {"pyyaml", "requests"})

        with self.assertRaises(ValueError):
            self.store.approve_alias(alias_id)

        restored_snapshot = self.store.revoke_alias(alias_id)
        self.assertEqual(restored_snapshot["alias"]["status"], "approved")
        restored = self.store.project_report("shopstack")
        self.assertEqual(self.component_statuses()["pkg:pypi/pyyaml"], "conflict")
        restored_paths = sorted(
            (item["canonical_name"], item["location_path"]) for item in restored["evidence"]
        )
        self.assertEqual(restored_paths, before_paths)

        history = self.store.alias_history(alias_id)
        self.assertEqual([event["action"] for event in history], ["proposed", "approved", "revoked"])
        approved_detail = json.loads(history[1]["detail"])
        self.assertEqual(approved_detail["impact"][0]["resulting_status"], "conflict")

        with self.assertRaises(ValueError):
            self.store.revoke_alias(alias_id)

    def test_reimport_after_approval_adds_no_duplicate_evidence(self) -> None:
        alias_id = self.store.propose_alias("pypi", "pyyaml", "requests")
        self.store.approve_alias(alias_id)
        self.store.close()
        root = Path(self.directory.name)
        results = import_files(self.db, sorted(root.glob("*.pip.lock.json")))
        self.assertEqual(sum(result["inserted_records"] for result in results), 0)
        self.store = Store(self.db)
        report = self.store.project_report("shopstack")
        components = {row["purl"]: row for row in report["components"]}
        self.assertEqual(components["pkg:pypi/requests"]["evidence_count"], 4)


if __name__ == "__main__":
    unittest.main()
