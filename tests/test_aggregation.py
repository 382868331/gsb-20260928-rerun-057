from __future__ import annotations

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
        result = store.approve_alias("maven:com.example:new-core", "maven:com.example:old-core")
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

        store.revoke_alias("maven:com.example:new-core", "maven:com.example:old-core")
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
        proposed = store.propose_alias("maven:com.a:lib", "maven:com.b:lib")
        self.assertEqual(proposed["status"], "pending")
        self.assertEqual(len(store.project_report("demo")["components"]), 2)
        store.approve_alias("maven:com.a:lib", "maven:com.b:lib")
        self.assertEqual(len(store.project_report("demo")["components"]), 1)

class InheritanceWorkflowTests(unittest.TestCase):
    def _manifest(self, version: str, path: str, names: tuple[tuple[str, str], ...], ecosystem: str = "pypi") -> ParsedManifest:
        return ParsedManifest(
            project="shopstack",
            project_version=version,
            source_path=path,
            manifest_type="test",
            evidence=tuple(
                DependencyEvidence(
                    ecosystem=ecosystem,
                    name=name,
                    version=component_version,
                    dependency_type=DependencyType.DIRECT,
                    location_path=f"{path}#{name}",
                    raw={"name": name, "version": component_version, "source": path},
                )
                for name, component_version in names
            ),
        )

    def test_version_change_requires_per_item_inheritance_and_reopen_keeps_old_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sca.sqlite3"
            store = Store(db)
            first = store.import_manifest(
                self._manifest(
                    "1.0.0",
                    "v1-lock.json",
                    (("alpha", "1.0"), ("beta", "1.0"), ("gamma", "1.2"), ("delta", "1.2"), ("epsilon", "1.4"), ("zeta", "1.4")),
                ),
                b"v1",
            )
            self.assertIsNone(first["inheritance_plan_id"])
            store.approve_alias("pypi:alpha", "pypi:beta", note="same vendor rename")
            store.approve_alias("pypi:gamma", "pypi:delta")
            store.approve_alias("pypi:epsilon", "pypi:zeta")

            v2_manifest = self._manifest(
                "2.0.0",
                "v2-lock.json",
                (("alpha", "2.0"), ("beta", "2.0"), ("gamma", "2.2"), ("delta", "2.2"), ("epsilon", "2.4"), ("zeta", "2.4")),
            )
            second = store.import_manifest(v2_manifest, b"v2")
            duplicate = store.import_manifest(v2_manifest, b"v2")
            self.assertEqual(second["inserted_records"], 6)
            self.assertEqual(duplicate["inserted_records"], 0)
            self.assertEqual(duplicate["inheritance_plan_id"], second["inheritance_plan_id"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM components WHERE ecosystem='pypi'").fetchone()[0], 6)

            review = store.inheritance_review("shopstack", "2.0.0")
            store.close()
            store = Store(db)
            review = store.inheritance_review("shopstack", "2.0.0")
            self.assertIsNotNone(review)
            self.assertEqual([item["status"] for item in review["items"]], ["pending"] * 3)
            self.assertEqual({item["classification"] for item in review["items"]}, {"source_changed"})
            self.assertEqual(len({item["source_edge"]["id"] for item in review["items"]}), 3)
            for item in review["items"]:
                self.assertEqual(len(item["affected_evidence"]), 2)
                self.assertTrue(all(evidence["source_path"] == "v2-lock.json" for evidence in item["affected_evidence"]))

            self.assertEqual(len(store.project_report("shopstack", "1.0.0")["components"]), 3)
            self.assertEqual(len(store.project_report("shopstack", "2.0.0")["components"]), 6)

            plan_id = second["inheritance_plan_id"]
            inherited, narrowed, skipped = [item["id"] for item in review["items"]]
            store.decide_inheritance(plan_id, [inherited], "inherit", note="vendor identity remains valid")
            store.decide_inheritance(plan_id, [narrowed], "narrowed", version_range=">=2.0")
            store.decide_inheritance(plan_id, [skipped], "skipped", note="source changed independently")

            new_report = store.project_report("shopstack", "2.0.0")
            self.assertEqual(len(new_report["components"]), 4)
            merged_purls = {component["purl"] for component in new_report["components"]}
            self.assertIn("pkg:pypi/alpha", merged_purls)
            self.assertIn("pkg:pypi/gamma", merged_purls)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM project_components WHERE project_id=2").fetchone()[0], 4)

            reopened = store.reopen_inheritance_items(plan_id, [inherited])
            self.assertEqual(reopened["review"]["items"][0]["status"], "pending")
            self.assertEqual(len(store.project_report("shopstack", "2.0.0")["components"]), 5)
            self.assertEqual(len(store.project_report("shopstack", "1.0.0")["components"]), 3)
            store.decide_inheritance(plan_id, [inherited], "skipped")
            self.assertEqual(len(store.project_report("shopstack", "2.0.0")["components"]), 5)

            old_history = store.project_history("shopstack", "1.0.0")
            self.assertTrue(any(entry["reason"] == "alias-approve" and len(entry["report"]["components"]) == 3 for entry in old_history))
            store.close()

    def test_same_names_in_another_ecosystem_do_not_inherit_old_approval(self) -> None:
        with Store() as store:
            store.import_manifest(
                self._manifest("1.0.0", "py-lock.json", (("alpha", "1.0"), ("beta", "1.0")), "pypi"),
                b"py",
            )
            store.approve_alias("pypi:alpha", "pypi:beta")
            result = store.import_manifest(
                self._manifest("2.0.0", "js-lock.json", (("alpha", "2.0"), ("beta", "2.0")), "npm"),
                b"js",
            )
            review = store.inheritance_review("shopstack", "2.0.0")
            self.assertIsNotNone(review)
            item = review["items"][0]
            self.assertEqual(item["classification"], "ecosystem_or_identity_changed")
            self.assertEqual({evidence["ecosystem"] for evidence in item["affected_evidence"]}, {"npm"})
            self.assertEqual(len(store.project_report("shopstack", "2.0.0")["components"]), 2)
            with self.assertRaises(ValueError):
                store.decide_inheritance(result["inheritance_plan_id"], [item["id"]], "inherit")
            store.decide_inheritance(result["inheritance_plan_id"], [item["id"]], "skipped")
            self.assertEqual(
                {component["purl"] for component in store.project_report("shopstack", "2.0.0")["components"]},
                {"pkg:npm/alpha", "pkg:npm/beta"},
            )


class ScopedAliasWorkflowTests(unittest.TestCase):
    def _store(self) -> Store:
        return Store()

    def _project(self, store: Store, name: str) -> int:
        return store.conn.execute(
            "INSERT INTO projects(name, version, created_at) VALUES(?, '1', 'now') RETURNING id",
            (name,),
        ).fetchone()[0]

    def _evidence(self, name: str, version: str, location: str) -> DependencyEvidence:
        return DependencyEvidence(
            ecosystem="pypi",
            name=name,
            version=version,
            dependency_type=DependencyType.DIRECT,
            location_path=location,
            raw={"name": name, "version": version},
        )

    def _add_source(self, store: Store, project_id: int, content_hash: str, evidence: tuple) -> None:
        source_id = store.conn.execute(
            "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
            "VALUES(?, ?, 'test', ?, 'now') RETURNING id",
            (project_id, f"src-{content_hash}.json", content_hash),
        ).fetchone()[0]
        cursor = store.conn.cursor()
        store._insert_evidence(cursor, project_id, source_id, evidence)
        store.conn.commit()
        store.refresh_project_components(project_id)

    def test_version_range_limits_merge(self) -> None:
        store = self._store()
        project_id = self._project(store, "demo")
        self._add_source(store, project_id, "h1", (
            self._evidence("alpha", "1.5", "build/a"),
            self._evidence("beta", "2.5", "build/b"),
        ))
        store.propose_alias("pypi:alpha", "pypi:beta", version_range="<2.0")
        store.approve_alias("pypi:alpha", "pypi:beta")
        self.assertEqual(len(store.project_report("demo")["components"]), 2)
        # re-proposing an approved edge with a wider range must not widen it
        store.propose_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
        store.approve_alias("pypi:alpha", "pypi:beta")
        self.assertEqual(len(store.project_report("demo")["components"]), 2)
        # widening requires explicit revoke and a fresh proposal
        store.revoke_alias("pypi:alpha", "pypi:beta")
        store.propose_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
        store.approve_alias("pypi:alpha", "pypi:beta")
        report = store.project_report("demo")
        self.assertEqual(len(report["components"]), 1)
        self.assertEqual(report["components"][0]["status"], "conflict")

    def test_source_scope_does_not_leak_to_other_projects(self) -> None:
        store = self._store()
        demo_id = self._project(store, "demo")
        other_id = self._project(store, "other")
        for project_id, prefix in ((demo_id, "d"), (other_id, "o")):
            self._add_source(store, project_id, f"h-{prefix}", (
                self._evidence("foo", "1.0", f"{prefix}/foo"),
                self._evidence("bar", "1.0", f"{prefix}/bar"),
            ))
        store.approve_alias("pypi:foo", "pypi:bar", sources=["demo"])
        self.assertEqual(len(store.project_report("demo")["components"]), 1)
        self.assertEqual(len(store.project_report("other")["components"]), 2)

    def _chain_store(self) -> tuple[Store, int, int]:
        store = self._store()
        project_id = self._project(store, "demo")
        self._add_source(store, project_id, "h1", (
            self._evidence("alpha", "1.0", "build/alpha"),
            self._evidence("beta", "1.0", "build/beta"),
            self._evidence("gamma", "2.0", "build/gamma"),
        ))
        first = store.propose_alias("pypi:alpha", "pypi:beta", note="same project")
        second = store.propose_alias("pypi:beta", "pypi:gamma", version_range=">=1.0")
        return store, first["edge_id"], second["edge_id"]

    def test_joint_preview_reports_chain_conflict_with_paths(self) -> None:
        store, first_id, second_id = self._chain_store()
        preview = store.joint_preview()
        self.assertEqual([c["id"] for c in preview["candidates"]], [first_id, second_id])
        self.assertEqual(len(preview["conflicts"]), 1)
        conflict = preview["conflicts"][0]
        self.assertEqual(conflict["versions"], ["1.0", "2.0"])
        self.assertEqual(
            {p["id"] for p in conflict["proposals"]}, {first_id, second_id},
        )
        locations = {item["location_path"] for item in conflict["evidence"]}
        self.assertEqual(locations, {"build/alpha", "build/beta", "build/gamma"})
        self.assertTrue(all(item["source_path"] for item in conflict["evidence"]))

    def test_partial_approve_repreviews_remaining_without_widening_scope(self) -> None:
        store, first_id, second_id = self._chain_store()
        result = store.approve_edges([first_id], decided_by="reviewer", note="first only")
        self.assertEqual([e["id"] for e in result["approved"]], [first_id])
        remaining = result["remaining_pending"]
        self.assertEqual([e["id"] for e in remaining], [second_id])
        self.assertEqual(remaining[0]["version_range"], ">=1.0")
        self.assertEqual(remaining[0]["sources"], ["*"])
        repreview = result["remaining_preview"]
        self.assertEqual([c["id"] for c in repreview["candidates"]], [second_id])
        report = store.project_report("demo")
        self.assertEqual(len(report["components"]), 2)
        edge = store.edge_dict(store._edge_row(first_id))
        self.assertEqual(edge["note"], "first only")

    def test_revoke_rechecks_dependents_and_keeps_rationale(self) -> None:
        store, first_id, second_id = self._chain_store()
        store.approve_edges([first_id], note="approved basis")
        store.reject_edge(second_id, "not adopted: versions diverge", decided_by="reviewer")
        third = store.propose_alias("pypi:alpha", "pypi:gamma")
        pending_before = store.pending_edges()
        self.assertEqual(len(pending_before), 1)
        result = store.revoke_edge(first_id, decided_by="reviewer")
        self.assertEqual(
            [e["id"] for e in result["affected_pending"]], [third["edge_id"]],
        )
        revoked = store.edge_dict(store._edge_row(first_id))
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["note"], "approved basis")
        rejected = store.edge_dict(store._edge_row(second_id))
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["reject_reason"], "not adopted: versions diverge")
        history = store.project_history("demo")
        self.assertTrue(any(entry["reason"] == "alias-revoke" for entry in history))
        report = store.project_report("demo")
        self.assertEqual(len(report["components"]), 3)


class RevocationPreviewWorkflowTests(unittest.TestCase):
    def _store(self) -> Store:
        return Store()

    def _project(self, store: Store, name: str, version: str = "1.0") -> int:
        return store.conn.execute(
            "INSERT INTO projects(name, version, created_at) VALUES(?, ?, 'now') RETURNING id",
            (name, version),
        ).fetchone()[0]

    def _evidence(self, name: str, version: str, location: str) -> DependencyEvidence:
        return DependencyEvidence(
            ecosystem="pypi",
            name=name,
            version=version,
            dependency_type=DependencyType.DIRECT,
            location_path=location,
            raw={"name": name, "version": version},
        )

    def _add_source(self, store: Store, project_id: int, content_hash: str, evidence: tuple) -> None:
        source_id = store.conn.execute(
            "INSERT INTO manifest_sources(project_id, source_path, manifest_type, content_hash, imported_at) "
            "VALUES(?, ?, 'test', ?, 'now') RETURNING id",
            (project_id, f"src-{content_hash}.json", content_hash),
        ).fetchone()[0]
        cursor = store.conn.cursor()
        store._insert_evidence(cursor, project_id, source_id, evidence)
        store.conn.commit()
        store.refresh_project_components(project_id)

    def _two_projects_store(self) -> Store:
        store = self._store()
        portal = self._project(store, "portal", "3.1.0")
        self._add_source(store, portal, "portal-lock", (
            self._evidence("alpha", "1.2.0", "portal/alpha"),
            self._evidence("beta", "1.9.0", "portal/beta"),
        ))
        shop = self._project(store, "shop", "2.4.0")
        self._add_source(store, shop, "shop-lock", (
            self._evidence("alpha", "1.5.0", "shop/alpha"),
            self._evidence("beta", "2.1.0", "shop/beta"),
            self._evidence("gamma", "2.1.0", "shop/gamma"),
        ))
        return store

    def test_preview_lists_splits_projects_ranges_and_dependents_without_changing_merges(self) -> None:
        store = self._two_projects_store()
        store.approve_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0", decided_by="owner")
        edge = store.list_edges("approved")[0]
        dependent = store.propose_alias("pypi:beta", "pypi:gamma")

        components_before = {
            name: len(store.project_report(name)["components"]) for name in ("portal", "shop")
        }
        self.assertEqual(components_before, {"portal": 1, "shop": 2})
        history_before = {
            name: [entry["reason"] for entry in store.project_history(name)]
            for name in ("portal", "shop")
        }

        preview = store.create_revocation_preview(edge["id"], created_by="auditor")
        self.assertEqual(preview["status"], "open")
        self.assertEqual(preview["created_by"], "auditor")
        self.assertIsNone(preview["confirmed_at"])
        self.assertEqual(
            {component["purl"] for component in preview["split_components"]},
            {"pkg:pypi/alpha", "pkg:pypi/beta"},
        )
        projects = {(item["project"], item["project_version"]): item for item in preview["affected_projects"]}
        self.assertEqual(set(projects), {("portal", "3.1.0"), ("shop", "2.4.0")})
        for impact in projects.values():
            self.assertEqual(impact["edge_version_range"], ">=1.0,<3.0")
            self.assertEqual(len(impact["groups"]), 2)
            roots = {group["purl"] for group in impact["groups"]}
            self.assertEqual(roots, {"pkg:pypi/alpha", "pkg:pypi/beta"})
            self.assertTrue(all(evidence["source_path"] for group in impact["groups"] for evidence in group["evidence"]))
        self.assertEqual(preview["edge"]["id"], edge["id"])
        self.assertEqual([candidate["id"] for candidate in preview["dependent_candidates"]], [dependent["edge_id"]])

        # creating a preview must not change current merge results or add snapshots
        self.assertEqual(
            {name: len(store.project_report(name)["components"]) for name in ("portal", "shop")},
            components_before,
        )
        for name in ("portal", "shop"):
            self.assertEqual(
                [entry["reason"] for entry in store.project_history(name)],
                history_before[name],
            )

    def test_cancel_then_recreate_preview_and_snapshot_survives_closing_and_reopening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "sca.sqlite3"
            store = Store(db)
            portal = self._project(store, "portal", "3.1.0")
            self._add_source(store, portal, "portal-lock", (
                self._evidence("alpha", "1.2.0", "portal/alpha"),
                self._evidence("beta", "1.9.0", "portal/beta"),
            ))
            shop = self._project(store, "shop", "2.4.0")
            self._add_source(store, shop, "shop-lock", (
                self._evidence("alpha", "1.5.0", "shop/alpha"),
                self._evidence("beta", "2.1.0", "shop/beta"),
            ))
            store.approve_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
            edge_id = store.list_edges("approved")[0]["id"]

            first = store.create_revocation_preview(edge_id, created_by="auditor")
            self.assertEqual(first["preview_id"], 1)
            cancelled = store.cancel_revocation_preview(first["preview_id"], cancelled_by="auditor")
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["cancelled_by"], "auditor")
            self.assertIsNotNone(cancelled["cancelled_at"])
            # cancelling a preview keeps the alias in force and merges unchanged
            self.assertEqual(len(store.project_report("portal")["components"]), 1)
            with self.assertRaises(ValueError):
                store.confirm_revocation_preview(first["preview_id"])

            second = store.create_revocation_preview(edge_id, created_by="auditor")
            self.assertEqual(second["preview_id"], 2)

            # close and reopen the database: open preview must still be readable
            store.close()
            store = Store(db)
            reopened_first = store.revocation_preview(1)
            reopened_second = store.revocation_preview(2)
            self.assertEqual(reopened_first["status"], "cancelled")
            self.assertEqual(reopened_second["status"], "open")
            self.assertEqual(
                {item["project"] for item in reopened_second["affected_projects"]},
                {"portal", "shop"},
            )

            result = store.confirm_revocation_preview(2, confirmed_by="operator")
            self.assertEqual(result["preview"]["status"], "confirmed")
            self.assertEqual(result["preview"]["confirmed_by"], "operator")
            self.assertIsNotNone(result["preview"]["confirmed_at"])
            self.assertEqual(len(store.project_report("portal")["components"]), 2)
            self.assertEqual(len(store.project_report("shop")["components"]), 2)
            self.assertTrue(any(entry["reason"] == "alias-revoke" for entry in store.project_history("portal")))
            self.assertTrue(any(entry["reason"] == "alias-revoke" for entry in store.project_history("shop")))

            # close/reopen after confirmation: snapshot is retained and immutable
            store.close()
            store = Store(db)
            confirmed = store.revocation_preview(2)
            self.assertEqual(confirmed["status"], "confirmed")
            self.assertEqual(
                {component["purl"] for component in confirmed["split_components"]},
                {"pkg:pypi/alpha", "pkg:pypi/beta"},
            )
            self.assertEqual(
                {item["project"] for item in confirmed["affected_projects"]},
                {"portal", "shop"},
            )
            with self.assertRaises(ValueError):
                store.cancel_revocation_preview(2)
            store.close()

    def test_later_rule_changes_never_rewrite_old_preview_records(self) -> None:
        store = self._two_projects_store()
        store.approve_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
        edge_id = store.list_edges("approved")[0]["id"]
        preview = store.create_revocation_preview(edge_id, created_by="auditor")
        result = store.confirm_revocation_preview(preview["preview_id"], confirmed_by="operator")
        frozen = store.revocation_preview(preview["preview_id"])
        self.assertEqual(
            [item["project"] for item in frozen["affected_projects"]],
            [item["project"] for item in preview["affected_projects"]],
        )

        # re-approve with a different, non-overlapping range; the old record stays as captured
        store.propose_alias("pypi:alpha", "pypi:beta", version_range=">=9.0")
        store.approve_alias("pypi:alpha", "pypi:beta")
        latest = store.list_edges("approved")[0]
        self.assertEqual(latest["version_range"], ">=9.0")
        # range >=9.0 merges nothing here, both projects remain split
        self.assertEqual(len(store.project_report("portal")["components"]), 2)
        self.assertEqual(len(store.project_report("shop")["components"]), 3)

        old_record = store.revocation_preview(preview["preview_id"])
        self.assertEqual(old_record["status"], "confirmed")
        self.assertEqual(old_record["confirmed_by"], "operator")
        self.assertEqual(
            [item["edge_version_range"] for item in old_record["affected_projects"]],
            [">=1.0,<3.0", ">=1.0,<3.0"],
        )
        self.assertEqual(
            {component["purl"] for component in old_record["split_components"]},
            {"pkg:pypi/alpha", "pkg:pypi/beta"},
        )
        self.assertEqual(
            {item["project"] for item in old_record["affected_projects"]},
            {"portal", "shop"},
        )
        listed = store.list_revocation_previews(status="confirmed")
        self.assertEqual([item["preview_id"] for item in listed], [preview["preview_id"]])
        self.assertEqual(result["revoked_edge"], edge_id)

    def test_exported_preview_contains_operator_and_confirmation_time(self) -> None:
        store = self._two_projects_store()
        store.approve_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
        edge_id = store.list_edges("approved")[0]["id"]
        preview = store.create_revocation_preview(edge_id, created_by="auditor")
        with self.assertRaises(ValueError):
            store.create_revocation_preview(edge_id)
        export_before = store.export_revocation_preview(preview["preview_id"], exported_by="exporter")
        self.assertEqual(export_before["document"], "alias-revocation-impact-preview")
        self.assertEqual(export_before["preview"]["confirmed_at"], None)
        self.assertTrue(export_before["exported_at"])

        store.confirm_revocation_preview(preview["preview_id"], confirmed_by="final-owner")
        export_after = store.export_revocation_preview(preview["preview_id"], exported_by="exporter")
        frozen = export_after["preview"]
        self.assertEqual(frozen["created_by"], "auditor")
        self.assertEqual(frozen["confirmed_by"], "final-owner")
        self.assertTrue(frozen["confirmed_at"])
        self.assertEqual(
            {item["project"] for item in frozen["affected_projects"]},
            {"portal", "shop"},
        )

    def test_confirm_without_operator_defaults_to_preview_creator(self) -> None:
        store = self._two_projects_store()
        store.approve_alias("pypi:alpha", "pypi:beta", version_range=">=1.0,<3.0")
        edge_id = store.list_edges("approved")[0]["id"]
        preview = store.create_revocation_preview(edge_id, created_by="creator-only")
        result = store.confirm_revocation_preview(preview["preview_id"])
        self.assertEqual(result["preview"]["confirmed_by"], "creator-only")
        self.assertEqual(
            store.edge_dict(store._edge_row(edge_id))["decided_by"], "creator-only"
        )


if __name__ == "__main__":
    unittest.main()
