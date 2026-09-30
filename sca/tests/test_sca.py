import os
import tempfile
import unittest

from sca.engine import compute_groups, impact_preview
from sca.store import Store


def comps():
    return [
        {"name": "lodash", "ecosystem": "npm", "version": "4.17.21", "build_path": "app>lodash"},
        {"name": "lodash-es", "ecosystem": "npm", "version": "4.17.21", "build_path": "app>lib>lodash-es"},
        {"name": "lodash", "ecosystem": "npm", "version": "3.10.1", "build_path": "app>old>lodash"},
        {"name": "lodash", "ecosystem": "pypi", "version": "4.17.21", "build_path": "py>lodash"},
    ]


class ScopedAliasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "events.jsonl")
        self.st = Store(self.db)
        self.st.record_scan("s1", "proj-a", comps())
        self.st.record_scan("s2", "proj-b", [
            {"name": "lodash", "ecosystem": "npm", "version": "4.17.21", "build_path": "b>lodash"},
            {"name": "lodash-es", "ecosystem": "npm", "version": "4.17.20", "build_path": "b>lodash-es"},
        ])

    def groups(self):
        return compute_groups(self.st.components(), self.st.decisions())

    def group_sizes(self):
        return sorted(len(g["components"]) for g in self.groups())

    def test_scope_limits_merging(self):
        d = self.st.propose_alias("lodash", "lodash-es", "npm", ">=4.0.0", ["proj-a"])
        self.st.set_decision_status(d.decision_id, "approved")
        # proj-a 4.x pair merged; pypi, 3.x and proj-b untouched
        self.assertEqual(self.group_sizes(), [1, 1, 1, 1, 2])

    def test_version_range_excludes_incompatible(self):
        d = self.st.propose_alias("lodash", "lodash-es", "npm", ">=4.0.0,<5.0.0", ["*"])
        self.st.set_decision_status(d.decision_id, "approved")
        for g in self.groups():
            names = {c.name for c in g["components"]}
            if "lodash-es" in names:
                for c in g["components"]:
                    self.assertTrue(c.version.startswith("4."))

    def test_impact_preview(self):
        d = self.st.propose_alias("lodash", "lodash-es", "npm", "*", ["proj-a"])
        prev = impact_preview(self.st.components(), self.st.decisions(), d)
        self.assertIn("proj-b", prev["unaffected_sources"])
        self.assertTrue(any("proj-a" in m for m in prev["merged_components"]))
        self.assertFalse(any("proj-b" in m for m in prev["merged_components"]))

    def test_withdraw_middle_of_chain(self):
        # lodash <-> lodash-es, lodash-es <-> lodash-utils: indirect chain
        self.st.record_scan("s3", "proj-a", [
            {"name": "lodash-utils", "ecosystem": "npm", "version": "4.17.21", "build_path": "app>lu"},
        ])
        d1 = self.st.propose_alias("lodash", "lodash-es", "npm", "*", ["proj-a"])
        d2 = self.st.propose_alias("lodash-es", "lodash-utils", "npm", "*", ["proj-a"])
        self.st.set_decision_status(d1.decision_id, "approved")
        self.st.set_decision_status(d2.decision_id, "approved")
        big = max(self.groups(), key=lambda g: len(g["components"]))
        self.assertEqual(
            {c.name for c in big["components"]}, {"lodash", "lodash-es", "lodash-utils"}
        )
        # withdraw the middle link: chain breaks, build paths intact
        self.st.set_decision_status(d1.decision_id, "withdrawn")
        groups = self.groups()
        sizes = sorted(len(g["components"]) for g in groups)
        self.assertIn(2, sizes)  # lodash-es + lodash-utils still merged
        for g in groups:
            for c in g["components"]:
                self.assertTrue(c.build_path)  # original build path preserved
        merged_names = [
            {c.name for c in g["components"]} for g in groups if len(g["components"]) > 1
        ]
        self.assertNotIn({"lodash", "lodash-es"}, merged_names)

    def test_incremental_and_duplicate_import(self):
        before = len(self.st.components())
        added = self.st.record_scan("s1-rerun", "proj-a", comps())
        self.assertEqual(added, 0)
        self.assertEqual(len(self.st.components()), before)
        added = self.st.record_scan("s4", "proj-a", [
            {"name": "react", "ecosystem": "npm", "version": "18.2.0", "build_path": "app>react"},
        ])
        self.assertEqual(added, 1)

    def test_conflict_evidence_survives_alias(self):
        from sca.engine import detect_conflicts
        for cf in detect_conflicts(self.st.components()):
            self.st.record_conflict(cf["name"], cf["ecosystem"], cf["source"], cf["versions"])
        d = self.st.propose_alias("lodash", "lodash-es", "npm", "*", ["*"])
        self.st.set_decision_status(d.decision_id, "approved")
        conflicts = self.st.conflicts()
        self.assertTrue(any(c.name == "lodash" for c in conflicts))

    def test_persistence_and_traceability(self):
        d = self.st.propose_alias("lodash", "lodash-es", "npm", "*", ["proj-a"])
        self.st.set_decision_status(d.decision_id, "approved")
        reopened = Store(self.db)
        self.assertEqual(
            compute_groups(reopened.components(), reopened.decisions()),
            compute_groups(self.st.components(), self.st.decisions()),
        )
        kinds = [e["kind"] for e in reopened.events]
        self.assertIn("decision", kinds)
        self.assertIn("decision_status", kinds)
        self.assertIn("scan", kinds)
        # group provenance: approved decision ids attached to merged groups
        for g in compute_groups(reopened.components(), reopened.decisions()):
            if len(g["components"]) > 1:
                self.assertEqual(g["decisions"], [d.decision_id])


if __name__ == "__main__":
    unittest.main()
