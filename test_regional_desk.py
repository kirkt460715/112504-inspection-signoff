"""区域联动台业务规则与 HTTP 接口的验收测试。

这些测试把题目中的关键情景固定下来：
* 签署时刻的资格（到期/停用）与已报回避；
* 事后补报回避的原件保留、指定对象复核与处置单“需复议”；
* 替换后只能领未开始且资质覆盖的事项、中途交接与记录署名；
* 替补不得同时持有未结主检与同对象独立签字；
* 采证对端断连提交的恢复、证据不改变归属/不补齐复核；
* 新委托不得复开旧范围；持久化后下一次任务可继续。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection, RemoteDisconnected
from pathlib import Path
from tempfile import TemporaryDirectory

from regional_desk import (
    Desk, DeskError, create_server, parse_ts, credential_decision,
)
from partner_stub import PartnerServer

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
REGISTERS = json.loads((FIXTURES / "registers.json").read_text(encoding="utf-8"))
EXCHANGES = FIXTURES / "partner-exchanges.json"

T0 = "2028-07-03T09:00:00+08:00"


def seeded_desk() -> Desk:
    desk = Desk(":memory:")
    desk.import_registers(REGISTERS)
    return desk


class FixtureLedgerTest(unittest.TestCase):
    """registers.json 给出的合成台账故事线。"""

    def setUp(self):
        self.desk = seeded_desk()

    def test_fixture_seeds_core_entities(self):
        self.assertEqual(self.desk.get("mandate", "mandate-2")["issuer"], "district-p")
        self.assertEqual({w["id"] for w in self.desk.list_workers(district="district-q")},
                         {"staff-1", "staff-2"})
        self.assertEqual(self.desk.get("case", "review-2")["status"], "needs_review")

    def test_credential_judged_at_signing_time(self):
        staff1 = self.desk.get("worker", "staff-1")
        self.assertTrue(credential_decision(
            staff1, "structure", parse_ts("2028-07-03T11:20:00+08:00"))["covered"])
        # 12:30 到期；12:45 不再有效
        self.assertFalse(credential_decision(
            staff1, "structure", parse_ts("2028-07-03T12:45:00+08:00"))["covered"])

    def test_late_recusal_taints_original_signature_and_flags_disposition(self):
        sig = self.desk.get("signature", "sig-review-2-staff-1-lead")
        self.assertEqual(sig["status"], "affected")  # 原件仍在，只是被标记
        self.assertTrue(sig.get("affects"))
        action = self.desk.get("disposition", "action-4")
        self.assertEqual(action["status"], "reconsideration_required")
        self.assertIn("reconsideration", action)

    def test_recipient_can_see_what_is_missing_for_object(self):
        readiness = self.desk.object_readiness({"object_id": "culvert-4"})
        self.assertFalse(readiness["ready"])
        self.assertIn("qualified_lead_signature", readiness["missing"])
        self.assertEqual(
            readiness["requirements"]["independent_review"]["state"], "valid")
        self.assertEqual(
            readiness["requirements"]["independent_review"]["worker"], "staff-2")
        self.assertTrue(readiness["open_reviews"])

    def test_fixture_substitute_did_not_hold_unfinished_lead(self):
        # 主检 11:20 已签，12:15 才列 staff-2 为替补：不构成“承接未结主检”，
        # 因此 staff-2 11:45 的独立签字仍然有效。
        readiness = self.desk.object_readiness({"object_id": "culvert-4"})
        review = readiness["requirements"]["independent_review"]
        self.assertEqual(review["state"], "valid")
        self.assertNotIn("reviewer_is_substitute_lead",
                         readiness["independence_problems"])

    def test_donor_sees_dispatch_and_handover_for_own_district(self):
        view = self.desk.donor_view("district-q")
        ids = {row["worker"]["id"] for row in view["workers"]}
        self.assertEqual(ids, {"staff-1", "staff-2"})
        self.assertTrue(view["handovers"])  # 台账中的替换记录可见

    def test_repeated_import_remains_idempotent(self):
        self.desk.import_registers(REGISTERS)
        self.desk.import_registers(REGISTERS)
        readiness = self.desk.object_readiness({"object_id": "culvert-4"})
        self.assertFalse(readiness["ready"])
        sig = self.desk.get("signature", "sig-review-2-staff-1-lead")
        self.assertEqual(sig["status"], "affected")
        self.assertEqual(len(sig.get("affects", [])), 1)
        self.assertEqual(len(self.desk._list("recusal")), 1)
        self.assertEqual(len(self.desk._list("review")), 1)

    def test_unit_based_recusal_matches_signature(self):
        desk = Desk(":memory:")
        desk.upsert_worker({"id": "w", "credential": "structure", "roles": ["lead"]})
        desk.sign({"case_id": "k", "site": "site-x", "unit": "contractor-abc",
                   "worker": "w", "role": "lead",
                   "signed_at": "2028-07-03T10:00:00+08:00"})
        impact = desk.report_recusal({
            "worker": "w", "unit": "contractor-abc",
            "relationship_since": "2028-01-01",
            "reported_at": "2028-07-05T09:00:00+08:00"})["impact"]
        self.assertEqual(len(impact["affected_signatures"]), 1)

    def test_persistence_across_reopen(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "desk.sqlite3"
            desk = Desk(path)
            desk.import_registers(REGISTERS)
            desk.close()
            again = Desk(path)
            try:
                self.assertEqual(len(again.list_mandates()), 1)
                self.assertEqual(
                    again.get("disposition", "action-4")["status"],
                    "reconsideration_required")
            finally:
                again.close()


class QualificationAndRecusalRulesTest(unittest.TestCase):
    def setUp(self):
        self.desk = Desk(":memory:")
        self.desk.upsert_worker({"id": "lead-a", "district": "q",
                                 "credential": "structure", "roles": ["lead"],
                                 "valid_through": "2028-07-03T12:30:00+08:00"})
        self.desk.upsert_worker({"id": "rev-b", "district": "p",
                                 "credential": "structure", "roles": ["review"],
                                 "valid_through": "2028-07-05T16:00:00+08:00"})

    def test_expired_credential_blocks_late_signature_but_not_earlier_one(self):
        ok = self.desk.sign({"case_id": "c1", "site": "s", "worker": "lead-a",
                             "role": "lead",
                             "signed_at": "2028-07-03T11:00:00+08:00"})
        self.assertEqual(ok["signature"]["status"], "valid")
        with self.assertRaises(DeskError) as ctx:
            self.desk.sign({"case_id": "c2", "site": "s", "worker": "lead-a",
                            "role": "lead",
                            "signed_at": "2028-07-03T13:00:00+08:00"})
        self.assertIn("credential_outside_validity", ctx.exception.detail["reasons"])

    def test_temporary_suspension_blocks_signing_during_window(self):
        self.desk.suspend_worker({"worker_id": "rev-b",
                                  "from": "2028-07-03T12:00:00+08:00",
                                  "until": "2028-07-03T18:00:00+08:00"})
        with self.assertRaises(DeskError):
            self.desk.sign({"case_id": "c", "site": "s2", "worker": "rev-b",
                            "role": "review",
                            "signed_at": "2028-07-03T14:00:00+08:00"})
        # 停用前的签字不受影响
        early = self.desk.sign({"case_id": "c0", "site": "s2", "worker": "rev-b",
                                "role": "review",
                                "signed_at": "2028-07-03T10:00:00+08:00"})
        self.assertEqual(early["signature"]["status"], "valid")

    def test_recusal_reported_before_signing_blocks_signature(self):
        self.desk.report_recusal({"worker": "lead-a", "site": "s",
                                  "relationship_since": "2028-06-01",
                                  "reported_at": "2028-07-02T09:00:00+08:00"})
        with self.assertRaises(DeskError) as ctx:
            self.desk.sign({"case_id": "c", "site": "s", "worker": "lead-a",
                            "role": "lead",
                            "signed_at": "2028-07-03T10:00:00+08:00"})
        self.assertIn("recusal_on_record_at_signing",
                      ctx.exception.detail["reasons"])

    def test_late_recusal_with_non_overlapping_relationship_does_not_taint(self):
        self.desk.sign({"case_id": "future-rel", "site": "s", "worker": "lead-a",
                        "role": "lead",
                        "signed_at": "2028-07-03T10:00:00+08:00"})
        impact = self.desk.report_recusal({
            "worker": "lead-a", "site": "s",
            "relationship_since": "2028-09-01",
            "reported_at": "2028-09-10T09:00:00+08:00"})["impact"]
        self.assertEqual(impact["affected_signatures"], [])
        readiness = self.desk.object_readiness({"object_id": "s"})
        self.assertEqual(readiness["requirements"]["lead"]["state"], "valid")

    def test_late_recusal_is_idempotent_on_same_id(self):
        payload = {"id": "rec-x", "worker": "lead-a", "site": "s",
                   "relationship_since": "2028-06-01",
                   "reported_at": "2028-07-04T09:00:00+08:00"}
        first = self.desk.report_recusal(payload)
        second = self.desk.report_recusal(payload)
        self.assertFalse(first.get("deduplicated"))
        self.assertTrue(second.get("deduplicated"))
        self.assertEqual(len(self.desk._list("recusal")), 1)


class ClaimReplacementHandoverTest(unittest.TestCase):
    def setUp(self):
        self.desk = Desk(":memory:")
        self.desk.upsert_worker({"id": "w1", "district": "q",
                                 "credential": "structure", "roles": ["lead"]})
        self.desk.upsert_worker({"id": "w2", "district": "q",
                                 "credential": "electrical", "roles": ["lead"]})
        self.desk.upsert_worker({"id": "w3", "district": "q",
                                 "credential": "structure",
                                 "roles": ["lead", "review"]})
        self.desk.create_mandate({"id": "m", "issuer": "p", "donor": "q",
                                  "worksites": ["site-a"], "scopes": ["structure"],
                                  "from": "2028-07-03T08:00:00+08:00",
                                  "to": "2028-07-03T18:00:00+08:00"})
        self.desk.create_check({"id": "task-open", "mandate_id": "m",
                                "site": "site-a", "category": "structure",
                                "kind": "lead"})

    def test_qualified_worker_can_claim_unstarted(self):
        check = self.desk.claim_check({"check_id": "task-open", "worker_id": "w1",
                                       "at": T0})
        self.assertEqual(check["status"], "in_progress")
        self.assertEqual(check["assignee"], "w1")

    def test_unqualified_replacement_cannot_claim(self):
        with self.assertRaises(DeskError) as ctx:
            self.desk.claim_check({"check_id": "task-open", "worker_id": "w2",
                                   "at": T0})
        self.assertEqual(ctx.exception.code, "credential_not_covered")

    def test_newcomer_cannot_claim_already_started_check(self):
        self.desk.claim_check({"check_id": "task-open", "worker_id": "w1", "at": T0})
        with self.assertRaises(DeskError):
            self.desk.claim_check({"check_id": "task-open", "worker_id": "w3",
                                   "at": "2028-07-03T10:00:00+08:00"})

    def test_workbench_lists_claimable_and_submittable(self):
        bench = self.desk.worker_workbench({"worker_id": "w1", "at": T0})
        self.assertEqual([c["id"] for c in bench["claimable"]], ["task-open"])
        self.desk.claim_check({"check_id": "task-open", "worker_id": "w1", "at": T0})
        bench = self.desk.worker_workbench({"worker_id": "w1", "at": T0})
        self.assertEqual([c["id"] for c in bench["submittable"]], ["task-open"])

    def test_handover_keeps_original_record_attribution(self):
        self.desk.claim_check({"check_id": "task-open", "worker_id": "w1", "at": T0})
        self.desk.submit_to_check({"check_id": "task-open", "worker_id": "w1",
                                   "content": "原始巡检记录", "at": "2028-07-03T09:20:00+08:00"})
        result = self.desk.handover({"check_id": "task-open", "to_worker": "w3",
                                     "at": "2028-07-03T11:00:00+08:00"})
        self.assertTrue(result["handover"]["assumed_unfinished_lead"])
        check = self.desk.get("check", "task-open")
        self.assertEqual(check["assignee"], "w3")
        self.assertEqual(check["records"][0]["by"], "w1")  # 原署名不被改写

    def test_duty_replacement_does_not_reassign_started_work(self):
        self.desk.claim_check({"check_id": "task-open", "worker_id": "w1", "at": T0})
        rec = self.desk.replace_duty({"mandate_id": "m", "site": "site-a",
                                       "from_worker": "w1", "to_worker": "w3",
                                       "at": "2028-07-03T12:00:00+08:00"})
        self.assertEqual(rec["type"], "duty_replacement")
        check = self.desk.get("check", "task-open")
        self.assertEqual(check["assignee"], "w1")  # 没有被自动划走


class SubstituteIndependenceTest(unittest.TestCase):
    def setUp(self):
        self.desk = Desk(":memory:")
        for wid, roles in (("lead1", ["lead"]), ("multi", ["lead", "review"]),
                           ("rev3", ["review"])):
            self.desk.upsert_worker({"id": wid, "district": "q",
                                      "credential": "structure", "roles": roles})
        self.desk.create_check({"id": "lead-task", "site": "obj",
                                "category": "structure", "kind": "lead"})

    def test_cannot_review_after_taking_unfinished_lead(self):
        self.desk.claim_check({"check_id": "lead-task", "worker_id": "lead1",
                               "at": T0})
        self.desk.handover({"check_id": "lead-task", "to_worker": "multi", "at": T0})
        with self.assertRaises(DeskError) as ctx:
            self.desk.sign({"case_id": "case-x", "site": "obj", "worker": "multi",
                            "role": "review", "signed_at": "2028-07-03T11:00+08:00"})
        self.assertIn("substitute_dual_role", ctx.exception.detail["reasons"])

    def test_cannot_take_unfinished_lead_after_valid_review_signature(self):
        self.desk.sign({"case_id": "case-y", "site": "obj", "worker": "multi",
                        "role": "review", "signed_at": "2028-07-03T10:00+08:00"})
        self.desk.claim_check({"check_id": "lead-task", "worker_id": "lead1",
                               "at": T0})
        with self.assertRaises(DeskError) as ctx:
            self.desk.handover({"check_id": "lead-task", "to_worker": "multi",
                                "at": "2028-07-03T11:00+08:00"})
        self.assertEqual(ctx.exception.code, "substitute_dual_role")

    def test_completed_lead_does_not_create_dual_role_conflict(self):
        # 主检已签完，再被列为替补不构成“承接未结主检”
        self.desk.sign({"case_id": "case-z", "site": "obj2", "worker": "lead1",
                        "role": "lead", "signed_at": "2028-07-03T09:30+08:00"})
        ho = self.desk.replace_duty({"site": "obj2", "from_worker": "lead1",
                                     "to_worker": "multi", "at": "2028-07-03T12:15+08:00"})
        self.assertFalse(ho["assumed_unfinished_lead"])
        review = self.desk.sign({"case_id": "case-z", "site": "obj2",
                                 "worker": "multi", "role": "review",
                                 "signed_at": "2028-07-03T11:45+08:00"})
        self.assertEqual(review["signature"]["status"], "valid")


class EvidencePartnerIntegrationTest(unittest.TestCase):
    """与 partner_stub 的本地采证联调，含 commit_disconnect 恢复。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = PartnerServer(
            ("127.0.0.1", 0), EXCHANGES, Path(self.tmp.name) / "journal.sqlite3")
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.desk = Desk(":memory:",
                         partner_url=f"http://127.0.0.1:{self.server.server_port}")

    def test_disconnect_example_is_recovered_with_actual_capture_time(self):
        result = self.desk.fetch_partner_evidence({
            "site": "culvert-4", "observer": "staff-1",
            "submission": "check-31"})
        self.assertEqual(result["reply"]["signature_receipt"], "proof-31")
        ev = result["evidence"]
        self.assertEqual(ev["captured_at"], "2028-07-03T09:10:00+08:00")
        self.assertEqual(ev["signature_receipt"], "proof-31")
        # 同一业务提交重放，仍拿到同一回报
        again = self.desk.fetch_partner_evidence({
            "operation_id": "ev-check-31",
            "site": "culvert-4", "observer": "staff-1",
            "submission": "check-31"})
        self.assertEqual(again["reply"]["signature_receipt"], "proof-31")

    def test_success_example_ingests_receipt(self):
        result = self.desk.fetch_partner_evidence({
            "site": "culvert-4", "observer": "staff-3",
            "submission": "check-32"})
        self.assertEqual(result["evidence"]["signature_receipt"], "proof-32")

    def test_evidence_does_not_assign_work_or_complete_review(self):
        self.desk.import_registers(REGISTERS)
        before = self.desk.object_readiness({"object_id": "culvert-4"})
        self.assertFalse(before["ready"])
        self.desk.fetch_partner_evidence({
            "site": "culvert-4", "observer": "staff-3",
            "submission": "check-32"})
        after = self.desk.object_readiness({"object_id": "culvert-4"})
        self.assertFalse(after["ready"])  # 证据不补主检签名
        self.assertTrue(any(r["observer"] == "staff-3" and r["usable"]
                            for r in after["usable_records"]))
        # staff-1 的 check-31 与回避人员绑定，不算可继续使用的独立记录
        self.desk.fetch_partner_evidence({
            "site": "culvert-4", "observer": "staff-1",
            "submission": "check-31"})
        report = self.desk.object_readiness({"object_id": "culvert-4"})
        usability = {e["observer"]: e["usable"] for e in report["evidence"]}
        self.assertFalse(usability.get("staff-1", True))
        self.assertTrue(usability["staff-3"])


class MandateScopeAndDispositionTest(unittest.TestCase):
    def setUp(self):
        self.desk = Desk(":memory:")
        self.desk.create_mandate({
            "id": "m-old", "issuer": "p", "donor": "q",
            "worksites": ["old-site"], "scopes": ["structure"],
            "allowed_actions": ["rectify"],
            "from": "2028-07-03T08:00:00+08:00",
            "to": "2028-07-03T16:00:00+08:00"})
        self.desk.create_check({"id": "old-task", "mandate_id": "m-old",
                                "site": "old-site", "category": "structure"})
        self.desk._put("check", {**self.desk.get("check", "old-task"),
                                 "status": "done"})

    def test_cannot_reopen_old_scope_under_expired_mandate(self):
        with self.assertRaises(DeskError) as ctx:
            self.desk.reopen_check({"check_id": "old-task",
                                     "at": "2028-07-05T09:00:00+08:00"})
        self.assertEqual(ctx.exception.code, "old_mandate_closed")

    def test_new_commission_does_not_authorize_old_scope(self):
        self.desk.create_mandate({
            "id": "m-new", "issuer": "p", "donor": "q",
            "worksites": ["new-site"], "scopes": ["structure"],
            "from": "2028-07-04T08:00:00+08:00",
            "to": "2028-07-04T18:00:00+08:00"})
        # 新委托不覆盖 old-site
        with self.assertRaises(DeskError):
            self.desk.create_check({"id": "x", "mandate_id": "m-new",
                                    "site": "old-site", "category": "structure"})

    def test_superseded_mandate_cannot_reopen(self):
        self.desk.create_mandate({
            "id": "m-two", "replaces": "m-old", "issuer": "p", "donor": "q",
            "worksites": ["old-site", "new-site"], "scopes": ["structure"],
            "from": "2028-07-03T12:00:00+08:00",
            "to": "2028-07-03T20:00:00+08:00"})
        with self.assertRaises(DeskError) as ctx:
            self.desk.reopen_check({"check_id": "old-task",
                                     "at": "2028-07-03T15:00:00+08:00"})
        self.assertIn("mandate_superseded", ctx.exception.detail["reasons"])

    def test_new_commission_blocks_reopen_even_if_old_window_open(self):
        self.desk.upsert_worker({"id": "roving", "district": "q",
                                 "credential": "structure", "roles": ["lead"]})
        self.desk.create_mandate({
            "id": "m-later", "issuer": "p", "donor": "q",
            "worksites": ["other-site"], "scopes": ["structure"],
            "from": "2028-07-10T08:00:00+08:00",
            "to": "2028-07-10T18:00:00+08:00"})
        with self.assertRaises(DeskError) as ctx:
            self.desk.reopen_check({"check_id": "old-task", "worker_id": "roving",
                                     "at": "2028-07-03T15:00:00+08:00"})
        self.assertEqual(ctx.exception.code,
                         "old_mandate_reopen_after_new_commission")

    def test_disposition_must_stay_within_allowed_actions(self):
        self.desk.create_case({"id": "case-ok", "site": "old-site"})
        ok = self.desk.create_disposition({"id": "d1", "site": "old-site",
                                            "signed_case": "case-ok",
                                            "action_type": "rectify"})
        self.assertEqual(ok["status"], "effective")
        with self.assertRaises(DeskError) as ctx:
            self.desk.create_disposition({"id": "d2", "site": "old-site",
                                           "signed_case": "case-ok",
                                           "action_type": "demolish"})
        self.assertEqual(ctx.exception.code, "disposition_not_allowed")


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desk = seeded_desk()
        cls.server = create_server("127.0.0.1", 0, desk=cls.desk)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.desk.close()
        cls.thread.join(2)

    def call(self, method, path, data=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path,
                         json.dumps(data) if data is not None else None,
                         {"Content-Type": "application/json", **(headers or {})})
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, json.loads(raw.decode()) if raw else {}
        finally:
            conn.close()

    def test_health_and_ui(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", "/ui")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.getheader("Content-Type"))
            resp.read()
        finally:
            conn.close()

    def test_object_readiness_route(self):
        status, body = self.call("GET", "/objects/culvert-4/readiness")
        self.assertEqual(status, 200)
        self.assertFalse(body["ready"])
        self.assertIn("qualified_lead_signature", body["missing"])

    def test_donor_and_recipient_views(self):
        status, body = self.call("GET", "/donors/district-q")
        self.assertEqual(status, 200)
        self.assertTrue(body["workers"])
        status, body = self.call("GET", "/recipient/district-p")
        self.assertEqual(status, 200)
        self.assertTrue(any(o["object_id"] == "culvert-4" for o in body["objects"]))

    def test_worker_workbench_route_with_as_of_header(self):
        status, body = self.call("GET", "/workers/staff-2/workbench",
                                 headers={"X-As-Of": "2028-07-03T10:00:00+08:00"})
        self.assertEqual(status, 200)
        self.assertIn("claimable", body)

    def test_rpc_style_action_endpoint(self):
        status, body = self.call("POST", "/api/object_readiness",
                                 {"object_id": "culvert-4"})
        self.assertEqual(status, 200)
        self.assertIn("missing", body)
        status, body = self.call("GET",
                                 "/api/object_readiness?object_id=culvert-4")
        self.assertEqual(status, 200)
        self.assertIn("missing", body)
        status, body = self.call("POST", "/rpc",
                                 {"action": "donor_view", "district": "district-q"})
        self.assertEqual(status, 200)
        self.assertIn("workers", body)

    def test_end_to_end_claim_submit_sign_over_http(self):
        self.call("POST", "/workers", {"id": "http-w", "district": "q",
                                       "credential": "structure",
                                       "roles": ["lead", "review"]})
        self.call("POST", "/checks", {"id": "http-task", "site": "bridge-9",
                                      "category": "structure", "kind": "lead"})
        status, body = self.call("POST", "/checks/http-task/claim",
                                 {"worker_id": "http-w", "at": T0})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["assignee"], "http-w")
        status, body = self.call("POST", "/checks/http-task/submit",
                                 {"worker_id": "http-w", "content": "ok",
                                  "at": "2028-07-03T09:30:00+08:00",
                                  "complete": True})
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/signatures",
                                 {"case_id": "http-case", "site": "bridge-9",
                                  "worker": "http-w", "role": "lead",
                                  "signed_at": "2028-07-03T09:40:00+08:00"})
        self.assertEqual(status, 201)
        self.assertEqual(body["signature"]["status"], "valid")

    def test_error_returns_json_status(self):
        status, body = self.call("POST", "/checks/no-such-task/claim",
                                 {"worker_id": "staff-2"})
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])

    def test_envelope_unwrap_and_api_prefix(self):
        status, body = self.call("POST", "/api/v1/workers",
                                 {"worker": {"id": "env-w", "district": "q",
                                              "credential": "c", "roles": ["lead"]}})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["id"], "env-w")
        status, body = self.call("POST", "/signatures",
                                 {"signature": {"case_id": "env-case", "site": "e",
                                                "worker": "env-w", "role": "lead",
                                                "category": "c",
                                                "signed_at": T0}})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["signature"]["status"], "valid")

    def test_case_readiness_route(self):
        status, body = self.call("GET", "/cases/review-2/readiness")
        self.assertEqual(status, 200)
        self.assertEqual(body["object_id"], "culvert-4")


class ServerFactoryTest(unittest.TestCase):
    def test_create_server_accepts_in_memory_desk(self):
        server = create_server("127.0.0.1", 0, db_path=":memory:")
        try:
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()
            server.desk.close()


if __name__ == "__main__":
    unittest.main()
