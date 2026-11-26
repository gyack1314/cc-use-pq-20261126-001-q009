from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
from unittest.mock import patch

from strata.runtime.errors import VaultError
from support import Scenario, sha


class Preservation(Scenario):
    def test_hold_survives_new_head_and_separates_shared_object_retention(self):
        old = self.edition("north", {"plate": [b"shared", b"original"]})
        self.vault.custody.hold("ridge", 1, "report-56", "Published appendix")
        self.edition("adjusted", {"plate": [b"shared", b"replacement"]}, parent=1)
        self.clock.advance(60)
        with self.assertRaises(VaultError) as rejected:
            self.vault.custody.retire("ridge", 1)
        self.assertEqual(rejected.exception.details["blockers"], ["active_holds"])
        before = self.vault.inspection.coverage("ridge", 1)
        self.assertTrue(before["files"][0]["restorable"])
        self.assertEqual(self.vault.custody.mark(100), [])
        self.vault.custody.release("ridge", 1, "report-56")
        self.vault.custody.retire("ridge", 1)
        self.assertEqual(self.vault.custody.mark(100), [sha(b"original")])
        self.restart()
        swept = self.vault.custody.sweep(100)
        self.assertEqual(swept["bytes"], len(b"original"))
        self.assertEqual([row["digest"] for row in swept["removed"]], [sha(b"original")])
        self.assertTrue(self.vault.media.path(sha(b"shared")).is_file())
        after = self.vault.catalog.revision("ridge", 1)
        self.assertEqual((after["state"], after["manifest_sha256"]), ("retired", old["manifest_sha256"]))
        self.assertEqual(self.vault.inspection.coverage("ridge", 1)["files"][0]["unavailable_parts"], [sha(b"original")])
        with self.assertRaises(VaultError) as gone:
            self.vault.delivery.file("ridge", 1, "plate")
        self.assertEqual(gone.exception.status, 410)

    def test_head_and_retain_until_have_independent_blockers(self):
        self.edition("one", {"f": [b"a"]})
        with self.assertRaises(VaultError) as caught:
            self.vault.custody.retire("ridge", 1)
        self.assertEqual(caught.exception.details["blockers"], ["current_head", "retention"])
        self.clock.advance(60)
        with self.assertRaises(VaultError) as caught:
            self.vault.custody.retire("ridge", 1)
        self.assertEqual(caught.exception.details["blockers"], ["current_head"])
        self.edition("two", {"empty": []}, parent=1)
        self.assertEqual(self.vault.custody.retire("ridge", 1)["retired_at"], self.clock())

    def test_retirement_and_holding_are_serializable(self):
        self.edition("one", {"f": [b"a"]})
        self.edition("two", {"f": [b"b"]}, parent=1)
        self.clock.advance(60)
        barrier = Barrier(2)

        def hold():
            barrier.wait(timeout=5)
            try:
                self.vault.custody.hold("ridge", 1, "reference", "research report")
                return "held"
            except VaultError:
                return "cannot_hold"

        def retire():
            barrier.wait(timeout=5)
            try:
                self.vault.custody.retire("ridge", 1)
                return "retired"
            except VaultError:
                return "cannot_retire"

        with ThreadPoolExecutor(max_workers=2) as workers:
            holding = workers.submit(hold)
            retiring = workers.submit(retire)
            results = holding.result(timeout=10), retiring.result(timeout=10)
        self.assertIn(results, [("held", "cannot_retire"), ("cannot_hold", "retired")])
        self.assertEqual(self.vault.custody.forecast(10)["unreferenced"]["count"], 0 if results[0] == "held" else 1)

    def test_marked_object_cannot_acquire_new_receipt_until_sweep_finishes(self):
        original = self.open("obsolete", {"f": [b"a"]})
        self.upload(original["id"], b"a")
        self.vault.deposits.cancel(original["id"])
        self.vault.custody.mark(10)
        newer = self.open("fresh", {"f": [b"a"]})
        self.assertEqual(newer["pending"][0]["digest"], sha(b"a"))
        with self.assertRaisesRegex(VaultError, "being reclaimed"):
            self.upload(newer["id"], b"a")
        self.vault.custody.sweep(10)
        self.assertTrue(self.upload(newer["id"], b"a")["stored"])
        self.assertEqual(self.vault.catalog.publish(newer["id"])["revision"], 1)

    def test_sweep_recovers_after_unlink_before_catalog_acknowledgment(self):
        aborted = self.open("abandoned", {"f": [b"unused"]})
        self.upload(aborted["id"], b"unused")
        self.vault.deposits.cancel(aborted["id"])
        self.vault.custody.mark(3)
        actual_remove = self.vault.media.remove

        def lose_commit(digest):
            actual_remove(digest)
            raise OSError("simulated process interruption")

        with patch.object(self.vault.media, "remove", side_effect=lose_commit):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.vault.custody.sweep(3)
        self.assertFalse(self.vault.media.path(sha(b"unused")).exists())
        self.assertEqual(self.vault.custody.forecast(2)["marked_count"], 1)
        self.restart()
        self.assertEqual(self.vault.custody.sweep(3)["bytes"], 6)
        self.assertEqual(self.vault.custody.forecast(2)["marked_count"], 0)

    def test_reconcile_protects_catalog_objects_and_separate_pending_files(self):
        self.edition("primary", {"f": [b"verified"]})
        for payload in (b"orphan-one", b"orphan-two"):
            self.vault.media.install(sha(payload), len(payload), BytesIO(payload))
        pending = self.vault.media.path(sha(b"verified")).parent / (".pending-" + "f" * 32)
        pending.write_bytes(b"partial")
        after = ""
        observed = []
        while after is not None:
            page = self.vault.custody.reconcile(after, 1, remove=False)
            observed.extend(page["orphans"])
            after = page["next_after"]
        self.assertEqual(sorted(observed), sorted([sha(b"orphan-one"), sha(b"orphan-two")]))
        report = self.vault.custody.reconcile("", 10, remove=True)
        self.assertEqual(set(report["orphans"]), set(observed))
        self.assertTrue(self.vault.media.path(sha(b"verified")).exists())
        self.assertTrue(pending.exists())
        self.assertEqual(len(self.vault.custody.clean_staging(10)), 1)
        self.assertFalse(pending.exists())

    def test_shared_damage_propagates_and_repair_is_bound_to_incident(self):
        self.vault.catalog.create_collection("valley", 60)
        self.edition("a", {"f": [b"shared"]})
        self.edition("b", {"g": [b"shared"], "safe": [b"fine"]}, collection="valley")
        obj = self.vault.media.path(sha(b"shared"))
        obj.unlink()
        with self.assertRaises(VaultError) as failed_restore:
            self.vault.delivery.file("ridge", 1, "f")
        self.assertEqual(failed_restore.exception.code, "object_unreadable")
        self.assertEqual(self.vault.catalog.revision("valley", 1)["state"], "degraded")
        coverage = self.vault.inspection.coverage("valley", 1)
        self.assertEqual([(item["path"], item["restorable"]) for item in coverage["files"]],
                         [("g", False), ("safe", True)])
        scan = self.vault.integrity.scan(limit=10)
        self.assertEqual({item["condition"] for item in scan["items"]}, {"missing", "verified"})
        report = self.vault.integrity.incidents(after=0, limit=5, open_only=True)
        self.assertEqual(len(report["items"]), 1)
        first = report["items"][0]
        self.assertEqual([r["collection"] for r in first["revisions"]], ["ridge", "valley"])
        with self.assertRaises(VaultError):
            self.vault.integrity.repair(sha(b"shared"), first["id"], BytesIO(b"mutate"))
        self.assertFalse(obj.exists())
        self.vault.integrity.repair(sha(b"shared"), first["id"], BytesIO(b"shared"))
        self.assertEqual(self.vault.catalog.revision("valley", 1)["state"], "available")
        obj.write_bytes(b"broken")
        self.vault.integrity.scan(limit=10)
        reopened = self.vault.integrity.incidents(after=0, limit=5, open_only=True)["items"][0]
        self.assertNotEqual(reopened["id"], first["id"])
        with self.assertRaisesRegex(VaultError, "no longer targets"):
            self.vault.integrity.repair(sha(b"shared"), first["id"], BytesIO(b"shared"))

    def test_footprint_counts_distinct_content_and_cross_collection_protection(self):
        self.vault.catalog.create_collection("valley", 60)
        self.edition("r", {"f": [b"a", b"a", b"bb"]})
        self.edition("v", {"b": [b"bb"]}, collection="valley")
        active = self.open("pending", {"f": [b"a", b"xxx"]}, parent=1)
        self.upload(active["id"], b"xxx")
        report = self.vault.footprint.collection("ridge")
        self.assertEqual(report["objects"]["total"], {"count": 3, "bytes": 6})
        self.assertEqual(report["objects"]["deposit_only"], {"count": 1, "bytes": 3})
        self.assertEqual(report["objects"]["shared_outside_collection"], {"count": 1, "bytes": 2})
        self.assertEqual(report["deposits"]["claimed_bytes_with_repetition"], 4)
        refs = self.vault.inspection.object_references(sha(b"bb"), 1)
        self.assertEqual(refs["editions"]["protecting"], 2)
        self.assertEqual(len(refs["editions"]["items"]), 1)

    def test_audit_separates_catalog_faults_from_media_incidents(self):
        initial = self.edition("survey", {"g": [b"x", b"y"]})
        clean = self.vault.audit.check(None, 10)
        self.assertTrue(clean["consistent"])
        self.assertEqual(clean["checked"]["revisions"], 1)
        self.vault.media.path(sha(b"x")).unlink()
        self.vault.integrity.scan(limit=10)
        self.assertTrue(self.vault.audit.check("ridge", 10)["consistent"])
        with self.vault.db.transaction() as connection:
            connection.execute("DELETE FROM revision_parts WHERE digest=?", (sha(b"y"),))
            connection.execute("UPDATE revisions SET retain_until=retain_until+1 WHERE number=?",
                               (initial["revision"],))
        damaged = self.vault.audit.check(None, 1)
        self.assertFalse(damaged["consistent"])
        self.assertTrue(damaged["examples_truncated"])
        self.assertEqual(damaged["issue_counts"], {"retention_differs": 1, "missing_manifest_part": 1})
