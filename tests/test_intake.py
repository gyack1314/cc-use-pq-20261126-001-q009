from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier
import unittest

from strata.runtime.errors import VaultError
from strata.transfer.manifest import MAX_PART, archive_path, parse_manifest
from support import Scenario, manifest, sha


class ManifestContract(unittest.TestCase):
    def test_stable_file_order_preserves_part_order(self):
        first = manifest({"z": [b"b", b"a"], "a": []})
        second = manifest({"a": [], "z": [b"b", b"a"]})
        reversed_parts = manifest({"a": [], "z": [b"a", b"b"]})
        self.assertEqual(parse_manifest(first).fingerprint(), parse_manifest(second).fingerprint())
        self.assertNotEqual(parse_manifest(first).fingerprint(), parse_manifest(reversed_parts).fingerprint())
        self.assertEqual([entry.path for entry in parse_manifest(first).files], ["a", "z"])
        self.assertEqual(parse_manifest(first).unique_parts(), {sha(b"a"): 1, sha(b"b"): 1})

    def test_nonportable_names(self):
        for path in ("", "/name", "one//two", "../foo", "one/./two", "CON.txt", "lpt1",
                     "e\u0301", "foo.", "bar ", "dir\\entry", "x:y", "_VAULT/external", "foo/\x00"):
            with self.subTest(path=path), self.assertRaisesRegex(VaultError, "Archive path|archive path"):
                archive_path(path)
        self.assertEqual(archive_path("测绘/北壁.png"), "测绘/北壁.png")

    def test_a_directory_cannot_be_shadowed_or_aliased(self):
        for names in (("a", "a/b"), ("a/b", "a"), ("A/x", "a/y"), ("A", "a")):
            with self.subTest(names=names), self.assertRaisesRegex(VaultError, "overlap"):
                parse_manifest(manifest({key: [b"x"] for key in names}))

    def test_rejects_claimed_digest_at_more_than_one_length(self):
        value = manifest({"a": [b"x"], "b": [b"yy"]})
        value["files"][1]["parts"][0]["sha256"] = sha(b"x")
        with self.assertRaisesRegex(VaultError, "multiple sizes"):
            parse_manifest(value)
        value["files"][1]["parts"][0] = {"sha256": sha(b"yy"), "size": MAX_PART + 1}
        with self.assertRaisesRegex(VaultError, "outside its supported range"):
            parse_manifest(value)

    def test_boolean_size_and_incorrect_empty_digest_are_invalid(self):
        data = manifest({"empty": []})
        data["files"][0]["size"] = False
        with self.assertRaisesRegex(VaultError, "file size"):
            parse_manifest(data)
        data["files"][0]["size"] = 0
        data["files"][0]["sha256"] = sha(b"x")
        with self.assertRaisesRegex(VaultError, "empty SHA-256"):
            parse_manifest(data)

    def test_total_must_equal_ordered_parts(self):
        data = manifest({"surface": [b"abc", b"def"]})
        data["files"][0]["size"] = 7
        with self.assertRaisesRegex(VaultError, "File size does not match"):
            parse_manifest(data)
        data["version"] = 2
        with self.assertRaisesRegex(VaultError, "manifest version"):
            parse_manifest(data)


class Intake(Scenario):
    def test_resume_upload_and_publication_idempotently_across_restart(self):
        raw = {"images/z": [b"first", b"second", b"first"], "notes": []}
        opened = self.open("north-2026", raw)
        latest = self.vault.deposits.get(opened["id"])
        self.assertEqual(len(latest["pending"]), 2)
        self.assertEqual(latest["manifest_sha256"], opened["manifest_sha256"])
        self.assertTrue(self.upload(opened["id"], b"second")["stored"])
        self.restart()
        resumed = self.open("north-2026", dict(reversed(list(raw.items()))))
        self.assertEqual(resumed["id"], opened["id"])
        self.assertEqual([item["digest"] for item in resumed["pending"]], [sha(b"first")])
        replayed = self.upload(opened["id"], b"second")
        self.assertTrue(replayed["duplicate"])
        self.assertFalse(replayed["stored"])
        self.upload(opened["id"], b"first")
        published = self.vault.catalog.publish(opened["id"])
        self.assertEqual(published["revision"], 1)
        self.assertEqual(self.vault.catalog.publish(opened["id"]), published)
        self.assertEqual(self.open("north-2026", raw)["status"], "published")
        self.assertEqual(self.vault.custody.forecast(10)["object_count"], 2)

    def test_idempotency_rejects_reused_token_with_changed_binding(self):
        opened = self.open("station", {"scan": [b"x"]})
        with self.assertRaisesRegex(VaultError, "another deposit"):
            self.open("station", {"scan": [b"y"]})
        with self.assertRaisesRegex(VaultError, "another deposit"):
            self.open("station", {"scan": [b"x"]}, lifetime=120)
        self.assertEqual(self.vault.deposits.get(opened["id"])["status"], "open")

    def test_valid_objects_cannot_publish_for_inconsistent_file_digest(self):
        raw = manifest({"sweep": [b"sub", b"layer"]})
        raw["files"][0]["sha256"] = sha(b"layersub")
        opened = self.vault.deposits.open("ridge", "bad-order", 0, 60, raw)
        for part in (b"sub", b"layer"):
            self.upload(opened["id"], part)
        with self.assertRaisesRegex(VaultError, "Ordered objects"):
            self.vault.catalog.publish(opened["id"])
        self.assertEqual(self.vault.catalog.collection("ridge")["head"], 0)
        self.assertEqual(self.vault.deposits.get(opened["id"])["pending"], [])
        self.assertEqual(self.vault.integrity.incidents(after=0, limit=10, open_only=True)["items"], [])

    def test_bytes_are_bound_to_expected_hash_and_length(self):
        deposit = self.open("m", {"f": [b"xyz"]})["id"]
        for wrong in (b"xya", b"x", b"xyza"):
            with self.subTest(wrong=wrong), self.assertRaises(VaultError):
                self.vault.deposits.receive(deposit, sha(b"xyz"), BytesIO(wrong))
        with self.assertRaisesRegex(VaultError, "expected part does not exist"):
            self.upload(deposit, b"another")
        self.assertEqual(self.vault.custody.forecast(2)["object_count"], 0)
        with self.assertRaisesRegex(VaultError, "incomplete"):
            self.vault.catalog.publish(deposit)

    def test_concurrent_head_publication_selects_one_winner(self):
        first = self.open("first", {"a": [b"1"]})["id"]
        second = self.open("second", {"b": [b"2"]})["id"]
        self.upload(first, b"1")
        self.upload(second, b"2")
        barrier = Barrier(2)

        def publish(deposit):
            barrier.wait(timeout=5)
            try:
                return self.vault.catalog.publish(deposit)["revision"]
            except VaultError as error:
                return int(error.details["current"]) + 100

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(publish, (first, second)))
        self.assertEqual(sorted(outcomes), [1, 101])
        first_state = self.vault.deposits.get(first)
        second_state = self.vault.deposits.get(second)
        self.assertEqual({first_state["status"], second_state["status"]}, {"open", "published"})
        self.assertEqual(len(self.vault.catalog.revisions("ridge", 0, 10)["items"]), 1)

    def test_cancel_and_expiration_drop_receipts_at_controlled_points(self):
        expired = self.open("expired", {"f": [b"1"]})["id"]
        canceled = self.open("cancel", {"f": [b"2"]}, lifetime=120)["id"]
        self.upload(expired, b"1")
        self.upload(canceled, b"2")
        self.vault.deposits.cancel(canceled)
        self.assertEqual(self.vault.deposits.cancel(canceled)["status"], "canceled")
        self.clock.advance(60)
        self.assertEqual(self.vault.deposits.get(expired)["status"], "expired")
        self.assertEqual(self.vault.custody.forecast(5)["unreferenced"]["count"], 1)
        with self.assertRaises(VaultError):
            self.upload(expired, b"1")
        self.assertEqual(self.vault.deposits.expire(1), [expired])
        self.assertEqual(self.vault.deposits.expire(100), [])
        self.assertEqual(self.vault.custody.forecast(5)["unreferenced"]["count"], 2)
