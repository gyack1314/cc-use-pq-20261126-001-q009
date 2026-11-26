from io import BytesIO
import json
from zipfile import ZipFile

from strata.field.receive import verify_package
from strata.runtime.errors import VaultError
from support import Scenario, sha


class Delivery(Scenario):
    def test_ranges_span_fragments_and_include_stable_digest(self):
        data = [b"north", b"east", b"west"]
        self.edition("ridge", {"survey/main.tif": data, "empty": []})
        link = "/collections/ridge/revisions/1/file"
        full = self.http.get(link, params={"path": "survey/main.tif"})
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"northeastwest")
        self.assertEqual(full.headers["etag"], '"' + sha(full.content) + '"')
        partial = self.http.get(link, params={"path": "survey/main.tif"}, headers={"Range": "bytes=3-7"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, b"theas")
        self.assertEqual(partial.headers["content-range"], "bytes 3-7/13")
        self.assertEqual(partial.headers["content-length"], "5")
        self.assertEqual(partial.headers["etag"], full.headers["etag"])
        suffix = self.http.get(link, params={"path": "survey/main.tif"}, headers={"Range": "bytes=-4"})
        self.assertEqual(suffix.content, b"west")
        upper = self.http.get(link, params={"path": "survey/main.tif"}, headers={"Range": "bytes=12-99"})
        self.assertEqual(upper.headers["content-range"], "bytes 12-12/13")
        self.assertEqual(upper.content, b"t")
        empty = self.http.get(link, params={"path": "empty"})
        self.assertEqual(empty.headers["content-length"], "0")
        invalid_range = self.http.get(link, params={"path": "empty"}, headers={"Range": "bytes=0-"})
        self.assertEqual(invalid_range.status_code, 416)
        self.assertEqual(invalid_range.headers["content-range"], "bytes */0")
        multipart = self.http.get(link, params={"path": "survey/main.tif"}, headers={"Range": "bytes=0-1,4-6"})
        self.assertEqual(multipart.status_code, 416)

    def test_small_range_fails_if_unselected_bytes_are_bad(self):
        self.edition("range", {"survey": [b"a", b"b"]})
        self.vault.media.path(sha(b"b")).write_bytes(b"c")
        reply = self.http.get("/collections/ridge/revisions/1/file", params={"path": "survey"},
                              headers={"Range": "bytes=0-0"})
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(reply.json()["error"]["details"]["digest"], sha(b"b"))
        self.assertNotIn("content-range", reply.headers)
        self.assertEqual(self.vault.catalog.revision("ridge", 1)["state"], "degraded")

    def test_package_is_reproducible_and_restored_by_external_reference(self):
        issued = self.edition("pack", {"maps/石片.tif": [b"a", b"bb"], "note": []})
        path = "/collections/ridge/revisions/1/package"
        first = self.http.get(path)
        self.clock.advance(5)
        repeat = self.http.get(path)
        self.assertEqual(first.content, repeat.content)
        self.assertEqual(first.headers["x-manifest-sha256"], issued["manifest_sha256"])
        with ZipFile(BytesIO(first.content)) as package:
            self.assertEqual(package.namelist(), ["_vault/manifest.json", "_vault/receipt.json", "maps/石片.tif", "note"])
            receipt = json.loads(package.read("_vault/receipt.json"))
            self.assertEqual(receipt["published_at"], issued["published_at"])
            self.assertEqual(receipt["revision"], 1)
            self.assertEqual(sha(package.read("_vault/manifest.json")), issued["manifest_sha256"])
            self.assertEqual(package.read("maps/石片.tif"), b"abb")
        source = self.root / "package.zip"
        source.write_bytes(first.content)
        target = self.root / "restored"
        restored = verify_package(source, expected_manifest=issued["manifest_sha256"],
                                  expected_collection="ridge", expected_revision=1, destination=target)
        self.assertEqual((restored.files, restored.bytes), (2, 3))
        self.assertEqual((target / "maps" / "石片.tif").read_bytes(), b"abb")
        self.assertEqual((target / "note").read_bytes(), b"")
        self.assertFalse((target / "_vault").exists())

    def test_wrong_zip_payload_never_exposes_partially_extracted_files(self):
        issued = self.edition("recover", {"a": [b"first"], "z": [b"last"]})
        source = self.root / "damaged.zip"
        target = self.root / "result"
        response = self.http.get("/collections/ridge/revisions/1/package")
        with ZipFile(BytesIO(response.content)) as correct, ZipFile(source, "w") as wrong:
            for member in correct.namelist():
                wrong.writestr(member, b"harm" if member == "z" else correct.read(member))
        with self.assertRaisesRegex(VaultError, "part does not match"):
            verify_package(source, expected_manifest=issued["manifest_sha256"],
                           expected_collection="ridge", expected_revision=1, destination=target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".recovery-*")), [])

    def test_recovery_requires_matching_receipt_and_exact_zip_membership(self):
        issued = self.edition("recovery", {"a": [b"evidence"]})
        response = self.http.get("/collections/ridge/revisions/1/package")
        source = self.root / "outside.zip"
        source.write_bytes(response.content)
        with self.assertRaisesRegex(VaultError, "identity differs"):
            verify_package(source, expected_manifest=issued["manifest_sha256"],
                           expected_collection="ridge", expected_revision=2)
        with ZipFile(source, "a") as package:
            package.writestr("../outside", b"outside")
        with self.assertRaisesRegex(VaultError, "membership"):
            verify_package(source, expected_manifest=issued["manifest_sha256"],
                           expected_collection="ridge", expected_revision=1)
        self.assertFalse((self.root / "outside").exists())

    def test_compare_distinguishes_layout_and_content(self):
        self.edition("one", {"layout": [b"ab"], "removed": [b"x"], "changed": [b"old"], "untouched": []})
        self.edition("two", {"layout": [b"a", b"b"], "added": [b"y"], "changed": [b"new"], "untouched": []},
                     parent=1)
        result = self.http.get("/collections/ridge/compare", params={"older": 1, "newer": 2}).json()
        self.assertEqual([item["path"] for item in result["added"]], ["added"])
        self.assertEqual([item["path"] for item in result["removed"]], ["removed"])
        self.assertEqual([item["before"]["path"] for item in result["content_changed"]], ["changed"])
        self.assertEqual([item["before"]["path"] for item in result["layout_changed"]], ["layout"])
