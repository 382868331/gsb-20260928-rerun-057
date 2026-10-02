from __future__ import annotations

import unittest

from sca.models import DependencyEvidence, DependencyType, ParsedManifest
from sca.storage import Store


def evidence(ecosystem: str, name: str, version: str, location: str) -> DependencyEvidence:
    return DependencyEvidence(
        ecosystem=ecosystem,
        name=name,
        version=version,
        dependency_type=DependencyType.DIRECT,
        location_path=location,
        raw={"name": name, "version": version, "location": location},
    )


def manifest(project: str, source_path: str, items: tuple) -> ParsedManifest:
    return ParsedManifest(
        project=project,
        project_version="1",
        source_path=source_path,
        manifest_type="test",
        evidence=items,
    )


class ScopedDecisionWorkflowTests(unittest.TestCase):
    def _store(self) -> Store:
        store = Store()
        store.import_manifest(
            manifest(
                "shop",
                "src/a.json",
                (
                    evidence("maven", "com.a:core", "1.0.0", "build-a/core"),
                    evidence("maven", "com.b:core", "1.0.0", "build-a/b-core"),
                    evidence("maven", "com.b:core", "2.0.0", "build-a/b-core-v2"),
                    evidence("maven", "com.c:core", "2.0.0", "build-a/c-core"),
                    evidence("maven", "com.d:core", "2.0.0", "build-a/d-core"),
                ),
            ),
            b"manifest-a",
        )
        store.import_manifest(
            manifest(
                "other",
                "src/b.json",
                (
                    evidence("maven", "com.a:core", "1.0.0", "build-b/core"),
                    evidence("maven", "com.b:core", "1.0.0", "build-b/b-core"),
                ),
            ),
            b"manifest-b",
        )
        return store

    def test_continuous_import_propose_adopt_history(self) -> None:
        store = self._store()
        proposed = store.propose_decision(
            "maven:com.a:core", "maven:com.b:core", version_range=">=1.0.0,<2.0.0", sources=["src/a.json"]
        )
        decision_id = proposed["decision"]["id"]
        self.assertEqual(proposed["decision"]["status"], "pending")
        self.assertEqual(len(store.project_report("shop")["components"]), 4)

        result = store.adopt_decisions([decision_id], decided_by="alice")
        self.assertEqual(result["adopted"][0]["status"], "approved")
        report = store.project_report("shop")
        merged = [c for c in report["components"] if len(c["member_components"]) > 1]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["status"], "conflict")  # 1.0.0 + 2.0.0 stay visible
        history = store.project_history("shop")
        self.assertTrue(any(entry["reason"] == "decision-adopt" for entry in history))
        # original evidence sources are preserved
        self.assertTrue(all(item["source_path"] for item in report["evidence"]))

    def test_source_scope_does_not_leak_to_other_projects(self) -> None:
        store = self._store()
        proposed = store.propose_decision(
            "maven:com.a:core", "maven:com.b:core", version_range="*", sources=["src/a.json"]
        )
        store.adopt_decisions([proposed["decision"]["id"]])
        shop = store.project_report("shop")
        other = store.project_report("other")
        self.assertTrue(any(len(c["member_components"]) > 1 for c in shop["components"]))
        self.assertTrue(all(len(c["member_components"]) == 1 for c in other["components"]))
        self.assertEqual(len(other["components"]), 2)

    def test_joint_preview_reports_conflict_chain(self) -> None:
        store = self._store()
        first = store.propose_decision(
            "maven:com.a:core", "maven:com.b:core", version_range=">=1.0.0,<2.0.0", sources=["src/a.json"]
        )["decision"]
        second = store.propose_decision(
            "maven:com.b:core", "maven:com.c:core", version_range=">=2.0.0", sources=["src/a.json"]
        )["decision"]
        preview = store.joint_preview()
        self.assertEqual([c["id"] for c in preview["candidates"]], [first["id"], second["id"]])
        self.assertEqual(len(preview["conflicts"]), 1)
        conflict = preview["conflicts"][0]
        self.assertEqual(conflict["project"]["name"], "shop")
        self.assertEqual(conflict["versions"], ["1.0.0", "2.0.0"])
        self.assertEqual(
            {link["id"] for link in conflict["via_decisions"]}, {first["id"], second["id"]}
        )
        paths = {(p["source_path"], p["location_path"]) for p in conflict["evidence_paths"]}
        self.assertIn(("src/a.json", "build-a/core"), paths)
        self.assertIn(("src/a.json", "build-a/c-core"), paths)
        # alone, the first proposal only chains through itself
        solo = store.joint_preview([first["id"]])
        self.assertTrue(solo["conflicts"])
        self.assertEqual(
            {link["id"] for c in solo["conflicts"] for link in c["via_decisions"]}, {first["id"]}
        )

    def test_partial_adoption_repreviews_remaining_without_widening_scope(self) -> None:
        store = self._store()
        first = store.propose_decision(
            "maven:com.a:core", "maven:com.b:core", version_range=">=1.0.0,<2.0.0", sources=["src/a.json"]
        )["decision"]
        second = store.propose_decision(
            "maven:com.b:core", "maven:com.c:core", version_range=">=2.0.0", sources=["src/a.json"]
        )["decision"]
        result = store.adopt_decisions([first["id"]])
        self.assertEqual([d["id"] for d in result["adopted"]], [first["id"]])
        self.assertEqual([item["decision"]["id"] for item in result["repreviewed"]], [second["id"]])
        remaining = store.get_decision(second["id"])
        self.assertEqual(remaining["status"], "pending")
        self.assertEqual(remaining["version_range"], ">=2.0.0")
        self.assertEqual(remaining["sources"], ["src/a.json"])
        # remaining proposal is re-previewed against the newly confirmed state
        repreview = result["repreviewed"][0]["preview"]
        self.assertEqual(repreview["candidates"][0]["id"], second["id"])
        self.assertTrue(all(m["project"]["name"] == "shop" for m in repreview["merges"]))
        self.assertTrue(all(c["project"]["name"] == "shop" for c in repreview["conflicts"]))

    def test_withdraw_rechecks_dependents_and_keeps_basis_and_notes(self) -> None:
        store = self._store()
        first = store.propose_decision(
            "maven:com.a:core", "maven:com.b:core", version_range="*", sources=["src/a.json"]
        )["decision"]
        second = store.propose_decision(
            "maven:com.b:core", "maven:com.c:core", version_range="*", sources=["src/a.json"]
        )["decision"]
        third = store.propose_decision(
            "maven:com.a:core", "maven:com.c:core", version_range="*", sources=["src/a.json"]
        )["decision"]
        fourth = store.propose_decision(
            "maven:com.c:core", "maven:com.d:core", version_range="*", sources=["src/a.json"]
        )["decision"]
        store.reject_decision(third["id"], reason="redundant with transitive merge", decided_by="bob")
        store.adopt_decisions([first["id"], second["id"]])
        basis_before = store.get_decision(first["id"])["basis"]
        self.assertIsNotNone(basis_before)

        result = store.withdraw_decision(first["id"], decided_by="alice")
        self.assertEqual(result["withdrawn"]["status"], "withdrawn")
        self.assertEqual([item["decision"]["id"] for item in result["rechecked"]], [fourth["id"]])
        # approval basis survives withdrawal
        self.assertEqual(store.get_decision(first["id"])["basis"], basis_before)
        # rejection note survives
        rejected = store.get_decision(third["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["note"], "redundant with transitive merge")
        # groups re-derived: a-b link gone, b-c still merged
        report = store.project_report("shop")
        merged = [c for c in report["components"] if len(c["member_components"]) > 1]
        self.assertEqual(len(merged), 1)
        names = {m["canonical_name"] for m in merged[0]["member_components"]}
        self.assertEqual(names, {"com.b:core", "com.c:core"})
        history = store.project_history("shop")
        self.assertTrue(any(entry["reason"] == "decision-withdraw" for entry in history))

    def test_cross_ecosystem_and_unknown_refs_rejected(self) -> None:
        store = self._store()
        store.import_manifest(
            manifest("shop", "src/c.json", (evidence("npm", "com.a:core", "1.0.0", "build-c/x"),)),
            b"manifest-c",
        )
        with self.assertRaises(ValueError):
            store.propose_decision("maven:com.a:core", "npm:com.a:core")
        with self.assertRaises(KeyError):
            store.propose_decision("maven:com.a:core", "maven:com.unknown:core")

    def test_existing_database_gains_decision_table(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sca.sqlite3"
            store = Store(db)
            store.import_manifest(
                manifest("shop", "src/a.json", (evidence("maven", "com.a:core", "1.0.0", "build-a/core"),)),
                b"manifest-a",
            )
            store.conn.execute("DROP TABLE alias_decisions")  # simulate pre-upgrade database
            store.close()
            reopened = Store(db)  # schema migration is additive
            reopened.conn.execute("SELECT COUNT(*) FROM alias_decisions").fetchone()
            proposed = reopened.propose_decision
            self.assertTrue(callable(proposed))
            self.assertEqual(len(reopened.project_report("shop")["components"]), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
