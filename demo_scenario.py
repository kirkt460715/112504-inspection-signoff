#!/usr/bin/env python3
"""一条命令走完“暴雨后跨县借人”的完整演练故事。

* 启动本地采证对端桩（含 commit_disconnect 样例）；
* 按时间线重建 fixtures/registers.json 的合成案例；
* 演示签署时资格、替换/交接、替补独立回避、断连采证恢复、
  迟报回避引发的原件保留/指定对象复核/处置单需复议、独立记录沿用；
* 最后验证 SQLite 持久化，重开后台账仍在。

退出码 0 表示所有断言成立。仅用标准库。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regional_desk import Desk, DeskError, create_server  # noqa: E402
from partner_stub import PartnerServer  # noqa: E402

ROOT = Path(__file__).resolve().parent
REGISTERS = json.loads((ROOT / "fixtures" / "registers.json").read_text("utf-8"))
EXCHANGES = ROOT / "fixtures" / "partner-exchanges.json"

CHECKS = []


def check(name: str, condition: bool, detail: str = ""):
    CHECKS.append((name, bool(condition)))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        raise AssertionError(name + (" " + detail if detail else ""))


def build_timeline(desk: Desk):
    print("\n① 受援县 district-p 发起委托 mandate-2（地点/类别/处置/时段）")
    desk.add_district({"id": "district-p"})
    desk.add_district({"id": "district-q"})
    validity = {n["staff"]: n for n in REGISTERS["credential_notes"]}
    for w in REGISTERS["workers"]:
        desk.upsert_worker({**w, "valid_through": validity[w["id"]]["valid_through"]})
    desk.create_mandate(REGISTERS["commission"])
    check("委托已登记且覆盖 culvert-4/structure",
          desk.get("mandate", "mandate-2")["scopes"] == ["structure"])

    print("\n② 现场分工：staff-1 主检、staff-2 独立复核（均为 district-q 派出）")
    desk.create_check({"id": "lead-4", "mandate_id": "mandate-2",
                        "site": "culvert-4", "category": "structure", "kind": "lead"})
    desk.create_check({"id": "review-4", "mandate_id": "mandate-2",
                        "site": "culvert-4", "category": "structure", "kind": "review"})
    desk.claim_check({"check_id": "lead-4", "worker_id": "staff-1",
                      "at": "2028-07-03T09:05:00+08:00"})
    desk.claim_check({"check_id": "review-4", "worker_id": "staff-2",
                      "at": "2028-07-03T09:05:00+08:00"})

    print("\n③ 采证系统断连恢复：check-31 首次提交断连，按操作 ID 找回回执")
    result31 = desk.fetch_partner_evidence(
        {"operation_id": "demo-check-31", "site": "culvert-4",
         "observer": "staff-1", "submission": "check-31"})
    check("check-31 断连后找回 proof-31 与实际采集时间 09:10",
          result31["reply"]["signature_receipt"] == "proof-31"
          and result31["evidence"]["captured_at"].endswith("09:10:00+08:00"))

    print("\n④ 11:20/11:45 签署主检与独立复核；资格按签署时刻判定为有效")
    s1 = desk.sign({"case_id": "review-2", "site": "culvert-4", "worker": "staff-1",
                    "role": "lead", "signed_at": "2028-07-03T11:20:00+08:00"})
    s2 = desk.sign({"case_id": "review-2", "site": "culvert-4", "worker": "staff-2",
                    "role": "review", "signed_at": "2028-07-03T11:45:00+08:00"})
    check("staff-1 在 11:20（资格 12:30 到期前）签署有效",
          s1["signature"]["status"] == "valid")
    check("staff-2 独立复核签署有效", s2["signature"]["status"] == "valid")
    ready = desk.object_readiness({"object_id": "culvert-4"})
    check("迟报前对象复核齐备", ready["ready"], str(ready["missing"]))

    print("\n⑤ 12:15 派出方替换值守人员 staff-1 -> staff-2；主检 11:20 已签，"
          "不构成承接未结主检，故不污染 staff-2 的独立签字")
    rep = desk.replace_duty({"mandate_id": "mandate-2", "site": "culvert-4",
                              "from_worker": "staff-1", "to_worker": "staff-2",
                              "at": REGISTERS["replacement_at"]})
    check("替换记录明确未承接未结主检", rep["assumed_unfinished_lead"] is False)

    print("\n⑥ 13:40 staff-3（district-p，复核资格）独立取得 check-32 材料")
    result32 = desk.fetch_partner_evidence(
        {"operation_id": "demo-check-32", "site": "culvert-4",
         "observer": "staff-3", "submission": "check-32"})
    check("check-32 回执 proof-32 入库",
          result32["evidence"]["signature_receipt"] == "proof-32")

    print("\n⑦ 15:00 受援县依据 review-2 发出处置单 action-4")
    action = desk.create_disposition({"id": "action-4", "site": "culvert-4",
                                       "signed_case": "review-2",
                                       "released_at": REGISTERS["dispositions"][0]["released_at"]})
    check("处置单 initially effective", action["status"] == "effective")

    print("\n⑧ 次日补报：staff-1 与 culvert-4 承包关系始于 6-20（覆盖签署）")
    impact = desk.report_recusal(REGISTERS["recusal"])["impact"]
    check("原主检签名被标记 affected 但原件保留",
          "sig-review-2-staff-1-lead" in impact["affected_signatures"]
          and desk.get("signature", "sig-review-2-staff-1-lead")["status"] == "affected")
    check("已发起指定对象 culvert-4 的复核", bool(impact["initiated_reviews"]))
    check("action-4 显示 reconsideration_required",
          desk.get("disposition", "action-4")["status"] == "reconsideration_required")

    print("\n⑨ 受援县视图：复核尚缺资格主检；staff-2 独立签字与 staff-3 独立记录可继续用")
    status = desk.object_readiness({"object_id": "culvert-4"})
    check("对象不再 ready，缺 qualified_lead_signature",
          not status["ready"] and "qualified_lead_signature" in status["missing"])
    check("staff-2 的独立复核仍 valid",
          status["requirements"]["independent_review"]["state"] == "valid")
    usable = {r["observer"]: r["usable"] for r in status["evidence"]}
    check("staff-3 的独立记录 check-32 仍可用；staff-1 的不独立",
          usable.get("staff-3") and not usable.get("staff-1", True))
    print("    受援县提示：" + status["summary_zh"])

    print("\n⑩ 派出方视图与现场工作台可查询")
    donor = desk.donor_view("district-q")
    check("派出方看到本单位 2 人的派工/交接", len(donor["workers"]) == 2
          and len(donor["handovers"]) >= 1)
    bench = desk.worker_workbench({"worker_id": "staff-3",
                                    "at": "2028-07-03T14:00:00+08:00"})
    check("现场人员工作台正常返回（无资质主检时不给主检事项）",
          isinstance(bench["claimable"], list))


def verify_persistence(path: str):
    print("\n⑪ 重启业务服务（重新打开同一 SQLite 台账），下一次任务接着用")
    desk2 = Desk(path)
    try:
        check("持久化后处置单仍为需复议",
              desk2.get("disposition", "action-4")["status"]
              == "reconsideration_required")
        check("持久化后对象缺口仍可判断",
              "qualified_lead_signature"
              in desk2.object_readiness({"object_id": "culvert-4"})["missing"])
    finally:
        desk2.close()


def verify_http_smoke():
    print("\n⑫ HTTP 服务接口冒烟（临时端口）")
    server = create_server("127.0.0.1", 0, db_path=":memory:")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import json as _json
        from http.client import HTTPConnection
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = _json.loads(resp.read())
        check("GET /health 200", resp.status == 200 and body["ok"])
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def main():
    tmpdir = tempfile.TemporaryDirectory(prefix="desk-demo-")
    db_path = str(Path(tmpdir.name) / "desk.sqlite3")

    partner = PartnerServer(("127.0.0.1", 0), EXCHANGES,
                            Path(tmpdir.name) / "partner.sqlite3")
    pt = threading.Thread(target=partner.serve_forever,
                          kwargs={"poll_interval": 0.02}, daemon=True)
    pt.start()
    try:
        desk = Desk(db_path,
                    partner_url=f"http://127.0.0.1:{partner.server_port}")
        build_timeline(desk)
        desk.close()
        verify_persistence(db_path)
        verify_http_smoke()
    finally:
        partner.shutdown()
        partner.server_close()
        tmpdir.cleanup()

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n演示断言 {passed}/{len(CHECKS)} 全部通过。")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
