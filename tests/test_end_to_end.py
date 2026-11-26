from unittest.mock import patch

from strata.field.check import check_parcel
from strata.field.parcel import build_parcel, open_parcel
from strata.field.receive import verify_package
from strata.field.uplink import Uplink
from strata.runtime.errors import VaultError, unavailable
from support import Scenario, manifest, sha


class InProcessEndpoint:
    def __init__(self, http):
        self.http = http
        self.drop = None
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        reply = self.http.request(method, path, **kwargs)
        if self.drop is not None and self.drop in path:
            self.drop = None
            raise unavailable("Receipt was lost after the vault committed")
        result = reply.json()
        if reply.is_error:
            error = result["error"]
            raise VaultError(error["code"], error["message"], reply.status_code, **error["details"])
        return result


class OperationalAcceptance(Scenario):
    def test_field_interruption_through_archival_recovery(self):
        field = self.root / "source"
        (field / "survey").mkdir(parents=True)
        (field / "survey" / "profile.dat").write_bytes(b"gridGRIdgrid")
        (field / "operator.txt").write_bytes(b"grid")
        (field / "blank").write_bytes(b"")
        build_parcel(field, self.root / "parcel", collection="ridge", token="field-01",
                     parent=0, lifetime=300, part_size=4)
        parcel = open_parcel(self.root / "parcel")
        inspection = check_parcel(parcel)
        self.assertEqual((inspection.files, inspection.unique_parts, inspection.logical_bytes), (3, 2, 16))
        self.assertEqual(inspection.available_bytes, 8)
        station = InProcessEndpoint(self.http)
        station.drop = "/parts/"
        uplink = Uplink(station)
        with self.assertRaisesRegex(VaultError, "lost"):
            uplink.send(parcel)
        self.assertEqual(self.vault.custody.forecast(3)["object_bytes"], 4)
        first = uplink.send(parcel)
        self.assertEqual((first.revision, first.transmitted), (1, 1))
        replay = uplink.send(parcel)
        self.assertEqual((replay.deposit, replay.revision, replay.transmitted), (first.deposit, 1, 0))

        edition = self.http.get("/collections/ridge/revisions/1").json()
        hold = self.http.put("/collections/ridge/revisions/1/holds/report-v1",
                             json={"reason": "Artifact cited by appendix"})
        self.assertEqual(hold.status_code, 200)
        self.edition("amended", {"survey/profile.dat": [b"grid", b"next"]}, parent=1)
        self.clock.advance(90)
        self.assertEqual(self.http.post("/collections/ridge/revisions/1/retire").json()["error"]["details"]["blockers"],
                         ["active_holds"])
        self.vault.media.path(sha(b"grid")).write_bytes(b"worn")
        scan = self.http.post("/operations/scan").json()
        self.assertIn("digest_mismatch", [item["condition"] for item in scan["items"]])
        incidents = self.http.get("/operations/incidents").json()["items"]
        self.assertEqual([item["number"] for item in incidents[0]["revisions"]], [1, 2])
        self.assertEqual(self.http.get("/collections/ridge/revisions/1/package").status_code, 409)

        repaired = self.http.put(f"/operations/objects/{sha(b'grid')}/repair/{incidents[0]['id']}",
                                 content=b"grid", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(repaired.status_code, 200)
        self.assertEqual(self.http.get("/collections/ridge/revisions/2").json()["state"], "available")
        package_path = self.root / "out.zip"
        package_path.write_bytes(self.http.get("/collections/ridge/revisions/1/package").content)
        destination = self.root / "recovered"
        restored = verify_package(package_path, expected_manifest=edition["manifest_sha256"],
                                  expected_collection="ridge", expected_revision=1, destination=destination)
        self.assertEqual((restored.files, restored.bytes), (3, 16))
        self.assertEqual((destination / "survey" / "profile.dat").read_bytes(), b"gridGRIdgrid")

        self.assertEqual(self.http.delete("/collections/ridge/revisions/1/holds/report-v1").status_code, 200)
        self.assertEqual(self.http.post("/collections/ridge/revisions/1/retire").status_code, 200)
        self.assertEqual(self.http.post("/operations/mark").json()["marked"], [sha(b"GRId")])
        self.assertEqual(self.http.post("/operations/sweep").json()["bytes"], 4)
        self.assertEqual(self.http.get("/collections/ridge/revisions/1/package").status_code, 410)
        self.assertTrue(self.http.get("/operations/audit").json()["consistent"])
        self.assertTrue(self.vault.media.path(sha(b"grid")).is_file())

    def test_lost_publish_receipt_does_not_create_another_edition(self):
        field = self.root / "source"
        field.mkdir()
        (field / "report").write_bytes(b"0123")
        parcel = build_parcel(field, self.root / "parcel", collection="ridge", token="field", parent=0, lifetime=60)
        station = InProcessEndpoint(self.http)
        station.drop = "/publish"
        uplink = Uplink(station)
        with self.assertRaises(VaultError):
            uplink.send(parcel)
        replay = uplink.send(parcel)
        self.assertEqual((replay.revision, replay.transmitted), (1, 0))
        self.assertEqual(len(self.vault.catalog.revisions("ridge", 0, 10)["items"]), 1)
        published = [event for event in self.vault.events("ridge", 0, 20)["items"]
                     if event["kind"] == "revision_published"]
        self.assertEqual(len(published), 1)

    def test_corrupt_cached_parcel_is_detected_without_uploading_affected_bytes(self):
        field = self.root / "source"
        field.mkdir()
        (field / "scan").write_bytes(b"hill")
        parcel = build_parcel(field, self.root / "parcel", collection="ridge", token="field", parent=0, lifetime=60)
        parcel.media().path(sha(b"hill")).write_bytes(b"dust")
        check = check_parcel(parcel)
        self.assertEqual(check.errors, 1)
        self.assertEqual(check.available_bytes, 0)
        station = InProcessEndpoint(self.http)
        with self.assertRaises(VaultError):
            Uplink(station).send(parcel)
        self.assertFalse(any(method == "PUT" for method, _ in station.calls))
        self.assertEqual(self.vault.custody.forecast(10)["object_count"], 0)

    def test_transport_size_type_and_payload_are_enforced(self):
        prepare = self.http.post("/collections/ridge/deposits",
                                 json={"token": "b", "parent": 0, "lifetime_seconds": 300,
                                       "manifest": manifest({"f": [b"xyz"]})})
        self.assertEqual(prepare.status_code, 201)
        uri = "/deposits/" + prepare.json()["id"] + "/parts/" + sha(b"xyz")
        self.assertEqual(self.http.put(uri, content=b"xyz").status_code, 415)
        binary = {"Content-Type": "application/octet-stream"}
        self.assertEqual(self.http.put(uri, content=b"xy", headers=binary).status_code, 422)
        self.assertEqual(self.http.put(uri, content=b"xya", headers=binary).status_code, 422)
        self.assertEqual(self.http.put(uri, content=iter([b"x", b"yz", b"!"]), headers=binary).status_code, 422)
        self.assertEqual(self.http.put(uri, content=iter([b"x", b"yz"]), headers=binary).status_code, 200)
        self.assertEqual(self.http.post("/collections", content=b" " * (2 * 1024 * 1024 + 1)).status_code, 413)

    def test_prepared_parcel_requires_explicit_directory_and_fresh_destination(self):
        directory = self.root / "source"
        directory.mkdir()
        (directory / "bytes").write_bytes(b"source")
        with self.assertRaisesRegex(VaultError, "outside the source"):
            build_parcel(directory, directory / "parcel", collection="ridge", token="field",
                         parent=0, lifetime=300)
        with patch("strata.field.parcel._seal_descriptor", side_effect=OSError("reported full disk")):
            with self.assertRaisesRegex(OSError, "full disk"):
                build_parcel(directory, self.root / "parcel", collection="ridge", token="field",
                             parent=0, lifetime=300)
        self.assertEqual(list(self.root.glob(".parcel-*")), [])
        self.assertFalse((self.root / "parcel").exists())
