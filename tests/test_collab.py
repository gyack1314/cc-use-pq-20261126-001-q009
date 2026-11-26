"""更正候选、显式移动与多方合订的验收测试。"""

from io import BytesIO
import unittest

from strata.field.parcel import build_parcel
from strata.field.uplink import Uplink
from strata.runtime.errors import VaultError
from support import Scenario, manifest, sha


class _Endpoint:
    def __init__(self, http):
        self.http = http

    def request(self, method, path, **kwargs):
        reply = self.http.request(method, path, **kwargs)
        result = reply.json()
        if reply.is_error:
            error = result["error"]
            raise VaultError(error["code"], error["message"], reply.status_code,
                             **error["details"])
        return result


class CollabScenario(Scenario):
    def submit(self, token, entries, parent):
        return self.edition(token, entries, parent=parent)

    def open_candidate(self, token, entries, *, parent=1, changes=None, lifetime=600,
                       collection="ridge"):
        return self.vault.candidates.open(
            collection, token, parent, lifetime, manifest(entries), changes)

    def fill_candidate(self, cid, entries):
        source = {sha(part): part for chunks in entries.values() for part in chunks}
        opened = self.vault.candidates.get(cid)
        for item in opened["pending"]:
            self.vault.candidates.receive(cid, item["digest"],
                                          BytesIO(source[item["digest"]]))

    def decided_publish(self, cid, *, choice="head", index=0):
        plan = self.vault.merges.plan(cid)
        conflict = plan["conflicts"][index]
        self.vault.merges.decide(cid, {"key": conflict["key"], "choice": choice})
        return self.vault.merges.publish(cid)


class CandidateIntake(CollabScenario):
    def test_late_package_is_an_independent_candidate_with_resumable_parts(self):
        self.edition("base", {"f": [b"base"]})
        self.edition("first-group", {"f": [b"first"]}, parent=1)

        opened = self.open_candidate("second-group", {"f": [b"base"], "new": [b"measure"]})
        self.assertRegex(opened["id"], r"^[0-9a-f]{32}$")
        self.assertEqual((opened["status"], opened["parent"], opened["base_revision"]),
                         ("open", 1, 1))
        # 已在库的 base 分片自动认领，只缺新测量片。
        self.assertEqual([p["digest"] for p in opened["pending"]], [sha(b"measure")])

        replay = self.open_candidate("second-group", {"f": [b"base"], "new": [b"measure"]})
        self.assertEqual(replay["id"], opened["id"])

        receipt = self.vault.candidates.receive(
            opened["id"], sha(b"measure"), BytesIO(b"measure"))
        self.assertTrue(receipt["stored"])
        duplicate = self.vault.candidates.receive(
            opened["id"], sha(b"measure"), BytesIO(b"measure"))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.vault.candidates.get(opened["id"])["pending"], [])

        # 普通存款合同不变：基于旧头的线性存款仍被拒绝。
        with self.assertRaisesRegex(VaultError, "superseded|current head|differs"):
            self.open("late-linear", {"x": [b"y"]}, parent=1)

    def test_token_binding_rejects_changed_candidate_or_changes(self):
        opened = self.open_candidate("crew", {"q": [b"a"]}, parent=0)
        with self.assertRaisesRegex(VaultError, "another candidate"):
            self.open_candidate("crew", {"q": [b"b"]}, parent=0)
        self.edition("moved-base", {"old": [b"a"]})
        other = self.open_candidate(
            "crew-move", {"new": [b"a"]}, parent=1,
            changes=[{"op": "move", "from": "old", "to": "new"}])
        self.assertRegex(other["changes_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(opened["id"])

    def test_incomplete_candidate_cannot_publish_merge(self):
        self.edition("base", {"f": [b"base"]})
        self.edition("first", {"f": [b"one"]}, parent=1)
        cid = self.open_candidate("late", {"fresh": [b"xyz"]})["id"]
        with self.assertRaisesRegex(VaultError, "incomplete"):
            self.vault.merges.publish(cid)

    def test_empty_history_candidate_publishes_first_revision(self):
        cid = self.open_candidate("genesis", {"a": [b"aaa"], "empty": []}, parent=0)["id"]
        self.vault.candidates.receive(cid, sha(b"aaa"), BytesIO(b"aaa"))
        plan = self.vault.merges.plan(cid)
        self.assertEqual((plan["base_revision"], plan["head_revision"], plan["clean"]),
                         (0, 0, True))
        published = self.vault.merges.publish(cid)
        self.assertEqual((published["revision"], published["parent"]), (1, 0))
        self.assertEqual(self.vault.delivery.file("ridge", 1, "a").spool.read(), b"aaa")


class ExplicitMoveMerging(CollabScenario):
    def test_equal_bytes_and_matching_basename_are_not_inferred_as_move(self):
        self.edition("base", {"survey/stone.tif": [b"same-bytes"]})
        self.edition("modifier", {"survey/stone.tif": [b"changed-by-a"]}, parent=1)
        # 晚到包把同字节、同名文件放到另一目录，但没有移动记录。
        cid = self.open_candidate(
            "mover-no-record",
            {"archive/stone.tif": [b"same-bytes"]})["id"]
        plan = self.vault.merges.plan(cid)
        self.assertEqual([c["type"] for c in plan["conflicts"]], ["delete_modify"])
        # 不能把 A 的修改自动带到新位置。
        self.assertFalse(any(e["path"] == "archive/stone.tif"
                             and e["sha256"] == sha(b"changed-by-a")
                             for e in plan["entries"]))

    def test_other_groups_modification_lands_on_recorded_move(self):
        self.edition("base", {"survey/profile.dat": [b"gridGRIdgrid"]})
        self.edition("modifier", {"survey/profile.dat": [b"grid", b"next"]}, parent=1)
        cid = self.open_candidate(
            "mover", {"survey/profile-v2.dat": [b"gridGRIdgrid"]},
            changes=[{"op": "move", "from": "survey/profile.dat",
                      "to": "survey/profile-v2.dat"}])["id"]
        plan = self.vault.merges.plan(cid)
        self.assertTrue(plan["clean"], plan["conflicts"])
        entry = plan["entries"][0]
        self.assertEqual(entry["path"], "survey/profile-v2.dat")
        self.assertEqual(entry["sha256"], sha(b"gridnext"))

        published = self.vault.merges.publish(cid)
        self.assertEqual(published["revision"], 3)
        checkout = self.vault.delivery.file("ridge", 3, "survey/profile-v2.dat")
        self.assertEqual(checkout.spool.read(), b"gridnext")
        with self.assertRaises(VaultError) as missing:
            self.vault.delivery.file("ridge", 3, "survey/profile.dat")
        self.assertEqual(missing.exception.code, "not_found")

    def test_inverse_direction_head_move_record_followed_by_candidate_edit(self):
        # 已发布一侧携带明确移动记录，候选在旧路径上修改。
        self.edition("base", {"f": [b"orig"]})
        opened = self.vault.deposits.open(
            "ridge", "mover-a", 1, 600, manifest({"moved": [b"orig"]}),
            changes=[{"op": "move", "from": "f", "to": "moved"}])
        self.vault.deposits.receive(opened["id"], sha(b"orig"), BytesIO(b"orig"))
        self.vault.catalog.publish(opened["id"])

        cid = self.open_candidate("editor-b", {"f": [b"edited"]})["id"]
        self.vault.candidates.receive(cid, sha(b"edited"), BytesIO(b"edited"))
        plan = self.vault.merges.plan(cid)
        self.assertTrue(plan["clean"], plan["conflicts"])
        self.assertEqual(plan["entries"][0]["path"], "moved")
        self.assertEqual(plan["entries"][0]["sha256"], sha(b"edited"))
        published = self.vault.merges.publish(cid)
        self.assertEqual(self.vault.delivery.file("ridge", 3, "moved").spool.read(), b"edited")

    def test_invalid_move_records_are_rejected(self):
        self.edition("base", {"f": [b"orig"]})
        with self.assertRaisesRegex(VaultError, "source is absent"):
            self.open_candidate("bad-source", {"q": [b"x"]},
                                changes=[{"from": "missing", "to": "q"}])
        with self.assertRaisesRegex(VaultError, "target is absent"):
            self.open_candidate("bad-target", {"q": [b"x"]},
                                changes=[{"from": "f", "to": "nowhere"}])
        with self.assertRaisesRegex(VaultError, "must not remain"):
            self.open_candidate("both-paths", {"f": [b"orig"], "q": [b"orig"]},
                                changes=[{"from": "f", "to": "q"}])


class ConflictReview(CollabScenario):
    def _collision_setup(self, token_b="arranger-b"):
        self.edition("base", {"a": [b"AA"], "b": [b"BB"]})
        self.edition("head-arranges", {"a": [b"AA"], "b": [b"BB"], "q": [b"XXX"]}, parent=1)
        cid = self.open_candidate(
            token_b, {"b": [b"BB"], "q": [b"AA"]},
            changes=[{"from": "a", "to": "q"}])["id"]
        return cid

    def test_different_materials_at_same_location_need_archivist_choice(self):
        cid = self._collision_setup()
        plan = self.vault.merges.plan(cid)
        self.assertEqual([c["type"] for c in plan["conflicts"]], ["path_conflict"])
        conflict = plan["conflicts"][0]
        digests = {source["sha256"] for source in conflict["sources"]}
        self.assertEqual(digests, {sha(b"XXX"), sha(b"AA")})
        self.assertIn("head", conflict["choices"])
        self.assertIn("candidate", conflict["choices"])
        # 两边来源都带可取回链接。
        self.assertTrue(conflict["hrefs"]["head"].startswith("/collections/ridge/revisions/2/"))
        self.assertIn(f"/candidates/{cid}/file", conflict["hrefs"]["candidate"])

        with self.assertRaises(VaultError) as blocked:
            self.vault.merges.publish(cid)
        self.assertEqual(blocked.exception.code, "conflict")

    def test_choose_candidate_material_for_collision_path(self):
        cid = self._collision_setup("choose-c")
        published = self.decided_publish(cid, choice="candidate")
        self.assertEqual(published["revision"], 3)
        self.assertEqual(self.vault.delivery.file("ridge", 3, "q").spool.read(), b"AA")
        self.assertEqual(self.vault.delivery.file("ridge", 3, "b").spool.read(), b"BB")

    def test_choose_head_material_for_collision_path(self):
        cid = self._collision_setup("choose-h")
        published = self.decided_publish(cid, choice="head")
        self.assertEqual(published["revision"], 3)
        self.assertEqual(self.vault.delivery.file("ridge", 3, "q").spool.read(), b"XXX")

    def test_delete_versus_modify_can_be_kept(self):
        self.edition("base", {"f": [b"orig"]})
        self.edition("modifier", {"f": [b"changed"]}, parent=1)
        cid = self.open_candidate("deleter", {"g": [b"other"]})["id"]
        self.fill_candidate(cid, {"g": [b"other"]})
        plan = self.vault.merges.plan(cid)
        conflict = plan["conflicts"][0]
        self.assertEqual(conflict["type"], "delete_modify")
        self.assertEqual(conflict["deleting_side"], "candidate")
        self.vault.merges.decide(cid, {"key": conflict["key"], "choice": "keep"})
        published = self.vault.merges.publish(cid)
        self.assertEqual(self.vault.delivery.file("ridge", published["revision"], "f").spool.read(),
                         b"changed")
        self.assertEqual(self.vault.delivery.file("ridge", published["revision"], "g").spool.read(),
                         b"other")

    def test_delete_versus_modify_can_delete(self):
        self.edition("base", {"f": [b"orig"]})
        self.edition("modifier", {"f": [b"changed"]}, parent=1)
        cid = self.open_candidate("deleter2", {"keeper": []})["id"]
        plan = self.vault.merges.plan(cid)
        conflict = next(c for c in plan["conflicts"] if c["type"] == "delete_modify")
        self.vault.merges.decide(cid, {"key": conflict["key"], "choice": "delete"})
        published = self.vault.merges.publish(cid)
        with self.assertRaises(VaultError):
            self.vault.delivery.file("ridge", published["revision"], "f")
        self.assertEqual(self.vault.delivery.file(
            "ridge", published["revision"], "keeper").spool.read(), b"")


class DecisionFreshness(CollabScenario):
    def _delete_modify_candidate(self, token="deleter"):
        self.edition("base", {"f": [b"orig"]})
        self.edition("mod-a", {"f": [b"a-change"]}, parent=1)
        cid = self.open_candidate(token, {"g": [b"other"]})["id"]
        self.fill_candidate(cid, {"g": [b"other"]})
        key = self.vault.merges.plan(cid)["conflicts"][0]["key"]
        self.vault.merges.decide(cid, {"key": key, "choice": "keep", "reason": "field note"})
        return cid, key

    def test_new_publication_shelves_decisions_until_reviewed(self):
        cid, key = self._delete_modify_candidate("strict")
        # 即使新发布只增加无关 n，旧决定也先降为审阅记录，不被默默使用。
        self.edition("interloper", {"f": [b"a-change"], "n": [b"new"]}, parent=2)

        with self.assertRaises(VaultError) as blocked:
            self.vault.merges.publish(cid)
        self.assertTrue(blocked.exception.details.get("review_required"))

        plan = self.vault.merges.plan(cid)
        self.assertEqual(plan["conflicts"][0]["status"], "pending")
        record = plan["review_history"][-1]
        self.assertEqual(record["state"], "stale")
        self.assertFalse(record["context_current"])
        # 冲突指纹未变：档案员显式 reconfirm 即可在当前上下文重申。
        self.assertTrue(record["unchanged_conflict"])
        self.assertEqual(record["decision"]["reason"], "field note")

        self.vault.merges.reconfirm(cid)
        plan = self.vault.merges.plan(cid)
        self.assertEqual(plan["conflicts"][0]["status"], "resolved")
        published = self.vault.merges.publish(cid)
        self.assertEqual(published["revision"], 4)
        self.assertEqual(self.vault.delivery.file("ridge", 4, "n").spool.read(), b"new")
        self.assertEqual(self.vault.delivery.file("ridge", 4, "f").spool.read(), b"a-change")

    def test_affected_change_blocks_until_rereviewed(self):
        cid, key = self._delete_modify_candidate("affected")
        # 新发布再次修改同一个 f：冲突来源被实际改变。
        self.edition("interloper", {"f": [b"second-change"]}, parent=2)

        with self.assertRaises(VaultError) as blocked:
            self.vault.merges.publish(cid)
        self.assertTrue(blocked.exception.details.get("review_required"))

        plan = self.vault.merges.plan(cid)
        self.assertEqual(plan["conflicts"][0]["status"], "pending")
        self.assertEqual(plan["review_history"][0]["state"], "stale")
        self.assertEqual(plan["review_history"][0]["decision"]["reason"], "field note")

        # reconfirm 不会把指纹已变的冲突盲目复制为已解决。
        self.vault.merges.reconfirm(cid)
        plan = self.vault.merges.plan(cid)
        self.assertEqual(plan["conflicts"][0]["status"], "pending")
        new_key = plan["conflicts"][0]["key"]
        self.vault.merges.decide(cid, {"key": new_key, "choice": "keep"})
        published = self.vault.merges.publish(cid)
        self.assertEqual(published["revision"], 4)
        self.assertEqual(self.vault.delivery.file("ridge", 4, "f").spool.read(),
                         b"second-change")
        self.assertGreaterEqual(len(self.vault.merges.plan(cid)["review_history"]), 2)

    def test_explicit_reconfirm_rebinds_unchanged_conflict(self):
        cid, _ = self._delete_modify_candidate("reconfirm")
        self.edition("interloper", {"f": [b"a-change"], "n": [b"new"]}, parent=2)
        # 严格流程下，档案员显式重申后即可绑定新上下文并发布。
        self.vault.merges.reconfirm(cid)
        plan = self.vault.merges.plan(cid)
        self.assertEqual(plan["conflicts"][0]["status"], "resolved")
        self.vault.merges.publish(cid)


class ByteCustody(CollabScenario):
    def test_base_dependencies_and_new_parts_survive_head_retire_and_other_withdrawal(self):
        self.edition("base", {"plate": [b"shared", b"orig"]})
        self.edition("adjusted", {"plate": [b"shared", b"replacement"]}, parent=1)

        first = self.open_candidate(
            "crew-1", {"plate": [b"shared", b"orig"], "extra": [b"xyz"]})
        self.fill_candidate(first["id"], {"extra": [b"xyz"]})
        second = self.open_candidate(
            "crew-2", {"plate": [b"shared", b"orig"], "extra": [b"xyz"]})

        self.clock.advance(60)
        self.vault.custody.retire("ridge", 1)  # 无 hold、保留期已过、head=2

        marked = self.vault.custody.mark(100)
        for protected in (sha(b"orig"), sha(b"xyz")):
            self.assertNotIn(protected, marked)

        # 旧版虽已退役，但候选仍依赖其历史字节：退役不等于字节消失，
        # 旧版与候选来源都应能取回。
        old_checkout = self.vault.delivery.file("ridge", 1, "plate")
        self.assertEqual(old_checkout.spool.read(), b"sharedorig")
        old_checkout.close()
        checkout = self.vault.delivery.candidate_file(first["id"], "extra")
        self.assertEqual(checkout.spool.read(), b"xyz")
        checkout.close()

        # 另一候选撤回不影响 first 的保护。
        self.vault.candidates.revoke(second["id"])
        self.assertEqual(self.vault.custody.mark(100), [])

        # first 完成合订发布后，合订版本保护所需字节；声明释放。
        published = self.vault.merges.publish(first["id"])
        self.assertEqual(published["revision"], 3)
        extra = self.vault.delivery.file("ridge", 3, "extra")
        self.assertEqual(extra.spool.read(), b"xyz")
        extra.close()
        # 候选声明释放后，已退役基础版恢复原合同的 410。
        with self.assertRaises(VaultError) as retired:
            self.vault.delivery.file("ridge", 1, "plate")
        self.assertEqual(retired.exception.status, 410)

    def test_revoking_last_candidate_releases_its_bytes(self):
        self.edition("base", {"f": [b"base"]})
        cid = self.open_candidate("crew", {"f": [b"base"], "only": [b"unique-bytes"]})["id"]
        self.fill_candidate(cid, {"only": [b"unique-bytes"]})
        self.vault.candidates.revoke(cid)
        marked = self.vault.custody.mark(100)
        self.assertIn(sha(b"unique-bytes"), marked)
        swept = self.vault.custody.sweep(100)
        self.assertIn(sha(b"unique-bytes"), [item["digest"] for item in swept["removed"]])

    def test_independent_candidate_expiry_releases_claims(self):
        self.edition("base", {"f": [b"base"]})
        cid = self.open_candidate("timed", {"f": [b"base"], "only": [b"temp"]},
                                  lifetime=60)["id"]
        self.fill_candidate(cid, {"only": [b"temp"]})
        self.clock.advance(60)
        self.assertEqual(self.vault.candidates.get(cid)["status"], "expired")
        self.assertEqual(self.vault.candidates.expire(100), [cid])
        self.assertIn(sha(b"temp"), self.vault.custody.mark(100))
        with self.assertRaises(VaultError):
            self.vault.merges.publish(cid)

    def test_operations_expire_endpoint_reports_candidates(self):
        self.edition("base", {"f": [b"base"]})
        cid = self.open_candidate("http-timed", {"f": [b"base"]}, lifetime=60)["id"]
        self.clock.advance(60)
        reply = self.http.post("/operations/expire").json()
        self.assertIn(cid, reply["candidates"])


class VerificationAndAudit(CollabScenario):
    def test_corrupt_merge_byte_blocks_publication(self):
        self.edition("base", {"f": [b"base"]})
        self.edition("a", {"f": [b"a"]}, parent=1)
        cid = self.open_candidate("b", {"g": [b"new-measure"]})["id"]
        self.fill_candidate(cid, {"g": [b"new-measure"]})
        # 候选相对 base 删除 f？这里候选只有 g，会产生 delete/modify 冲突。
        plan = self.vault.merges.plan(cid)
        if plan["conflicts"]:
            conflict = plan["conflicts"][0]
            self.vault.merges.decide(cid, {"key": conflict["key"], "choice": "head"})
        self.vault.media.path(sha(b"new-measure")).write_bytes(b"corrupt")
        with self.assertRaises(VaultError):
            self.vault.merges.publish(cid)
        self.assertEqual(self.vault.catalog.collection("ridge")["head"], 2)

    def test_audit_is_consistent_after_merge(self):
        self.edition("base", {"a": [b"AA"], "f": [b"orig"]})
        self.edition("a", {"a": [b"AA"], "f": [b"changed"], "q": [b"XXX"]}, parent=1)
        cid = self.open_candidate(
            "b", {"f": [b"orig"], "q": [b"AA"]},
            changes=[{"from": "a", "to": "q"}])["id"]
        self.decided_publish(cid, choice="candidate")
        report = self.vault.audit.check("ridge", 50)
        self.assertTrue(report["consistent"], report["issues"])

    def test_published_merge_is_idempotent_and_listed(self):
        self.edition("base", {"f": [b"base"]})
        cid = self.open_candidate("clean", {"f": [b"base"], "g": []})["id"]
        first = self.vault.merges.publish(cid)
        replay = self.vault.merges.publish(cid)
        self.assertEqual(first["revision"], replay["revision"])
        merges = self.vault.merges.list_merges("ridge")
        self.assertEqual([m["candidate"] for m in merges["items"]], [cid])
        detail = self.vault.candidates.get(cid)
        self.assertEqual(detail["retrieval"]["revision"], first["revision"])
        self.assertIn("/package", detail["retrieval"]["package"])


class HttpFlow(CollabScenario):
    def test_full_candidate_review_and_retrieval_over_http(self):
        self.edition("base", {"f": [b"orig"]})
        self.edition("a", {"f": [b"changed"]}, parent=1)

        body = {
            "token": "http-crew", "parent": 1, "lifetime_seconds": 600,
            "manifest": manifest({"q": [b"orig"]}),
            "moves": [{"from": "f", "to": "q"}],
        }
        opened = self.http.post("/collections/ridge/candidates", json=body)
        self.assertEqual(opened.status_code, 201, opened.text)
        cid = opened.json()["id"]

        plan = self.http.get(f"/candidates/{cid}/plan").json()
        self.assertTrue(plan["clean"], plan["conflicts"])
        published = self.http.post(f"/candidates/{cid}/publish-merge", json={})
        self.assertEqual(published.status_code, 200, published.text)
        number = published.json()["revision"]
        self.assertEqual(number, 3)

        fetched = self.http.get("/collections/ridge/revisions/3/file",
                                params={"path": "q"})
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, b"changed")
        package = self.http.get("/collections/ridge/revisions/3/package")
        self.assertEqual(package.status_code, 200)

        listing = self.http.get("/collections/ridge/candidates").json()
        self.assertEqual([item["id"] for item in listing["items"]], [cid])

    def test_conflict_decision_and_candidate_source_over_http(self):
        self.edition("base", {"f": [b"orig"]})
        self.edition("a", {"f": [b"changed"]}, parent=1)
        body = {"token": "http-del", "parent": 1, "lifetime_seconds": 600,
                "manifest": manifest({"g": [b"new"]})}
        cid = self.http.post("/collections/ridge/candidates", json=body).json()["id"]
        upload = self.http.put(
            f"/candidates/{cid}/parts/{sha(b'new')}", content=b"new",
            headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(upload.status_code, 200, upload.text)

        plan = self.http.get(f"/candidates/{cid}/plan").json()
        conflict = plan["conflicts"][0]
        reply = self.http.post(f"/candidates/{cid}/decisions",
                               json={"key": conflict["key"], "choice": "keep"})
        self.assertEqual(reply.status_code, 200, reply.text)
        published = self.http.post(f"/candidates/{cid}/publish-merge", json={})
        self.assertEqual(published.status_code, 200, published.text)
        number = published.json()["revision"]
        source = self.http.get(f"/candidates/{cid}/file", params={"path": "g"})
        self.assertEqual(source.content, b"new")
        kept = self.http.get(f"/collections/ridge/revisions/{number}/file",
                             params={"path": "f"})
        self.assertEqual(kept.content, b"changed")

    def test_delete_route_revokes_candidate(self):
        self.edition("base", {"f": [b"base"]})
        cid = self.http.post("/collections/ridge/candidates", json={
            "token": "doomed", "parent": 1, "lifetime_seconds": 600,
            "manifest": manifest({"f": [b"base"]}),
        }).json()["id"]
        reply = self.http.delete(f"/candidates/{cid}")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["status"], "revoked")


class FieldCandidateUplink(CollabScenario):
    def test_field_group_keeps_supplying_same_candidate(self):
        self.edition("base", {"f": [b"orig"]})
        self.edition("a", {"f": [b"changed"]}, parent=1)
        source = self.root / "field"
        source.mkdir()
        (source / "moved.dat").write_bytes(b"orig")
        parcel = build_parcel(source, self.root / "parcel-c", collection="ridge",
                              token="crew-c", parent=1, lifetime=600, part_size=4)
        endpoint = _Endpoint(self.http)
        uplink = Uplink(endpoint)
        accepted = uplink.send_candidate(
            parcel, changes=[{"op": "move", "from": "f", "to": "moved.dat"}])
        self.assertEqual(accepted.status, "open")
        self.assertIsNone(accepted.revision)
        # 同一个包再次补送：回到同一候选且不再传片。
        replay = uplink.send_candidate(
            parcel, changes=[{"op": "move", "from": "f", "to": "moved.dat"}])
        self.assertEqual(replay.candidate, accepted.candidate)
        self.assertEqual(replay.transmitted, 0)
        plan = self.vault.merges.plan(accepted.candidate)
        self.assertTrue(plan["clean"], plan["conflicts"])
        published = self.vault.merges.publish(accepted.candidate)
        self.assertEqual(published["revision"], 3)


if __name__ == "__main__":
    unittest.main()
