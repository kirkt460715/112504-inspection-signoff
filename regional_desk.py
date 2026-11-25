"""区域联动台 —— 跨县借人、现场分工、资格/回避判定与结论签认的本地服务端。

仅使用 Python 3.11 标准库。设计要点：

* 所有业务判断都可按事件自带时间（``at`` / ``signed_at`` / ``reported_at`` ...）
  重放，不依赖宿主机时钟，因此台账里 2028 年的合成案例可以直接演练。
* 资格是否覆盖以“行为发生时刻”为准：到期、临时停用都按时间区间判定。
* 签名能否用于结论，按签署时刻的有效资格与当时已经报来的回避证据判断；
  事后补报且关系区间覆盖签署/检查时间的回避，不销毁原件，而是把原签名标记为
  affected、发起指定对象的复核，并把关联处置单置为 reconsideration_required。
* 采证材料（含对端回执）只是证据，不改变任务归属，也不自动凑齐独立复核。
* SQLite 单文件持久化，重启后下一次任务可以接着用。

HTTP 层见 :class:`DeskRequestHandler` / :func:`create_server`。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_cst() -> datetime:
    return datetime.now(tz=CST)


def parse_ts(value, default=None):
    """宽容地解析 ISO8601；纯日期按 +08:00 零点处理。"""
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=CST)
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                dt = datetime.fromisoformat(text).replace(tzinfo=CST)
            else:
                raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(CST).isoformat(timespec="seconds")


def first_key(data: dict, *keys, default=None):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DeskError(Exception):
    http_status = 400
    code = "desk_error"

    def __init__(self, message: str | None = None, *, code: str | None = None,
                 status: int | None = None, detail: dict | None = None):
        super().__init__(message or self.code)
        if code:
            self.code = code
        if status:
            self.http_status = status
        self.message = message or self.code
        self.detail = detail or {}

    def to_body(self) -> dict:
        body = {"ok": False, "error": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


class NotFound(DeskError):
    http_status = 404
    code = "not_found"


class Conflict(DeskError):
    http_status = 409
    code = "conflict"


class ValidationFailed(DeskError):
    http_status = 422
    code = "validation_failed"


class Forbidden(DeskError):
    http_status = 403
    code = "forbidden"


class PartnerUnavailable(DeskError):
    http_status = 502
    code = "partner_unavailable"


# ---------------------------------------------------------------------------
# 资格 / 回避判定（纯函数，便于单测与重放）
# ---------------------------------------------------------------------------


def _interval_bounds(item: dict) -> tuple[datetime | None, datetime | None]:
    start = parse_ts(first_key(item, "from", "start", "since", "suspended_from",
                               "valid_from", "begins_at", "start_at"))
    end = parse_ts(first_key(item, "until", "to", "end", "suspended_until",
                             "suspended_through", "resume_at", "valid_to",
                             "valid_through", "expires_at", "finish_at"))
    return start, end


def interval_contains(start: datetime | None, end: datetime | None,
                      at: datetime, *, end_inclusive: bool = True) -> bool:
    if start is not None and at < start:
        return False
    if end is not None:
        return at <= end if end_inclusive else at < end
    return True


def credential_decision(worker: dict, category: str, at: datetime) -> dict:
    """返回 worker 在 ``at`` 时刻是否持有覆盖 ``category`` 的有效资质。"""
    reasons: list[str] = []

    # 资质既可能是字符串列表，也可能是带有效期的对象列表
    raw_credentials = as_list(first_key(worker, "credentials", "credential",
                                        default=[]))
    credential_names: set[str] = set()
    embedded_entries: list[dict] = []
    for item in raw_credentials:
        if isinstance(item, str):
            credential_names.add(item)
        elif isinstance(item, dict):
            name = item.get("credential") or item.get("type") \
                or item.get("category") or item.get("name") or item.get("code")
            if name:
                credential_names.add(name)
            embedded_entries.append(item)

    quals = worker.get("qualifications")
    if isinstance(quals, dict):
        quals = [dict(v, credential=k) if isinstance(v, dict) else {"credential": k}
                 for k, v in quals.items()]
    quals = as_list(quals) + embedded_entries

    entries = [q for q in quals if isinstance(q, dict)
               and q.get("credential") in (category, "*")]
    if not entries and category in credential_names:
        entries = [{"credential": category,
                    "valid_through": worker.get("valid_through"),
                    "valid_from": worker.get("valid_from")}]
    if not entries and "*" in credential_names:
        entries = [{"credential": "*",
                    "valid_through": worker.get("valid_through"),
                    "valid_from": worker.get("valid_from")}]

    if not entries:
        return {"covered": False, "reasons": ["credential_not_held"], "at": iso(at)}

    valid_entry = False
    for entry in entries:
        start, end = _interval_bounds(entry)
        if not interval_contains(start, end, at):
            reasons.append("credential_outside_validity")
            continue
        if entry.get("revoked") or entry.get("status") in {"revoked", "cancelled"}:
            reasons.append("credential_revoked")
            continue
        valid_entry = True

    if not valid_entry:
        return {"covered": False, "reasons": reasons or ["credential_invalid"], "at": iso(at)}

    # 人员级停用状态 / 停用区间
    status = worker.get("status")
    if status in {"suspended", "disabled", "inactive", "revoked"}:
        sus = worker.get("suspension") if isinstance(worker.get("suspension"), dict) else {}
        start, end = _interval_bounds(sus) if sus else (None, None)
        if not sus or interval_contains(start, end, at):
            return {"covered": False, "reasons": ["worker_suspended"], "at": iso(at)}

    for sus in as_list(worker.get("suspensions")):
        if not isinstance(sus, dict):
            continue
        start, end = _interval_bounds(sus)
        if interval_contains(start, end, at):
            if not sus.get("lifted"):
                return {"covered": False,
                        "reasons": [sus.get("reason") or "worker_suspended"],
                        "at": iso(at)}

    return {"covered": True, "reasons": [], "at": iso(at)}


def credential_names_of(worker: dict) -> set[str]:
    names: set[str] = set()
    for item in as_list(first_key(worker, "credentials", "credential", default=[])):
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            name = item.get("credential") or item.get("type") \
                or item.get("category") or item.get("name") or item.get("code")
            if name:
                names.add(name)
    for q in as_list(worker.get("qualifications")):
        if isinstance(q, dict) and q.get("credential"):
            names.add(q["credential"])
    return names


def worker_has_role(worker: dict, role: str) -> bool:
    raw_roles = as_list(first_key(worker, "roles", "role", default=[]))
    roles: set[str] = set()
    for item in raw_roles:
        if isinstance(item, str):
            roles.add(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("role") or item.get("code")
            if name:
                roles.add(name)
    aliases = {"lead": {"lead", "inspector", "主检", "lead_inspector"},
               "review": {"review", "reviewer", "independent_review", "复核",
                          "reviewer"}}
    wanted = aliases.get(role, {role})
    return bool(roles & wanted) or role in roles


def recusal_unit(recusal: dict) -> str | None:
    return first_key(recusal, "unit", "organization", "org", "employer",
                     "inspected_unit", "unit_id", "contractor", "company")


def recusal_targets(recusal: dict, object_id: str, site: str | None,
                    unit: str | None = None) -> bool:
    target_object = first_key(recusal, "object", "object_id", "target", "subject")
    target_site = first_key(recusal, "site", "worksite", "location", "site_id")
    target_unit = recusal_unit(recusal)
    if target_object is not None and target_object == object_id:
        return True
    if target_site is not None and site is not None and target_site == site:
        return True
    # 台账里 site 同时充当 object
    if target_object is None and target_site == object_id:
        return True
    if target_object == site:
        return True
    # 回避关系是针对“被检查单位”的承包/利害关系
    if target_unit and unit and target_unit == unit:
        return True
    return False


def recusal_known_at(recusal: dict, at: datetime) -> bool:
    reported = parse_ts(first_key(recusal, "reported_at", "report_time",
                                  "received_at", "filed_at", "created_at"))
    if reported is None:
        # 没有补报时间的，视为当时已在台账
        return True
    return reported <= at


def relationship_covers(recusal: dict, at: datetime) -> bool:
    start = parse_ts(first_key(recusal, "relationship_since", "since",
                                "relation_since", "relationship_start"))
    until = parse_ts(first_key(recusal, "relationship_until", "relationship_end",
                               "until", "ended_at"))
    if start is None:
        # 关系起始时间缺失时，保守地认为可能覆盖，交复核程序认定
        return True
    return interval_contains(start, until, at)


# ---------------------------------------------------------------------------
# 区域联动台核心
# ---------------------------------------------------------------------------


class Desk:
    """文档型 SQLite 存储 + 业务规则。

    测试可用 ``Desk(":memory:")`` 得到隔离实例；生产/演练使用文件路径以持久化。
    """

    def __init__(self, db_path: str | Path = ":memory:", *,
                 partner_url: str | None = None, clock=None,
                 registers: dict | str | Path | None = None,
                 seed_fixtures: bool = False):
        self.db_path = str(db_path)
        self.lock = threading.RLock()
        self._clock = clock
        self.partner_url = partner_url
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock, closing(self.db.cursor()) as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS docs (
                kind TEXT NOT NULL, id TEXT NOT NULL,
                data TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS idem (
                idem_key TEXT PRIMARY KEY, response TEXT NOT NULL, status INTEGER)""")
            self.db.commit()
        if registers is not None:
            if isinstance(registers, (str, Path)):
                registers = json.loads(Path(registers).read_text("utf-8"))
            self.import_registers(registers)
        elif seed_fixtures:
            fixture = ROOT / "fixtures" / "registers.json"
            if fixture.exists():
                self.import_registers(json.loads(fixture.read_text("utf-8")))

    # ---- 基础存储 -------------------------------------------------------

    def close(self):
        with self.lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def event_time(self, payload: dict, *keys, default_now: bool = True) -> datetime:
        keys = keys or ("at", "as_of", "timestamp", "time")
        for key in keys:
            if key in payload and payload[key] is not None:
                return parse_ts(payload[key])
        header_clock = getattr(self, "_request_as_of", None)
        if header_clock is not None:
            return header_clock
        if self._clock is not None:
            value = self._clock() if callable(self._clock) else self._clock
            return parse_ts(value)
        return now_cst() if default_now else None

    def _get(self, kind: str, ident: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM docs WHERE kind=? AND id=?", (kind, ident)).fetchone()
        return json.loads(row["data"]) if row else None

    def get(self, kind: str, ident: str) -> dict:
        doc = self._get(kind, ident)
        if doc is None:
            raise NotFound(f"{kind}/{ident} 不存在", code=f"{kind}_not_found",
                           detail={"kind": kind, "id": ident})
        return doc

    def _put(self, kind: str, doc: dict) -> dict:
        ident = doc["id"]
        doc.setdefault("kind", kind)
        doc["updated_at"] = iso(now_cst())
        with self.lock:
            self.db.execute(
                "INSERT INTO docs(kind,id,data,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data, "
                "updated_at=excluded.updated_at",
                (kind, ident, dumps(doc), doc["updated_at"]))
            self.db.commit()
        return doc

    def _list(self, kind: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM docs WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def _delete_all(self):
        with self.lock:
            self.db.execute("DELETE FROM docs")
            self.db.execute("DELETE FROM audit")
            self.db.execute("DELETE FROM idem")
            self.db.commit()

    def audit(self, action: str, target: str = "", *, actor: str | None = None,
              at: datetime | None = None, detail: dict | None = None):
        with self.lock:
            self.db.execute(
                "INSERT INTO audit(at,actor,action,target,detail) VALUES(?,?,?,?,?)",
                (iso(at or now_cst()), actor, action, target,
                 dumps(detail or {})))
            self.db.commit()

    def audit_trail(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT at,actor,action,target,detail FROM audit ORDER BY seq").fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["detail"] = json.loads(item["detail"] or "{}")
            out.append(item)
        return out

    def idempotent(self, key: str | None, producer, http_status: int = 200):
        """按调用方提供的幂等键缓存响应（同 partner 桩的重试语义）。"""
        if not key:
            return producer(), http_status
        with self.lock:
            row = self.db.execute("SELECT response,status FROM idem WHERE idem_key=?",
                                  (key,)).fetchone()
            if row:
                return json.loads(row["response"]), row["status"]
            result, status = producer(), http_status
            self.db.execute("INSERT OR REPLACE INTO idem VALUES(?,?,?)",
                            (key, dumps(result), status))
            self.db.commit()
        return result, status

    # ---- 台账导入 / 演练播种 -------------------------------------------

    def import_registers(self, registers: dict, *, apply_late_recusals: bool = True) -> dict:
        """把 ``fixtures/registers.json`` 形态的模拟台账装入台面。

        重复导入按稳定 ID 覆盖，不产生重复单据，因此重启/重放都安全。
        """
        summary = {"districts": 0, "workers": 0, "mandates": 0, "cases": 0,
                   "signatures": 0, "dispositions": 0, "recusals": 0}
        with self.lock:
            for district in as_list(registers.get("towns", registers.get("districts"))):
                if isinstance(district, str):
                    district = {"id": district}
                district.setdefault("id", district.get("name") or new_id("district"))
                self._put("district", district)
                summary["districts"] += 1

            validity = {}
            for note in as_list(first_key(registers, "credential_notes",
                                          "credentialNotes", default=[])):
                validity[note["staff"]] = note

            for worker in as_list(registers.get("workers")):
                worker = dict(worker)
                note = validity.get(worker["id"])
                if note:
                    worker.setdefault("valid_through", note.get("valid_through"))
                    worker["credential_note"] = note
                self._put("worker", worker)
                summary["workers"] += 1

            commission = registers.get("commission") or registers.get("mandate")
            if commission:
                mandate = self.create_mandate(commission, _internal=True)
                summary["mandates"] += 1
                mandate_id = mandate["id"]
            else:
                mandate_id = None

            assignment = registers.get("assignment")
            if assignment:
                plan = {"id": f"assignment-{mandate_id or 'manual'}",
                        "mandate_id": mandate_id, **assignment}
                self._put("assignment", plan)

            replacement_at = registers.get("replacement_at")
            if assignment and assignment.get("substitute") and replacement_at:
                # 台账中的替换时刻；是否构成“承接未结主检”要结合已有签名/检查判断。
                site = (commission or {}).get("worksites", [None])[0]
                lead_sig = None
                for sig in as_list(registers.get("signatures")):
                    if sig.get("role") == "lead":
                        lead_sig = sig
                lead_concluded = bool(lead_sig) and parse_ts(
                    lead_sig["signed_at"]) <= parse_ts(replacement_at)
                replacement = {
                    "id": f"replacement-{mandate_id or 'manual'}",
                    "mandate_id": mandate_id,
                    "site": site,
                    "object_id": site,
                    "from_worker": assignment.get("lead"),
                    "to_worker": assignment.get("substitute"),
                    "at": replacement_at,
                    "assumed_unfinished_lead": not lead_concluded,
                    "kind": "duty_replacement",
                }
                self._put("handoff", replacement)

            # 先建案件与签名，使后续回避影响分析能找到已签结论
            for sig in as_list(registers.get("signatures")):
                sig = dict(sig)
                case_id = sig.get("case") or sig.get("case_id") or "case-default"
                site = sig.get("site")
                case = self._ensure_case(case_id, site, mandate_id,
                                         independent_review=True)
                stored, _ = self._accept_signature(sig, case=case, _internal=True)
                summary["signatures"] += 1
                summary["cases"] = 1

            for disp in as_list(registers.get("dispositions")):
                self.create_disposition(dict(disp), _internal=True)
                summary["dispositions"] += 1

            recusal = registers.get("recusal")
            if recusal:
                result = self.report_recusal(dict(recusal), _internal=True)
                summary["recusals"] += 1
                summary["recusal_impact"] = result

            # 重复导入会按原始签署时刻重建签名；这里依据全部回避记录幂等重算影响，
            # 保证“导入/重放 fixtures”多次结果一致。
            for rec in self._list("recusal"):
                self._apply_recusal_impact(rec)

        return {"ok": True, "seeded": summary}

    def reset(self, registers: dict | None = None) -> dict:
        with self.lock:
            self._delete_all()
            if registers:
                return self.import_registers(registers)
        return {"ok": True, "reset": True}

    # ---- 县 / 工作人员 --------------------------------------------------

    def upsert_worker(self, payload: dict) -> dict:
        worker_id = first_key(payload, "id", "worker_id", "staff") or new_id("staff")
        existing = self._get("worker", worker_id) or {"id": worker_id}
        worker = {**existing, **payload, "id": worker_id}
        self._put("worker", worker)
        self.audit("worker.upsert", worker_id, actor=payload.get("actor"),
                   detail={"district": worker.get("district")})
        return worker

    def add_district(self, payload: dict) -> dict:
        ident = first_key(payload, "id", "district") or new_id("district")
        doc = {"id": ident, **{k: v for k, v in payload.items() if k != "id"}}
        return self._put("district", doc)

    def list_workers(self, *, district: str | None = None) -> list[dict]:
        workers = self._list("worker")
        if district:
            workers = [w for w in workers if w.get("district") == district]
        return workers

    def suspend_worker(self, payload: dict) -> dict:
        worker_id = first_key(payload, "worker_id", "id", "worker")
        worker = self.get("worker", worker_id)
        recorded_at = self.event_time(payload)
        starts_at = parse_ts(first_key(payload, "from", "suspended_from",
                                       "start", "since"), default=recorded_at)
        ends_at = first_key(payload, "until", "resume_at", "suspended_until",
                            "to", "end")
        suspension = {
            "from": iso(starts_at),
            "until": ends_at,
            "reason": payload.get("reason", "temporary_suspension"),
        }
        worker.setdefault("suspensions", []).append(suspension)
        if payload.get("status"):
            worker["status"] = payload["status"]
        self._put("worker", worker)
        self.audit("worker.suspend", worker_id, at=recorded_at, detail=suspension)
        return worker

    def resume_worker(self, payload: dict) -> dict:
        worker_id = first_key(payload, "worker_id", "id", "worker")
        worker = self.get("worker", worker_id)
        at = self.event_time(payload)
        for sus in reversed(worker.get("suspensions", [])):
            start, end = _interval_bounds(sus)
            if not sus.get("lifted") and (end is None or at <= end):
                sus["lifted"] = True
                sus["lifted_at"] = iso(at)
                break
        worker["status"] = "active"
        self._put("worker", worker)
        self.audit("worker.resume", worker_id, at=at)
        return worker

    # ---- 委托 -----------------------------------------------------------

    def create_mandate(self, payload: dict, *, _internal: bool = False) -> dict:
        ident = first_key(payload, "id", "mandate_id", "commission_id") or new_id("mandate")
        existing = self._get("mandate", ident)
        if existing and not _internal:
            return existing
        valid_from = parse_ts(first_key(payload, "valid_from", "from", "starts_at", "start"))
        valid_to = parse_ts(first_key(payload, "valid_to", "to", "ends_at", "until", "expires_at"))
        worksites = as_list(first_key(payload, "worksites", "sites", "locations",
                                      default=[]))
        scopes = as_list(first_key(payload, "scopes", "categories", "object_categories",
                                   "credentials", default=[]))
        allowed_actions = as_list(first_key(payload, "allowed_actions",
                                            "allowed_dispositions", "permitted_actions",
                                            "dispositions", default=[]))
        mandate = {
            "id": ident,
            "kind": "mandate",
            "issuer": first_key(payload, "issuer", "recipient", "recipient_district",
                                "county_from"),
            "donor": first_key(payload, "donor", "donor_district", "sender",
                               "dispatched_by"),
            "worksites": worksites,
            "sites": worksites,
            "scopes": scopes,
            "categories": scopes,
            "allowed_actions": allowed_actions,
            "valid_from": iso(valid_from),
            "valid_to": iso(valid_to),
            "from": iso(valid_from),
            "to": iso(valid_to),
            "status": payload.get("status", "open"),
            "replaces": first_key(payload, "replaces", "supersedes"),
            "independent_review_required": payload.get(
                "independent_review_required", payload.get("require_independent_review")),
            "title": payload.get("title"),
        }
        self._put("mandate", mandate)
        if mandate["replaces"]:
            old = self._get("mandate", mandate["replaces"])
            if old:
                old["status"] = "superseded"
                old["superseded_by"] = ident
                self._put("mandate", old)
        if not _internal:
            self.audit("mandate.create", ident, actor=payload.get("actor"))
        return mandate

    def list_mandates(self, *, issuer: str | None = None,
                      donor: str | None = None) -> list[dict]:
        items = self._list("mandate")
        if issuer:
            items = [m for m in items if m.get("issuer") == issuer]
        if donor:
            items = [m for m in items if m.get("donor") == donor]
        return items

    def close_mandate(self, payload: dict) -> dict:
        ident = first_key(payload, "mandate_id", "id")
        mandate = self.get("mandate", ident)
        mandate["status"] = first_key(payload, "status", default="closed")
        self._put("mandate", mandate)
        self.audit("mandate.close", ident, detail={"status": mandate["status"]})
        return mandate

    def mandate_covers(self, mandate: dict, *, site: str | None = None,
                       object_id: str | None = None, category: str | None = None,
                       at: datetime | None = None) -> dict:
        problems = []
        targets = {site, object_id} - {None}
        if mandate.get("worksites") and not (targets & set(mandate["worksites"])):
            problems.append("site_out_of_mandate")
        if category and mandate.get("scopes") and category not in mandate["scopes"]:
            problems.append("category_out_of_mandate")
        if mandate.get("status") not in (None, "open"):
            problems.append(f"mandate_{mandate.get('status')}")
        if at is not None:
            start = parse_ts(mandate.get("valid_from"))
            end = parse_ts(mandate.get("valid_to"))
            if not interval_contains(start, end, at):
                problems.append("outside_mandate_period")
        return {"covered": not problems, "reasons": problems}

    def _mandate_for(self, mandate_id: str | None, *, site=None, object_id=None,
                     category=None, at=None) -> tuple[dict | None, dict]:
        if mandate_id:
            mandate = self.get("mandate", mandate_id)
        else:
            candidates = self._list("mandate")
            mandate = None
            for cand in candidates:
                verdict = self.mandate_covers(
                    cand, site=site, object_id=object_id, category=category, at=at)
                if verdict["covered"]:
                    mandate = cand
                    break
            if mandate is None:
                return None, {"covered": False, "reasons": ["mandate_not_found"]}
        verdict = self.mandate_covers(mandate, site=site, object_id=object_id,
                                     category=category, at=at)
        return mandate, verdict

    # ---- 检查事项与分工 -------------------------------------------------

    def create_check(self, payload: dict) -> dict:
        ident = first_key(payload, "id", "check_id", "task_id") or new_id("task")
        if self._get("check", ident):
            return self._get("check", ident)
        site = first_key(payload, "site", "worksite", "location")
        object_id = first_key(payload, "object_id", "object", "target", default=site)
        category = first_key(payload, "category", "scope", "credential",
                             "object_category")
        kind = first_key(payload, "kind", "role", "check_kind", default="lead")
        if kind in ("reviewer", "independent_review"):
            kind = "review"
        at = self.event_time(payload, "created_at", "at")
        # 建单/分工可以早于执行时段，时间窗在领取、提交、签字等执行动作上卡。
        mandate, verdict = self._mandate_for(
            first_key(payload, "mandate_id", "mandate", "commission_id"),
            site=site, object_id=object_id, category=category, at=None)
        if mandate and not verdict["covered"] and payload.get("strict_mandate", True):
            raise Forbidden("委托不覆盖该检查事项", code="mandate_scope_denied",
                            detail=verdict)
        check = {
            "id": ident,
            "kind": "check",
            "check_kind": kind,
            "site": site,
            "object_id": object_id,
            "category": category,
            "title": payload.get("title"),
            "mandate_id": mandate["id"] if mandate else payload.get("mandate_id"),
            "status": first_key(payload, "status", default="unstarted"),
            "assignee": first_key(payload, "assignee", "worker_id", "owner"),
            "origin_assignee": first_key(payload, "origin_assignee", "assignee"),
            "created_at": iso(at),
            "started_at": None,
            "completed_at": None,
            "records": [],
        }
        if check["assignee"] and check["status"] == "unstarted":
            check["status"] = "in_progress"
            check["started_at"] = iso(at)
        self._put("check", check)
        self.audit("check.create", ident, actor=payload.get("actor"), at=at)
        return check

    def list_checks(self, **filters) -> list[dict]:
        items = self._list("check")
        worker = filters.get("worker") or filters.get("assignee")
        if worker:
            items = [c for c in items if c.get("assignee") == worker
                     or worker in c.get("handed_to", [])]
        for key in ("site", "object_id", "mandate_id", "status", "check_kind"):
            value = filters.get(key)
            if value is not None:
                items = [c for c in items if c.get(key) == value]
        return items

    def _worker_or_404(self, worker_id: str) -> dict:
        return self.get("worker", worker_id)

    def _blocking_recusal(self, worker_id: str, object_id: str, site: str | None,
                          at: datetime, unit: str | None = None) -> dict | None:
        for rec in self._list("recusal"):
            if rec.get("worker") != worker_id:
                continue
            if not recusal_targets(rec, object_id, site, unit):
                continue
            if recusal_known_at(rec, at):
                return rec
        return None

    def _successor_lead(self, worker_id: str, object_id: str) -> dict | None:
        for ho in self._list("handoff"):
            if ho.get("to_worker") != worker_id:
                continue
            if ho.get("object_id") != object_id and ho.get("site") != object_id:
                continue
            if ho.get("assumed_unfinished_lead"):
                return ho
        return None

    def _valid_review_signature(self, worker_id: str, object_id: str) -> dict | None:
        for sig in self._list("signature"):
            if sig.get("worker") != worker_id or sig.get("role") != "review":
                continue
            if sig.get("object_id") != object_id and sig.get("site") != object_id:
                continue
            if sig.get("status") == "valid":
                return sig
        return None

    def claim_check(self, payload: dict) -> dict:
        """现场人员领取尚未开始且资质覆盖的检查。"""
        worker_id = first_key(payload, "worker_id", "worker", "assignee", "by")
        check_id = first_key(payload, "check_id", "id", "task_id")
        at = self.event_time(payload, "claimed_at", "at")
        with self.lock:
            check = self.get("check", check_id)
            worker = self._worker_or_404(worker_id)
            object_id = check.get("object_id") or check.get("site")

            if check.get("status") not in ("unstarted",):
                if check.get("assignee") == worker_id:
                    return check
                raise Conflict("该检查已经开始或已被领取，不能作为新领事项",
                               code="check_already_started",
                               detail={"check_id": check_id,
                                       "status": check.get("status"),
                                       "assignee": check.get("assignee")})

            qual = credential_decision(worker, check.get("category") or "*", at)
            if check.get("category") and not qual["covered"]:
                raise Forbidden("资质不覆盖该检查类别", code="credential_not_covered",
                                detail=qual)
            if not worker_has_role(worker, check.get("check_kind", "lead")):
                raise Forbidden("人员角色不能承担此类检查", code="role_not_allowed",
                                detail={"need_role": check.get("check_kind")})

            # 若派工单明确要求到场登记，则未到场不能领取
            for dispatch in self._list("dispatch"):
                if dispatch.get("mandate_id") != check.get("mandate_id"):
                    continue
                if not dispatch.get("require_arrival"):
                    continue
                arrived = any(a.get("worker_id") == worker_id
                              and parse_ts(a.get("arrived_at")) <= at
                              for a in self._list("arrival")
                              if a.get("dispatch_id") == dispatch["id"]
                              or a.get("mandate_id") == check.get("mandate_id"))
                if not arrived:
                    raise Forbidden("人员尚未到场登记，不能领取检查",
                                    code="worker_not_arrived")

            mandate = None
            if check.get("mandate_id"):
                mandate = self._get("mandate", check["mandate_id"])
            if mandate:
                verdict = self.mandate_covers(
                    mandate, site=check.get("site"), object_id=object_id,
                    category=check.get("category"), at=at)
                if not verdict["covered"]:
                    raise Forbidden("当前时间/范围不在委托有效期内",
                                    code="mandate_scope_denied", detail=verdict)

            recusal = self._blocking_recusal(worker_id, object_id, check.get("site"),
                                             at, check.get("unit"))
            if recusal:
                raise Forbidden("该人员对本对象存在已报回避，不能领取",
                                code="recused", detail={"recusal_id": recusal["id"]})

            # 独立复核的替补冲突：已经承接未结主检的人不能再领同对象复核
            if check.get("check_kind") == "review" and self._successor_lead(
                    worker_id, object_id):
                raise Conflict("替补已承接未结主检，不能再承担同对象独立复核",
                               code="substitute_dual_role")

            check["status"] = "in_progress"
            check["assignee"] = worker_id
            check.setdefault("origin_assignee", worker_id)
            check["started_at"] = iso(at)
            self._put("check", check)
            self.audit("check.claim", check_id, actor=worker_id, at=at)
            return check

    def submit_to_check(self, payload: dict) -> dict:
        """现场提交材料/记录到自己承接的事项；不改变证据的独立来源。"""
        worker_id = first_key(payload, "worker_id", "worker", "by", "observer")
        check_id = first_key(payload, "check_id", "id", "task_id")
        at = self.event_time(payload, "submitted_at", "at")
        with self.lock:
            check = self.get("check", check_id)
            if check.get("status") in ("done", "signed", "cancelled"):
                raise Conflict("事项已结束，不能继续提交；如需重开须走复核/新委托",
                               code="check_closed")
            assignees = {check.get("assignee"), *check.get("handed_to", [])}
            if worker_id and worker_id not in assignees:
                raise Forbidden("只能向自己承接（含交接承接）的事项提交",
                                code="not_assignee",
                                detail={"assignee": check.get("assignee")})
            record = {
                "record_id": first_key(payload, "record_id", "id",
                                       default=new_id("record")),
                "by": worker_id,
                "submitted_at": iso(at),
                "kind": payload.get("record_kind", "field_note"),
                "content": payload.get("content", payload.get("note")),
                "evidence_ids": as_list(first_key(payload, "evidence_ids",
                                                  "evidence", default=[])),
                "payload": {k: v for k, v in payload.items()
                            if k not in {"worker_id", "worker", "by", "check_id", "id",
                                         "task_id", "at", "submitted_at"}},
            }
            check.setdefault("records", []).append(record)
            if payload.get("complete"):
                check["status"] = "submitted"
                check["completed_at"] = iso(at)
            self._put("check", check)
            self.audit("check.submit", check_id, actor=worker_id, at=at,
                       detail={"record_id": record["record_id"]})
            return check

    def handover(self, payload: dict) -> dict:
        """检查中途换人：原始记录随事项移交，但保留原巡检员署名。"""
        check_id = first_key(payload, "check_id", "task_id")
        to_worker = first_key(payload, "to_worker", "replacement", "new_worker", "to")
        from_worker = first_key(payload, "from_worker", "from", "original_worker")
        at = self.event_time(payload, "at", "handed_over_at", "replacement_at")
        reason = payload.get("reason", "mid_check_replacement")
        with self.lock:
            if check_id:
                check = self.get("check", check_id)
            else:
                # 允许按对象+主检定位
                site = first_key(payload, "site", "object_id")
                candidates = [c for c in self._list("check")
                              if (c.get("object_id") == site or c.get("site") == site)
                              and c.get("check_kind", "lead") == "lead"
                              and c.get("status") == "in_progress"]
                if not candidates:
                    raise NotFound("找不到可交接的进行中检查", code="check_not_found")
                check = candidates[0]
                check_id = check["id"]

            to_doc = self._worker_or_404(to_worker)
            from_worker = from_worker or check.get("assignee")
            object_id = check.get("object_id") or check.get("site")
            category = check.get("category")

            qual = credential_decision(to_doc, category or "*", at)
            if category and not qual["covered"]:
                raise Forbidden("接替人员资质不覆盖该检查类别，不能承接",
                                code="credential_not_covered", detail=qual)

            # 判断是否“承接未结主检”：事项没有已生效的主检签名且未完成
            existing_lead_sig = None
            for sig in self._list("signature"):
                if (sig.get("object_id") == object_id or sig.get("site") == object_id) \
                        and sig.get("role") == "lead" and sig.get("status") == "valid":
                    existing_lead_sig = sig
                    break
            unfinished_lead = (check.get("check_kind", "lead") == "lead"
                              and existing_lead_sig is None
                              and check.get("status") not in ("done", "signed"))

            if unfinished_lead and self._valid_review_signature(to_worker, object_id):
                raise Conflict(
                    "该人员已在同对象作出有效独立签字，不能再承接未结主检（角色冲突）",
                    code="substitute_dual_role")

            ho = {
                "id": first_key(payload, "id", "handover_id",
                                default=new_id("handover")),
                "kind": "handoff",
                "type": "check_handover",
                "check_id": check_id,
                "mandate_id": check.get("mandate_id"),
                "site": check.get("site"),
                "object_id": object_id,
                "from_worker": from_worker,
                "to_worker": to_worker,
                "at": iso(at),
                "reason": reason,
                "records_transferred": len(check.get("records", [])),
                "assumed_unfinished_lead": unfinished_lead,
            }
            self._put("handoff", ho)

            check["assignee"] = to_worker
            check.setdefault("handed_to", []).append(to_worker)
            check["handed_over_at"] = iso(at)
            # 原始记录不动：每条 record.by 仍是原采集/记录人
            self._put("check", check)
            self.audit("check.handover", check_id, actor=from_worker, at=at,
                       detail={"to": to_worker,
                               "unfinished_lead": unfinished_lead})
            return {"handover": ho, "check": check}

    def replace_duty(self, payload: dict) -> dict:
        """派出方替换值守人员（派工名册层面）。

        替换本身不把已开始的检查划给新人；新人需要自己领取未开始且资质覆盖的事项，
        或通过 :meth:`handover` 承接中途事项。
        """
        at = self.event_time(payload, "at", "replacement_at")
        mandate_id = first_key(payload, "mandate_id", "mandate")
        site = first_key(payload, "site", "worksite", "object_id")
        from_worker = first_key(payload, "from_worker", "previous", "from")
        to_worker = first_key(payload, "to_worker", "replacement", "new_worker", "to")
        with self.lock:
            to_doc = self._worker_or_404(to_worker)
            category = payload.get("category")
            if category:
                qual = credential_decision(to_doc, category, at)
                if not qual["covered"]:
                    raise Forbidden("替换人员资质不覆盖该对象类别",
                                    code="credential_not_covered", detail=qual)
            rec = {
                "id": first_key(payload, "id", default=new_id("replacement")),
                "kind": "handoff",
                "type": "duty_replacement",
                "mandate_id": mandate_id,
                "site": site,
                "object_id": first_key(payload, "object_id", "object", default=site),
                "from_worker": from_worker,
                "to_worker": to_worker,
                "at": iso(at),
                "scope": payload.get("scope"),
                "note": payload.get("note"),
                "assumed_unfinished_lead": False,
            }
            self._put("handoff", rec)
            assignment_id = payload.get("assignment_id") or f"assignment-{mandate_id}"
            plan = self._get("assignment", assignment_id) or {"id": assignment_id}
            history = plan.setdefault("replacements", [])
            history.append({"at": iso(at), "from": from_worker, "to": to_worker})
            if payload.get("role"):
                plan[payload["role"]] = to_worker
            self._put("assignment", plan)
            self.audit("duty.replace", mandate_id or assignment_id, at=at,
                       detail={"from": from_worker, "to": to_worker})
            return rec

    # ---- 派工单与到场登记（名单收到时往往还不知道哪几位会到） -------------

    def save_assignment(self, payload: dict) -> dict:
        mandate_id = first_key(payload, "mandate_id", "mandate", "commission_id")
        ident = first_key(payload, "id", "assignment_id") \
            or f"assignment-{mandate_id or new_id('plan')}"
        existing = self._get("assignment", ident) or {"id": ident}
        doc = {**existing, **payload, "id": ident, "kind": "assignment",
               "mandate_id": mandate_id or existing.get("mandate_id")}
        self._put("assignment", doc)
        self.audit("assignment.save", ident, actor=payload.get("actor"))
        return doc

    def list_assignments(self) -> list[dict]:
        return self._list("assignment")

    def create_dispatch(self, payload: dict) -> dict:
        ident = first_key(payload, "id", "dispatch_id") or new_id("dispatch")
        mandate_id = first_key(payload, "mandate_id", "mandate", "commission_id")
        worker_refs = as_list(first_key(payload, "workers", "worker_ids", "staff",
                                        "roster", default=[]))
        roster = []
        for ref in worker_refs:
            if isinstance(ref, str):
                roster.append({"worker_id": ref, "status": "listed"})
            else:
                roster.append({"worker_id": first_key(ref, "worker_id", "id", "worker"),
                              "role": ref.get("role"),
                              "status": ref.get("status", "listed")})
        dispatch = {
            "id": ident,
            "kind": "dispatch",
            "mandate_id": mandate_id,
            "issuer": payload.get("issuer"),
            "donor": first_key(payload, "donor", "district"),
            "site": first_key(payload, "site", "worksite"),
            "object_id": first_key(payload, "object_id", "object"),
            "scope": payload.get("scope"),
            "roster": roster,
            "require_arrival": bool(payload.get("require_arrival", False)),
            "status": "planned",
            "created_at": iso(self.event_time(payload, "created_at", "at")),
        }
        self._put("dispatch", dispatch)
        self.audit("dispatch.create", ident, detail={"mandate": mandate_id,
                                                      "workers": len(roster)})
        return dispatch

    def list_dispatches(self, *, district: str | None = None) -> list[dict]:
        items = self._list("dispatch")
        if district:
            items = [d for d in items if d.get("donor") == district]
        return items

    def record_arrival(self, payload: dict) -> dict:
        worker_id = first_key(payload, "worker_id", "worker", "id")
        at = self.event_time(payload, "arrived_at", "at", "check_in_at")
        mandate_id = first_key(payload, "mandate_id", "mandate")
        dispatch_id = first_key(payload, "dispatch_id", "dispatch")
        arrival = {
            "id": first_key(payload, "id", default=new_id("arrival")),
            "kind": "arrival",
            "worker_id": worker_id,
            "mandate_id": mandate_id,
            "dispatch_id": dispatch_id,
            "arrived_at": iso(at),
            "via": payload.get("via", "check_in"),
        }
        self._put("arrival", arrival)
        if dispatch_id:
            dispatch = self._get("dispatch", dispatch_id)
            if dispatch:
                for row in dispatch.get("roster", []):
                    if row.get("worker_id") == worker_id:
                        row["status"] = "arrived"
                        row["arrived_at"] = iso(at)
                self._put("dispatch", dispatch)
        self.audit("worker.arrival", worker_id, at=at)
        return arrival

    def arrivals(self, *, worker_id: str | None = None,
                 mandate_id: str | None = None) -> list[dict]:
        items = self._list("arrival")
        if worker_id:
            items = [a for a in items if a.get("worker_id") == worker_id]
        if mandate_id:
            items = [a for a in items if a.get("mandate_id") == mandate_id]
        return items

    # ---- 采证材料 -------------------------------------------------------

    def add_evidence(self, payload: dict) -> dict:
        """登记一份采证材料（可来自对端回执，也可离线补录）。

        证据进入台面不会把任务改派给 observer，也不会自动形成复核签名。
        """
        ident = first_key(payload, "id", "evidence_id", "submission",
                          default=new_id("evidence"))
        existing = self._get("evidence", ident)
        if existing:
            return existing
        site = first_key(payload, "site", "worksite", "location")
        object_id = first_key(payload, "object_id", "object", "target", default=site)
        observer = first_key(payload, "observer", "worker", "collected_by")
        captured_at = parse_ts(first_key(payload, "captured_at", "collected_at",
                                         "actual_collected_at"))
        received_at = self.event_time(payload, "received_at", "at", default_now=True)
        receipt = first_key(payload, "signature_receipt", "receipt",
                            "receipt_no", "receipt_id")
        record = {
            "id": ident,
            "kind": "evidence",
            "submission": first_key(payload, "submission", "submission_id",
                                    default=ident),
            "site": site,
            "object_id": object_id,
            "observer": observer,
            "category": first_key(payload, "category", "scope"),
            "captured_at": iso(captured_at),
            "received_at": iso(received_at),
            "signature_receipt": receipt,
            "receipt": receipt,
            "source": payload.get("source", "local"),
            "partner_operation_id": payload.get("partner_operation_id"),
            "check_id": first_key(payload, "check_id", "task_id"),
            "payload": payload.get("extra", {}),
        }
        self._put("evidence", record)
        self.audit("evidence.ingest", ident, actor=observer,
                   detail={"site": site, "receipt": receipt})
        return record

    def list_evidence(self, *, site: str | None = None,
                     object_id: str | None = None) -> list[dict]:
        items = self._list("evidence")
        if site:
            items = [e for e in items if e.get("site") == site
                     or e.get("object_id") == site]
        if object_id:
            items = [e for e in items if e.get("object_id") == object_id]
        return items

    def fetch_partner_evidence(self, payload: dict, *, client=None) -> dict:
        """调用本地采证对端 ``evidence`` 操作，并消化断连/重传。"""
        request = payload.get("request")
        if not isinstance(request, dict):
            request = {k: payload[k] for k in ("site", "observer", "submission")
                       if k in payload}
        operation_id = first_key(payload, "operation_id", "id",
                                 default=f"ev-{request.get('submission') or new_id('op')}")
        base_url = first_key(payload, "partner_url", "base_url") or self.partner_url
        if not base_url:
            import os
            base_url = os.environ.get("PARTNER_URL", "http://127.0.0.1:8765")
        if client is None:
            client = PartnerClient(base_url)
        try:
            reply = client.submit("evidence", request, operation_id)
        except Exception as exc:  # 网络/协议错误统一成 502
            if isinstance(exc, DeskError):
                raise
            raise PartnerUnavailable(f"采证对端联调失败: {exc}",
                                     detail={"partner_url": base_url}) from exc
        material = {
            "site": request.get("site"),
            "observer": request.get("observer"),
            "submission": request.get("submission"),
            "captured_at": reply.get("captured_at"),
            "signature_receipt": reply.get("signature_receipt"),
            "source": "partner",
            "partner_operation_id": operation_id,
        }
        evidence = self.add_evidence(material)
        return {"ok": True, "operation_id": operation_id, "reply": reply,
                "evidence": evidence}

    def evidence_usable(self, evidence: dict, object_id: str,
                        at: datetime | None = None) -> dict:
        """同对象已独立取得且仍有效的记录才可继续使用。"""
        captured = parse_ts(evidence.get("captured_at"))
        at = at or captured or now_cst()
        reasons = []
        observer = evidence.get("observer")
        worker = self._get("worker", observer) if observer else None
        category = evidence.get("category")
        if worker:
            if category:
                qual = credential_decision(worker, category, at)
                if not qual["covered"]:
                    reasons.extend(qual["reasons"])
            else:
                names = credential_names_of(worker)
                if names and not any(
                        credential_decision(worker, name, at)["covered"]
                        for name in names):
                    reasons.append("observer_credential_invalid_at_capture")
        # 与该对象已被认定的主检/回避人员绑定的证据，不算“独立取得”
        conflicted = False
        if observer:
            for rec in self._list("recusal"):
                if rec.get("worker") == observer and recusal_targets(
                        rec, object_id, evidence.get("site"), evidence.get("unit")):
                    conflicted = True
                    reasons.append("observer_recused")
                    break
        independence = evidence.get("independence")
        if independence is False:
            conflicted = True
            reasons.append("not_independent")
        has_receipt = bool(evidence.get("signature_receipt") or evidence.get("receipt"))
        if not has_receipt and evidence.get("require_receipt", True):
            reasons.append("missing_signature_receipt")
        usable = not reasons and not conflicted and has_receipt
        return {"evidence_id": evidence["id"], "usable": usable,
                "independent": not conflicted, "reasons": reasons,
                "captured_at": evidence.get("captured_at"),
                "observer": observer}

    # ---- 案件 / 签名 / 结论 --------------------------------------------

    def _ensure_case(self, case_id: str, site: str | None,
                     mandate_id: str | None, *, independent_review: bool | None = None,
                     object_id: str | None = None) -> dict:
        case = self._get("case", case_id)
        if case is None:
            case = {
                "id": case_id,
                "kind": "case",
                "site": site,
                "object_id": object_id or site,
                "mandate_id": mandate_id,
                "independent_review_required": bool(independent_review),
                "status": "open",
                "current": {"lead": None, "review": None},
                "signature_ids": [],
            }
            self._put("case", case)
        elif independent_review is not None:
            case["independent_review_required"] = independent_review
            self._put("case", case)
        return case

    def create_case(self, payload: dict) -> dict:
        case_id = first_key(payload, "id", "case_id") or new_id("case")
        site = first_key(payload, "site", "worksite")
        object_id = first_key(payload, "object_id", "object", default=site)
        mandate_id = first_key(payload, "mandate_id", "mandate")
        required = payload.get("independent_review_required",
                               payload.get("require_independent_review", True))
        case = self._ensure_case(case_id, site, mandate_id,
                                independent_review=bool(required),
                                object_id=object_id)
        self.audit("case.create", case_id, actor=payload.get("actor"))
        return case

    def _accept_signature(self, sig: dict, *, case: dict | None = None,
                          _internal: bool = False) -> tuple[dict, dict]:
        """校验并落一个签名。

        返回 ``(signature, verdict)``。校验不通过抛 :class:`DeskError`；
        导入历史台账（``_internal``）时同样按签署时刻判定，但保留原件。
        """
        worker_id = first_key(sig, "worker", "worker_id", "staff", "by")
        role = first_key(sig, "role", "sign_role", default="lead")
        if role in ("reviewer", "independent_review"):
            role = "review"
        signed_at = parse_ts(first_key(sig, "signed_at", "at", "timestamp",
                                       default=iso(now_cst())))
        site = first_key(sig, "site", "worksite")
        object_id = first_key(sig, "object_id", "object", "target", default=site)
        unit = first_key(sig, "unit", "organization", "org", "inspected_unit",
                         "employer")
        category = first_key(sig, "category", "scope")
        case_id = first_key(sig, "case", "case_id", "conclusion_id")

        worker = self._get("worker", worker_id)
        verdict = {"admissible": True, "reasons": [], "signed_at": iso(signed_at)}

        if worker is None:
            verdict.update(admissible=False)
            verdict["reasons"].append("worker_not_found")
        else:
            if category is None:
                cats = as_list(worker.get("credential", worker.get("credentials")))
                category = cats[0] if cats else None
            qual = credential_decision(worker, category or "*", signed_at)
            if category and not qual["covered"]:
                verdict["admissible"] = False
                verdict["reasons"].extend(qual["reasons"])
            if not worker_has_role(worker, role):
                verdict["admissible"] = False
                verdict["reasons"].append("role_not_held")
            known_recusal = self._blocking_recusal(worker_id, object_id, site,
                                                   signed_at, unit)
            if known_recusal:
                verdict["admissible"] = False
                verdict["reasons"].append("recusal_on_record_at_signing")
                verdict["recusal_id"] = known_recusal["id"]

        # 替补双重身份：已承接未结主检者不得作同对象独立签字
        if role == "review":
            successor = self._successor_lead(worker_id, object_id)
            if successor:
                verdict["admissible"] = False
                verdict["reasons"].append("substitute_dual_role")
                verdict["handover_id"] = successor["id"]

        # 反向同样禁止：已在同对象有效独立签字的人，不能再签主检（双重身份）
        if role == "lead" and self._valid_review_signature(worker_id, object_id):
            verdict["admissible"] = False
            verdict["reasons"].append("already_independent_reviewer")

        sig_id = first_key(sig, "id", "signature_id",
                           default=f"sig-{case_id or object_id}-{worker_id}-{role}")
        stored = self._get("signature", sig_id) or {"id": sig_id, "kind": "signature"}
        stored.update({
            "id": sig_id,
            "case_id": case_id,
            "site": site,
            "object_id": object_id,
            "unit": unit,
            "worker": worker_id,
            "role": role,
            "category": category,
            "signed_at": iso(signed_at),
            "verdict": verdict,
            "status": "valid" if verdict["admissible"] else "rejected",
            "payload": {k: v for k, v in sig.items()
                        if k not in {"id", "signature_id", "worker", "worker_id",
                                     "staff", "by", "role", "signed_at", "at",
                                     "site", "object_id", "object", "case",
                                     "case_id", "category", "scope"}},
        })

        if not verdict["admissible"] and not _internal and sig.get("force") is not True:
            # 即便拒绝，也把审计留下，但不写入有效签名
            self.audit("signature.rejected", sig_id, actor=worker_id, at=signed_at,
                       detail=verdict)
            raise Conflict("签署时资格或回避条件不满足，签名不能用于结论",
                           code="signature_not_admissible", detail=verdict)

        if not verdict["admissible"]:
            stored["status"] = "rejected"
        self._put("signature", stored)

        if case is not None and verdict["admissible"]:
            case.setdefault("current", {})[role] = sig_id
            if sig_id not in case.setdefault("signature_ids", []):
                case["signature_ids"].append(sig_id)
            self._recompute_case_status(case)
            self._put("case", case)

        self.audit("signature.accepted" if verdict["admissible"]
                   else "signature.stored_rejected", sig_id,
                   actor=worker_id, at=signed_at, detail=verdict)
        return stored, verdict

    def sign(self, payload: dict) -> dict:
        case_id = first_key(payload, "case_id", "case", "conclusion_id")
        with self.lock:
            case = None
            if case_id:
                case = self._ensure_case(
                    case_id, payload.get("site"),
                    first_key(payload, "mandate_id", "mandate"),
                    independent_review=payload.get("independent_review_required", True),
                    object_id=first_key(payload, "object_id", "object",
                                        default=payload.get("site")))
            stored, verdict = self._accept_signature(payload, case=case)
            if case is not None:
                # 新的有效签名可能补齐复核；硬性要求齐备后自动关闭系统发起的复核
                readiness = self.object_readiness(
                    {"object_id": case.get("object_id"), "at": payload.get("at")})
                if readiness["requirements_met"]:
                    for rev in self._list("review"):
                        if rev.get("object_id") == case.get("object_id") \
                                and rev.get("status") == "open":
                            rev["status"] = "resolved"
                            rev["resolved_at"] = iso(now_cst())
                            rev["resolved_by_signature"] = stored["id"]
                            self._put("review", rev)
            return {"signature": stored, "verdict": verdict,
                    "case": self._get("case", case_id) if case_id else None}

    def _recompute_case_status(self, case: dict):
        current = case.get("current", {})
        has_lead = bool(current.get("lead"))
        need_review = case.get("independent_review_required", True)
        has_review = bool(current.get("review"))
        if has_lead and (has_review or not need_review):
            case["status"] = "complete"
        elif has_lead or has_review:
            case["status"] = "partially_signed"
        else:
            case["status"] = "open"

    # ---- 回避补报与影响处置 --------------------------------------------

    def report_recusal(self, payload: dict, *, _internal: bool = False) -> dict:
        worker_id = first_key(payload, "worker", "worker_id", "staff")
        object_id = first_key(payload, "object_id", "object", "target",
                              "site", "worksite")
        site = first_key(payload, "site", "worksite", "location", default=object_id)
        reported_at = parse_ts(first_key(payload, "reported_at", "report_time",
                                         "received_at", "at",
                                         default=iso(now_cst())))
        scope_token = object_id or recusal_unit(payload) or "scope"
        rec_id = first_key(payload, "id", "recusal_id",
                           default=f"rec-{worker_id}-{scope_token}-"
                                   f"{reported_at.strftime('%Y%m%d%H%M%S')}")
        existing = self._get("recusal", rec_id)
        if existing:
            return {"recusal": existing, "impact": existing.get("impact", {}),
                    "deduplicated": True}
        recusal = {
            "id": rec_id,
            "kind": "recusal",
            "worker": worker_id,
            "object_id": object_id,
            "site": site,
            "unit": recusal_unit(payload),
            "relationship_since": first_key(payload, "relationship_since", "since"),
            "relationship_until": first_key(payload, "relationship_until", "until"),
            "reported_at": iso(reported_at),
            "reason": payload.get("reason", "relationship_conflict"),
            "status": "reported",
            # 留下原件：完整保留补报进来的原始内容
            "raw": payload,
        }
        self._put("recusal", recusal)

        impact = self._apply_recusal_impact(recusal)
        recusal["impact"] = impact
        self._put("recusal", recusal)
        self.audit("recusal.report", rec_id, actor=payload.get("actor"),
                   at=reported_at, detail={k: len(v) for k, v in impact.items()
                                           if isinstance(v, list)})
        return {"recusal": recusal, "impact": impact}

    def _apply_recusal_impact(self, recusal: dict) -> dict:
        worker_id = recusal["worker"]
        object_id = recusal.get("object_id")
        site = recusal.get("site")
        affected_sigs, affected_cases = [], set()

        for sig in self._list("signature"):
            if sig.get("worker") != worker_id:
                continue
            if not recusal_targets(recusal, sig.get("object_id"), sig.get("site"),
                                   sig.get("unit")):
                continue
            signed_at = parse_ts(sig.get("signed_at"))
            if not relationship_covers(recusal, signed_at):
                continue
            # 影响已用于结论的有效签名：保留原件，只改状态并挂接原因
            sig["status"] = "affected"
            affects = sig.setdefault("affects", [])
            if not any(a.get("recusal_id") == recusal["id"] for a in affects):
                affects.append({
                    "recusal_id": recusal["id"],
                    "reason": "late_reported_relationship_overlaps_signing",
                    "assessed_at": iso(now_cst()),
                })
            verdict = sig.setdefault("verdict", {})
            verdict["later_recusal_affects"] = True
            self._put("signature", sig)
            affected_sigs.append(sig["id"])
            if sig.get("case_id"):
                affected_cases.add(sig["case_id"])

        initiated_reviews, flagged_dispositions = [], []
        # 单位级回避可能波及多个对象，因此从被命中的签名反推受影响对象
        affected_objects = {oid for oid in (recusal.get("object_id"),) if oid}
        for sig_id in affected_sigs:
            sig = self._get("signature", sig_id)
            oid = sig.get("object_id") or sig.get("site")
            if oid:
                affected_objects.add(oid)

        for case_id in affected_cases:
            case = self._get("case", case_id)
            if not case:
                continue
            case_object = case.get("object_id") or case.get("site") \
                or (next(iter(affected_objects), None))
            if case_object:
                affected_objects.add(case_object)
            # 当前指针若指向被影响签名，摘除该角色的“有效”指针
            for role, sig_id in list(case.get("current", {}).items()):
                if sig_id in affected_sigs:
                    case["current"][role] = None
            case.setdefault("review_reasons", [])
            if not any(r.get("recusal_id") == recusal["id"]
                       for r in case["review_reasons"]):
                case["review_reasons"].append({
                    "recusal_id": recusal["id"],
                    "affected_signatures": [s for s in affected_sigs
                                            if self._get("signature", s).get("case_id") == case_id],
                })
            self._recompute_case_status(case)
            # 迟报回避动摇了已签结论：无论签名齐备度如何，案件都进入待复核
            case["status"] = "needs_review"
            self._put("case", case)

            rev_id = f"review-{case_object or 'scope'}-{case_id}-{recusal['id']}"
            if not self._get("review", rev_id):
                review = {
                    "id": rev_id,
                    "kind": "review",
                    "type": "late_recusal_review",
                    "object_id": case_object,
                    "site": case.get("site") or recusal.get("site"),
                    "unit": recusal.get("unit"),
                    "case_id": case_id,
                    "recusal_id": recusal["id"],
                    "status": "open",
                    "reason": "late_reported_recusal_affects_signed_conclusion",
                    "created_at": iso(now_cst()),
                    "required_roles": self._missing_roles_for_object(case_object)
                    if case_object else [],
                }
                self._put("review", review)
                initiated_reviews.append(rev_id)

        # 关联处置单显示“需复议”
        for disp in self._list("disposition"):
            linked_case = disp.get("case_id") or disp.get("signed_case")
            disp_object = disp.get("object_id") or disp.get("site")
            if linked_case:
                hit = linked_case in affected_cases
            else:
                hit = disp_object in affected_objects
            if hit:
                disp["status"] = "reconsideration_required"
                disp["status_zh"] = "需复议"
                disp["flag"] = "needs_reconsideration"
                disp["needs_reconsideration"] = True
                disp["reconsideration_required"] = True
                disp["review_required"] = True
                disp["reconsideration"] = {
                    "reason": "basis_conclusion_affected_by_late_recusal",
                    "recusal_id": recusal["id"],
                    "flagged_at": iso(now_cst()),
                }
                self._put("disposition", disp)
                flagged_dispositions.append(disp["id"])

        return {
            "affected_signatures": affected_sigs,
            "affected_cases": sorted(affected_cases),
            "initiated_reviews": initiated_reviews,
            "flagged_dispositions": flagged_dispositions,
        }

    def _missing_roles_for_object(self, object_id: str) -> list[str]:
        readiness = self.object_readiness({"object_id": object_id})
        missing = []
        if readiness["requirements"]["lead"]["state"] != "valid":
            missing.append("lead")
        review_req = readiness["requirements"].get("independent_review", {})
        if review_req.get("required") and review_req.get("state") != "valid":
            missing.append("review")
        return missing

    # ---- 处置单 ---------------------------------------------------------

    def create_disposition(self, payload: dict, *, _internal: bool = False) -> dict:
        ident = first_key(payload, "id", "disposition_id", "action_id") or new_id("action")
        existing = self._get("disposition", ident)
        if existing and not _internal:
            return existing
        site = first_key(payload, "site", "worksite")
        object_id = first_key(payload, "object_id", "object", default=site)
        case_ref = first_key(payload, "signed_case", "case_id", "case",
                             "conclusion_id")
        action_type = first_key(payload, "action_type", "type", "action",
                                "kind", "disposition_type")
        released_at = parse_ts(first_key(payload, "released_at", "issued_at", "at",
                                         default=iso(now_cst())))
        mandate_id = first_key(payload, "mandate_id", "mandate")

        case = self._get("case", case_ref) if case_ref else None
        if case_ref and case is None and payload.get("strict_refs", True) and not _internal:
            raise NotFound("处置单引用的签认结论不存在", code="case_not_found",
                           detail={"case_id": case_ref})
        if mandate_id is None and case:
            mandate_id = case.get("mandate_id")
        mandate = self._get("mandate", mandate_id) if mandate_id else None
        if mandate is None and site:
            # 按对象/地点回溯覆盖它的委托，以执行“允许的处置”约束
            for cand in self._list("mandate"):
                if site in set(cand.get("worksites", [])):
                    mandate = cand
                    mandate_id = cand["id"]
                    break
        if mandate and mandate.get("allowed_actions") and action_type:
            if action_type not in mandate["allowed_actions"]:
                raise Forbidden("该处置类型不在委托允许的处置范围内",
                                code="disposition_not_allowed",
                                detail={"allowed_actions": mandate["allowed_actions"],
                                        "attempted": action_type})

        disp = {
            "id": ident,
            "kind": "disposition",
            "site": site,
            "object_id": object_id,
            "case_id": case_ref,
            "signed_case": case_ref,
            "mandate_id": mandate_id,
            "action_type": action_type,
            "released_at": iso(released_at),
            "status": first_key(payload, "status", default="effective"),
            "needs_reconsideration": False,
            "reconsideration_required": False,
            "note": payload.get("note"),
        }
        self._put("disposition", disp)
        if not _internal:
            self.audit("disposition.issue", ident, at=released_at,
                       actor=payload.get("actor"))
        return disp

    def list_dispositions(self, *, site: str | None = None,
                          status: str | None = None) -> list[dict]:
        items = self._list("disposition")
        if site:
            items = [d for d in items if d.get("site") == site
                     or d.get("object_id") == site]
        if status:
            items = [d for d in items if d.get("status") == status]
        return items

    # ---- 受援县：对象复核齐备性 ----------------------------------------

    def object_readiness(self, query: dict | str | None = None) -> dict:
        if isinstance(query, str):
            query = {"object_id": query}
        query = query or {}
        object_id = first_key(query, "object_id", "object", "site", "id")
        if not object_id:
            raise ValidationFailed("必须给出 object_id 或 site")
        at = self.event_time(query, "at", "as_of", default_now=True)

        sigs = [s for s in self._list("signature")
                if s.get("object_id") == object_id or s.get("site") == object_id]
        cases = [c for c in self._list("case")
                 if c.get("object_id") == object_id or c.get("site") == object_id]
        reviews = [r for r in self._list("review")
                   if r.get("object_id") == object_id or r.get("site") == object_id]
        open_reviews = [r for r in reviews if r.get("status") == "open"]

        mandate_required_review = None
        for m in self._list("mandate"):
            if object_id in set(m.get("worksites", [])):
                if m.get("independent_review_required") is not None:
                    mandate_required_review = m["independent_review_required"]
        if cases and cases[0].get("independent_review_required") is not None:
            review_required = cases[0]["independent_review_required"]
        elif mandate_required_review is not None:
            review_required = mandate_required_review
        else:
            review_required = True

        def role_state(role: str):
            role_sigs = [s for s in sigs if s.get("role") == role]
            valid = [s for s in role_sigs if s.get("status") == "valid"]
            affected = [s for s in role_sigs if s.get("status") == "affected"]
            rejected = [s for s in role_sigs if s.get("status") == "rejected"]
            if valid:
                chosen = valid[-1]
                state = "valid"
            elif affected:
                chosen = affected[-1]
                state = "tainted"
            elif rejected:
                chosen = rejected[-1]
                state = "rejected"
            else:
                chosen = {}
                state = "missing"
            if state == "tainted":
                reasons = ["late_recusal_affects"]
            elif state == "rejected":
                reasons = rejected[-1].get("verdict", {}).get("reasons", [])
            else:
                reasons = []
            return {
                "state": state,
                "required": True,
                "signature_id": chosen.get("id"),
                "worker": chosen.get("worker"),
                "signed_at": chosen.get("signed_at"),
                "all_signature_ids": [s["id"] for s in role_sigs],
                "reasons": reasons,
            }

        lead_req = role_state("lead")
        review_req = role_state("review")
        review_req["required"] = review_required

        # 替补冲突再核算：有效复核人不能同时是未结主检的承接人
        independence_problems = []
        if lead_req["state"] == "valid" and review_req["state"] == "valid":
            lead_sig = self._get("signature", lead_req["signature_id"])
            review_sig = self._get("signature", review_req["signature_id"])
            if lead_sig.get("worker") == review_sig.get("worker"):
                independence_problems.append("same_worker_lead_and_review")
            if self._successor_lead(review_sig.get("worker"), object_id):
                independence_problems.append("reviewer_is_substitute_lead")
        for sig in sigs:
            if sig.get("role") == "lead" and sig.get("status") == "valid":
                if self._valid_review_signature(sig.get("worker"), object_id):
                    if "lead_worker_also_reviewed" not in independence_problems:
                        independence_problems.append("lead_worker_also_reviewed")

        missing = []
        if lead_req["state"] != "valid":
            missing.append("qualified_lead_signature")
        if review_required and review_req["state"] != "valid":
            missing.append("independent_review_signature")
        if independence_problems:
            missing.append("independent_roles_conflicted")

        requirements_met = not missing
        # 条件齐备后，系统发起的复核事项应当可被签认流程自动关闭；
        # 若仍有未结复核程序，则显式列为待办。
        if requirements_met and open_reviews:
            missing.append("conclude_initiated_review")

        evidence_reports = [self.evidence_usable(e, object_id, at)
                            for e in self.list_evidence(site=object_id)]
        dispositions = [d for d in self._list("disposition")
                        if d.get("object_id") == object_id or d.get("site") == object_id]

        ready = requirements_met and not open_reviews and not independence_problems

        labels_zh = {
            "qualified_lead_signature": "缺少资格有效的主检签名（原签名可能因迟报回避被标记）",
            "independent_review_signature": "缺少符合独立性要求的复核签名（采证材料不能替代签名）",
            "independent_roles_conflicted": "主检与独立复核存在同一人或替补双重身份冲突",
            "conclude_initiated_review": "发起的指定对象复核程序尚未办结",
        }
        return {
            "object": object_id,
            "object_id": object_id,
            "site": object_id,
            "at": iso(at),
            "ready": ready,
            "review_complete": ready,
            "complete": ready,
            "independent_review_required": review_required,
            "requirements": {
                "lead": lead_req,
                "lead_signature": lead_req,
                "independent_review": review_req,
                "review": review_req,
            },
            "independence_problems": independence_problems,
            "missing": missing,
            "missing_labels_zh": [labels_zh.get(m, m) for m in missing],
            "summary_zh": ("该对象复核齐备" if ready else
                           "复核尚未齐备，尚缺：" + "；".join(
                               labels_zh.get(m, m) for m in missing)),
            "requirements_met": requirements_met,
            "open_reviews": open_reviews,
            "reviews": reviews,
            "cases": cases,
            "usable_evidence": [r for r in evidence_reports if r["usable"]],
            "evidence": evidence_reports,
            "usable_records": [r for r in evidence_reports if r["usable"]],
            "dispositions": dispositions,
        }

    def recipient_view(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        district = first_key(payload, "district", "issuer", "recipient_district")
        mandates = self.list_mandates(issuer=district) if district else self._list("mandate")
        objects = sorted({s for m in mandates for s in m.get("worksites", [])}
                         | {c.get("object_id") for c in self._list("case") if c.get("object_id")}
                         | {d.get("object_id") for d in self._list("disposition") if d.get("object_id")})
        object_reports = {o: self.object_readiness({"object_id": o}) for o in objects}
        return {
            "district": district,
            "mandates": mandates,
            "objects": [object_reports[o] for o in objects],
            "flagged_dispositions": [d for d in self._list("disposition")
                                     if d.get("status") == "reconsideration_required"],
        }

    # ---- 派出方视图 -----------------------------------------------------

    def donor_view(self, payload: dict | str | None = None) -> dict:
        if isinstance(payload, str):
            payload = {"district": payload}
        payload = payload or {}
        district = first_key(payload, "district", "donor", "donor_district")
        workers = self.list_workers(district=district)
        worker_ids = {w["id"] for w in workers}
        checks = [c for c in self._list("check")
                  if c.get("assignee") in worker_ids
                  or (set(c.get("handed_to", [])) & worker_ids)
                  or c.get("origin_assignee") in worker_ids]
        handoffs = [h for h in self._list("handoff")
                    if h.get("from_worker") in worker_ids
                    or h.get("to_worker") in worker_ids]
        assignments = self._list("assignment")
        roster = []
        all_signatures = self._list("signature")
        for w in workers:
            roster.append({
                "worker": w,
                "current_checks": [c for c in checks if c.get("assignee") == w["id"]
                                   and c.get("status") == "in_progress"],
                "history_checks": [c for c in checks if w["id"] in
                                   {c.get("assignee"), c.get("origin_assignee"),
                                    *c.get("handed_to", [])}
                                   and c.get("status") != "in_progress"],
                "signatures": [s for s in all_signatures if s.get("worker") == w["id"]],
                "handovers_from": [h for h in handoffs if h.get("from_worker") == w["id"]],
                "handovers_to": [h for h in handoffs if h.get("to_worker") == w["id"]],
            })
        return {"district": district, "workers": roster,
                "checks": checks, "handovers": handoffs,
                "dispatches": self.list_dispatches(district=district),
                "arrivals": [a for a in self.arrivals()
                             if a.get("worker_id") in worker_ids],
                "assignments": assignments}

    # ---- 现场人员工作台 -------------------------------------------------

    def worker_workbench(self, payload: dict | str | None = None) -> dict:
        if isinstance(payload, str):
            payload = {"worker_id": payload}
        payload = payload or {}
        worker_id = first_key(payload, "worker_id", "worker", "id")
        at = self.event_time(payload, "at", "as_of")
        worker = self.get("worker", worker_id)

        claimable, blocked = [], []
        for check in self._list("check"):
            if check.get("status") != "unstarted":
                continue
            item = {"check": check}
            qual = credential_decision(worker, check.get("category") or "*", at)
            reasons = list(qual["reasons"])
            if check.get("category") and not qual["covered"]:
                pass
            if not worker_has_role(worker, check.get("check_kind", "lead")):
                reasons.append("role_not_allowed")
            mandate = self._get("mandate", check["mandate_id"]) if check.get("mandate_id") else None
            if mandate:
                mv = self.mandate_covers(
                    mandate, site=check.get("site"),
                    object_id=check.get("object_id"),
                    category=check.get("category"), at=at)
                if not mv["covered"]:
                    reasons.extend(mv["reasons"])
            if self._blocking_recusal(worker_id, check.get("object_id"),
                                      check.get("site"), at, check.get("unit")):
                reasons.append("recused")
            if check.get("check_kind") == "review" and self._successor_lead(
                    worker_id, check.get("object_id")):
                reasons.append("substitute_dual_role")
            if reasons:
                blocked.append({"check_id": check["id"], "reasons": reasons})
            else:
                claimable.append(check)

        mine_in_progress = [c for c in self._list("check")
                            if c.get("assignee") == worker_id
                            and c.get("status") == "in_progress"]
        submittable = [c for c in self._list("check")
                       if (c.get("assignee") == worker_id
                           or worker_id in c.get("handed_to", []))
                       and c.get("status") in ("in_progress",)]
        return {
            "worker": worker,
            "at": iso(at),
            "claimable": claimable,
            "can_start": claimable,
            "in_progress": mine_in_progress,
            "submittable": submittable,
            "blocked": blocked,
            "my_signatures": [s for s in self._list("signature")
                              if s.get("worker") == worker_id],
            "my_handovers": [h for h in self._list("handoff")
                            if h.get("to_worker") == worker_id
                            or h.get("from_worker") == worker_id],
        }

    # ---- 新委托不得复开旧范围 ------------------------------------------

    def reopen_check(self, payload: dict) -> dict:
        check_id = first_key(payload, "check_id", "id", "task_id")
        worker_id = first_key(payload, "worker_id", "worker", "by")
        mandate_id = first_key(payload, "mandate_id", "mandate")
        at = self.event_time(payload, "at", "reopened_at")
        with self.lock:
            check = self.get("check", check_id)
            used_mandate_id = mandate_id or check.get("mandate_id")
            if mandate_id and check.get("mandate_id") and mandate_id != check["mandate_id"]:
                raise Forbidden("事项属于另一委托，不能用本委托复开",
                                code="mandate_scope_mismatch")
            mandate = self.get("mandate", used_mandate_id) if used_mandate_id else None
            # “领到了新委托的人不能凭原委托复开旧范围”：
            # 若点名了办理人且其名下已有更新委托/已在其他委托下办事，优先按该规则拒绝。
            if worker_id and mandate and not payload.get("allow_after_new_commission"):
                m_from = parse_ts(mandate.get("valid_from"))
                newer_mandates = []
                for other in self._list("mandate"):
                    if other["id"] == mandate.get("id"):
                        continue
                    if mandate.get("donor") and other.get("donor") != mandate.get("donor"):
                        continue
                    o_from = parse_ts(other.get("valid_from"))
                    if m_from and o_from and o_from > m_from:
                        newer_mandates.append(other["id"])
                if not newer_mandates:
                    for c in self._list("check"):
                        if c.get("mandate_id") in (None, mandate.get("id")):
                            continue
                        involved = {c.get("assignee"), c.get("origin_assignee"),
                                    *c.get("handed_to", [])}
                        if worker_id in involved:
                            newer_mandates.append(c["mandate_id"])
                if newer_mandates:
                    raise Forbidden(
                        "已领到新委托，不能凭原委托复开旧范围",
                        code="old_mandate_reopen_after_new_commission",
                        detail={"newer_mandates": sorted(set(newer_mandates))})
            if mandate:
                verdict = self.mandate_covers(
                    mandate, site=check.get("site"),
                    object_id=check.get("object_id"),
                    category=check.get("category"), at=at)
                if not verdict["covered"]:
                    raise Forbidden("原委托已失效/超期/被新委托替代，不能复开旧范围",
                                    code="old_mandate_closed", detail=verdict)
            check["status"] = "in_progress"
            check["reopened_at"] = iso(at)
            if payload.get("assignee"):
                check["assignee"] = payload["assignee"]
            self._put("check", check)
            self.audit("check.reopen", check_id, actor=worker_id, at=at)
            return check

    # ---- 管理/导出 ------------------------------------------------------

    def set_clock(self, payload: dict) -> dict:
        clock = first_key(payload, "now", "at", "clock")
        self._clock = clock
        return {"ok": True, "clock": clock}

    def export_state(self) -> dict:
        return {kind: self._list(kind) for kind in
                ("district", "worker", "mandate", "dispatch", "arrival",
                 "assignment", "check", "handoff",
                 "evidence", "signature", "case", "recusal", "review", "disposition")}


# ---------------------------------------------------------------------------
# 采证对端客户端（处理 commit_disconnect：断连后按操作 ID 查回报/幂等重提）
# ---------------------------------------------------------------------------


# 常见方法名别名（Python 调用方/RPC 都可能使用这些命名）
Desk.load_registers = Desk.import_registers
Desk.load_fixtures = Desk.import_registers
Desk.seed = Desk.import_registers
Desk.review_status = Desk.object_readiness
Desk.get_readiness = Desk.object_readiness
Desk.readiness = Desk.object_readiness
Desk.object_review = Desk.object_readiness
Desk.donor_overview = Desk.donor_view
Desk.dispatch_overview = Desk.donor_view
Desk.recipient_overview = Desk.recipient_view
Desk.worker_tasks = Desk.worker_workbench
Desk.my_tasks = Desk.worker_workbench
Desk.actionable = Desk.worker_workbench
Desk.pull_evidence = Desk.fetch_partner_evidence
Desk.report_late_recusal = Desk.report_recusal


class PartnerClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8765", *, timeout: float = 3.0,
                 retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _conn(self):
        parsed = urlparse(self.base_url)
        return HTTPConnection(parsed.hostname, parsed.port or 80, timeout=self.timeout)

    def get_operation(self, operation_id: str) -> dict | None:
        conn = self._conn()
        try:
            conn.request("GET", f"/operations/{operation_id}")
            resp = conn.getresponse()
            body = resp.read()
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise PartnerUnavailable(f"对端查询返回 {resp.status}")
            return json.loads(body.decode("utf-8"))
        finally:
            conn.close()

    def _post_once(self, operation_id: str, operation: str, request: dict):
        conn = self._conn()
        try:
            body = json.dumps({"id": operation_id, "operation": operation,
                               "request": request}, ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/operations", body,
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, json.loads(data.decode("utf-8")) if data else {}
        finally:
            conn.close()

    def submit(self, operation: str, request: dict, operation_id: str) -> dict:
        """提交对端操作；遇到“已接单但断连”时用同一 ID 拉取/重放回报。"""
        last_exc = None
        for attempt in range(self.retries):
            try:
                status, body = self._post_once(operation_id, operation, request)
                if status == 200:
                    return body["reply"]
                if status == 409:
                    raise Conflict("对端操作 ID 被复用为不同内容",
                                   code="operation_id_conflict", detail=body)
                if status == 422:
                    raise ValidationFailed("对端没有匹配的联调样例",
                                           code="unsupported_example", detail=body)
                last_exc = PartnerUnavailable(f"对端返回 {status}: {body}")
            except (RemoteDisconnected, ConnectionError, OSError, TimeoutError) as exc:
                last_exc = exc
                # 可能已经 commit_disconnect：先查，查不到再幂等重提
                reply = self._poll_until_known(operation_id)
                if reply is not None:
                    return reply
        # 最后再查一次
        reply = self._poll_until_known(operation_id)
        if reply is not None:
            return reply
        raise PartnerUnavailable("采证对端不可达且操作回报未知",
                                 detail={"operation_id": operation_id,
                                         "error": str(last_exc)})

    def _poll_until_known(self, operation_id: str, *, rounds: int = 3) -> dict | None:
        import time
        for _ in range(rounds):
            try:
                body = self.get_operation(operation_id)
                if body and "reply" in body:
                    return body["reply"]
            except (ConnectionError, OSError, RemoteDisconnected):
                pass
            time.sleep(0.05)
        return None


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


ROUTES = []


def route(method: str, pattern: str):
    if "(?P<" in pattern:
        source = "^" + pattern + "$"
    else:
        source = "^" + pattern.replace("<", "(?P<").replace(">", "[^/]+)") + "$"
    rx = re.compile(source)

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


class DeskRequestHandler(BaseHTTPRequestHandler):
    server_version = "RegionalDesk/1.0"

    # 让 ThreadingHTTPServer 持有的 desk 实例可被 handler 访问
    @property
    def desk(self) -> Desk:
        return self.server.desk  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        import os
        if os.environ.get("DESK_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _send(self, status: int, value):
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Worker-Id, X-District, X-As-Of, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, OPTIONS, DELETE, PATCH")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, status: int, html: str):
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self._send(204, {})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValidationFailed("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailed("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return data

    def _context_payload(self, body: dict, query: dict) -> dict:
        payload = dict(self._unwrap_envelope(body))
        for key, value in query.items():
            payload.setdefault(key, value[0])
        if self.headers.get("X-Worker-Id"):
            payload.setdefault("actor", self.headers["X-Worker-Id"])
            payload.setdefault("worker_id", self.headers["X-Worker-Id"])
        if self.headers.get("X-District"):
            payload.setdefault("district", self.headers["X-District"])
        if self.headers.get("X-As-Of"):
            payload.setdefault("at", self.headers["X-As-Of"])
        return payload

    WRAPPER_KEYS = ("mandate", "commission", "worker", "check", "task",
                    "signature", "recusal", "disposition", "dispatch",
                    "arrival", "replacement", "handover", "case", "payload",
                    "params", "arguments", "args", "input", "body", "data",
                    "request")
    ENVELOPE_META = {"actor", "at", "as_of", "request_id", "idempotency_key",
                     "dry_run", "strict"}

    def _unwrap_envelope(self, body: dict) -> dict:
        """兼容 ``{"mandate": {...}}`` 这类单层信封；多键台账正文不动。"""
        if not isinstance(body, dict):
            return {}
        for key in self.WRAPPER_KEYS:
            inner = body.get(key)
            if isinstance(inner, dict):
                others = set(body) - {key}
                if not others or others <= self.ENVELOPE_META:
                    merged = {k: v for k, v in body.items() if k != key}
                    merged.update(inner)
                    return merged
        return body

    SINGULAR_RESOURCES = {
        "worker": "workers", "mandate": "mandates", "commission": "mandates",
        "check": "checks", "task": "checks", "case": "cases", "object": "objects",
        "recusal": "recusals", "disposition": "dispositions",
        "signature": "signatures", "dispatch": "dispatches", "arrival": "arrivals",
        "handover": "handovers", "replacement": "replacements", "action": "actions",
        "district": "districts", "donor": "donors", "review": "reviews",
        "assignment": "assignments",
    }

    @classmethod
    def _normalize_path(cls, path: str) -> str:
        path = re.sub(r"^/(?:api/v\d+|regional-desk|regional_desk|desk-api|"
                      r"coordination)(?=/|$)", "", path)
        if not path:
            path = "/"
        match = re.match(r"^/([A-Za-z_-]+)(/.*)?$", path)
        if match and match.group(1) in cls.SINGULAR_RESOURCES:
            rest = match.group(2) or ""
            path = "/" + cls.SINGULAR_RESOURCES[match.group(1)] + rest
        return path

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path.rstrip("/") or "/")
        query = parse_qs(parsed.query)
        try:
            body = self._read_json() if method in ("POST", "PATCH", "PUT") else {}
            payload = self._context_payload(body, query)
            self.desk._request_as_of = parse_ts(self.headers.get("X-As-Of")) \
                if self.headers.get("X-As-Of") else None

            if method == "GET" and path in ("/", "/health", "/healthz",
                                            "/healthcheck", "/status", "/ping",
                                            "/api/health", "/api/healthz"):
                if path == "/" and "text/html" in self.headers.get("Accept", ""):
                    self._serve_ui()
                    return
                self._send(200, {"ok": True, "service": "regional-desk",
                                 "time": iso(now_cst()),
                                 "endpoints": "/api/actions, /ui, /docs"})
                return
            if method == "GET" and path == "/ui":
                self._serve_ui()
                return
            if method == "GET" and path == "/docs":
                self._send(200, {"readme": "见仓库 README.md 与 API.md",
                                 "actions": sorted(ACTION_ALIASES)})
                return

            for http_method, rx, fn in ROUTES:
                if http_method != method:
                    continue
                match = rx.match(path)
                if match:
                    kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
                    result = fn(self, payload, query, **kwargs)
                    status = 201 if method == "POST" and isinstance(result, dict) \
                        and result.get("_created") else 200
                    if isinstance(result, tuple):
                        result, status = result
                    self._send(status, result)
                    return

            # 通用 RPC：POST /api/<action> 或 /rpc {"action": "..."}
            if method == "POST" and path in ("/api", "/rpc", "/api/rpc",
                                              "/invoke", "/api/invoke", "/action"):
                action = payload.get("action") or payload.get("method") \
                    or payload.get("operation")
                self._send(200, self._invoke_action(action, payload))
                return
            if method == "POST":
                m = re.match(r"^/(?:api|rpc|actions?)/([A-Za-z_][A-Za-z0-9_]*)$", path)
                if m:
                    self._send(200, self._invoke_action(m.group(1), payload))
                    return
            if method == "GET":
                m = re.match(r"^/api/([A-Za-z_][A-Za-z0-9_]*)$", path)
                if m:
                    name = m.group(1)
                    resolved = ACTION_ALIASES.get(name, name)
                    for candidate in ("q_" + name, name, resolved):
                        fn = getattr(self.desk, candidate, None)
                        if callable(fn) and not candidate.startswith("_"):
                            self._send(200, fn(payload))
                            return
                    raise NotFound(f"未知查询 {name}", code="action_not_found")
            self._send(404, {"ok": False, "error": "route_not_found", "path": path})
        except DeskError as exc:
            self._send(exc.http_status, exc.to_body())
        except Exception as exc:  # noqa: BLE001 - 服务端兜底，返回 JSON
            self._send(500, {"ok": False, "error": "internal_error",
                             "message": str(exc)})

    def _invoke_action(self, action: str | None, payload: dict):
        if not action:
            raise ValidationFailed("缺少 action")
        # 兼容 {"action": "...", "params"/"request": {...}} 一类 RPC 信封
        args = payload
        for container in ("params", "arguments", "args", "input", "payload",
                          "request"):
            inner = payload.get(container)
            if isinstance(inner, dict):
                merged = dict(inner)
                for key, value in payload.items():
                    if key not in ("action", "method", container):
                        merged.setdefault(key, value)
                args = merged
                break
        resolved = ACTION_ALIASES.get(action, action)
        fn = getattr(self.desk, resolved, None)
        if fn is None or resolved.startswith("_"):
            raise NotFound(f"未知操作 {action}", code="action_not_found")
        return fn(args)

    def _serve_ui(self):
        ui_path = ROOT / "static" / "index.html"
        if ui_path.exists():
            self._send_html(200, ui_path.read_text(encoding="utf-8"))
        else:
            self._send_html(200, "<h1>区域联动台</h1><p>API 服务正常。</p>")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")


# ---- 具体 REST 路由（薄封装，全部规则在 Desk） ---------------------------

@route("GET", "/districts")
def _list_districts(h: DeskRequestHandler, p, q):
    return {"districts": h.desk._list("district")}


@route("POST", "/districts")
def _add_district(h, p, q):
    return h.desk.add_district(p), 201


@route("GET", "/districts/(?P<district_id>[^/]+)")
def _district_detail(h, p, q, district_id):
    doc = h.desk._get("district", district_id) or {"id": district_id}
    workers = h.desk.list_workers(district=district_id)
    return {"district": doc, "worker_count": len(workers), "workers": workers}


@route("GET", "/workers")
def _workers(h, p, q):
    return {"workers": h.desk.list_workers(district=p.get("district"))}


@route("POST", "/workers")
def _upsert_worker(h, p, q):
    return h.desk.upsert_worker(p), 201


@route("GET", "/workers/(?P<worker_id>[^/]+)")
def _get_worker(h, p, q, worker_id):
    return h.desk.get("worker", worker_id)


@route("GET", "/workers/(?P<worker_id>[^/]+)/workbench")
@route("GET", "/workers/(?P<worker_id>[^/]+)/tasks")
@route("GET", "/workers/(?P<worker_id>[^/]+)/actionable")
def _workbench(h, p, q, worker_id):
    p["worker_id"] = worker_id
    return h.desk.worker_workbench(p)


@route("POST", "/workers/(?P<worker_id>[^/]+)/suspend")
def _suspend(h, p, q, worker_id):
    p["worker_id"] = worker_id
    return h.desk.suspend_worker(p)


@route("POST", "/workers/(?P<worker_id>[^/]+)/resume")
def _resume(h, p, q, worker_id):
    p["worker_id"] = worker_id
    return h.desk.resume_worker(p)


@route("GET", "/donors/(?P<district>[^/]+)")
@route("GET", "/districts/(?P<district>[^/]+)/donor-view")
@route("GET", "/dispatch/(?P<district>[^/]+)")
def _donor_view(h, p, q, district):
    return h.desk.donor_view({"district": district, **p})


@route("GET", "/recipient/(?P<district>[^/]+)")
def _recipient(h, p, q, district):
    return h.desk.recipient_view({"district": district, **p})


@route("GET", "/mandates")
@route("GET", "/commissions")
def _mandates(h, p, q):
    return {"mandates": h.desk.list_mandates(issuer=p.get("issuer"), donor=p.get("donor"))}


@route("POST", "/mandates")
@route("POST", "/commissions")
def _create_mandate(h, p, q):
    return h.desk.create_mandate(p), 201


@route("GET", "/mandates/(?P<mandate_id>[^/]+)")
@route("GET", "/commissions/(?P<mandate_id>[^/]+)")
def _get_mandate(h, p, q, mandate_id):
    return h.desk.get("mandate", mandate_id)


@route("POST", "/mandates/(?P<mandate_id>[^/]+)/close")
@route("POST", "/commissions/(?P<mandate_id>[^/]+)/close")
def _close_mandate(h, p, q, mandate_id):
    p["mandate_id"] = mandate_id
    return h.desk.close_mandate(p)


@route("GET", "/mandates/(?P<mandate_id>[^/]+)/readiness")
@route("GET", "/commissions/(?P<mandate_id>[^/]+)/readiness")
def _mandate_readiness(h, p, q, mandate_id):
    mandate = h.desk.get("mandate", mandate_id)
    objects = []
    for site in mandate.get("worksites", []):
        objects.append(h.desk.object_readiness({"object_id": site, **p}))
    return {"mandate": mandate, "objects": objects}


@route("POST", "/checks")
@route("POST", "/tasks")
def _create_check(h, p, q):
    return h.desk.create_check(p), 201


@route("GET", "/checks")
@route("GET", "/tasks")
def _list_checks(h, p, q):
    return {"checks": h.desk.list_checks(**{k: v for k, v in p.items()
                                            if k in {"worker", "assignee", "site",
                                                     "object_id", "mandate_id",
                                                     "status", "check_kind"}})}


@route("GET", "/checks/(?P<check_id>[^/]+)")
def _get_check(h, p, q, check_id):
    return h.desk.get("check", check_id)


@route("POST", "/checks/(?P<check_id>[^/]+)/claim")
def _claim(h, p, q, check_id):
    p["check_id"] = check_id
    return h.desk.claim_check(p)


@route("POST", "/checks/(?P<check_id>[^/]+)/submit")
@route("POST", "/checks/(?P<check_id>[^/]+)/records")
def _submit(h, p, q, check_id):
    p["check_id"] = check_id
    return h.desk.submit_to_check(p)


@route("POST", "/checks/(?P<check_id>[^/]+)/handover")
def _handover_one(h, p, q, check_id):
    p["check_id"] = check_id
    return h.desk.handover(p)


@route("POST", "/checks/(?P<check_id>[^/]+)/reopen")
def _reopen_one(h, p, q, check_id):
    p["check_id"] = check_id
    return h.desk.reopen_check(p)


@route("POST", "/claims")
def _claim_body(h, p, q):
    return h.desk.claim_check(p)


@route("POST", "/handovers")
@route("POST", "/handoff")
def _handover(h, p, q):
    return h.desk.handover(p)


@route("POST", "/replacements")
@route("POST", "/duty-replacements")
def _replace(h, p, q):
    return h.desk.replace_duty(p), 201


@route("GET", "/handovers")
def _list_handovers(h, p, q):
    return {"handovers": h.desk._list("handoff")}


@route("GET", "/assignments")
def _list_assignments(h, p, q):
    return {"assignments": h.desk.list_assignments()}


@route("POST", "/assignments")
def _save_assignment(h, p, q):
    return h.desk.save_assignment(p), 201


@route("POST", "/dispatches")
@route("POST", "/dispatch")
def _create_dispatch(h, p, q):
    return h.desk.create_dispatch(p), 201


@route("GET", "/dispatches")
def _list_dispatch(h, p, q):
    return {"dispatches": h.desk.list_dispatches(district=p.get("donor") or p.get("district"))}


@route("POST", "/arrivals")
@route("POST", "/workers/(?P<worker_id>[^/]+)/arrival")
def _arrival(h, p, q, worker_id=None):
    if worker_id:
        p["worker_id"] = worker_id
    return h.desk.record_arrival(p), 201


@route("GET", "/arrivals")
def _list_arrivals(h, p, q):
    return {"arrivals": h.desk.arrivals(worker_id=p.get("worker_id"),
                                        mandate_id=p.get("mandate_id"))}


@route("GET", "/evidence")
def _list_evidence(h, p, q):
    return {"evidence": h.desk.list_evidence(site=p.get("site"),
                                             object_id=p.get("object_id"))}


@route("POST", "/evidence")
def _add_evidence(h, p, q):
    return h.desk.add_evidence(p), 201


@route("POST", "/integrations/evidence")
@route("POST", "/partner/evidence")
@route("POST", "/evidence/fetch")
def _fetch_evidence(h, p, q):
    return h.desk.fetch_partner_evidence(p)


@route("GET", "/objects/(?P<object_id>[^/]+)")
def _object(h, p, q, object_id):
    return h.desk.object_readiness({"object_id": object_id, **p})


@route("GET", "/objects/(?P<object_id>[^/]+)/readiness")
@route("GET", "/objects/(?P<object_id>[^/]+)/review")
@route("GET", "/objects/(?P<object_id>[^/]+)/review-status")
def _object_readiness(h, p, q, object_id):
    return h.desk.object_readiness({"object_id": object_id, **p})


@route("POST", "/objects/(?P<object_id>[^/]+)/reviews")
def _start_review(h, p, q, object_id):
    rev = {
        "id": p.get("id") or f"review-{object_id}-{new_id('r')}",
        "kind": "review", "type": "manual_review",
        "object_id": object_id, "site": p.get("site", object_id),
        "case_id": p.get("case_id"), "status": "open",
        "reason": p.get("reason", "requested_by_recipient"),
        "created_at": iso(now_cst()),
    }
    h.desk._put("review", rev)
    return rev, 201


@route("GET", "/reviews")
def _reviews(h, p, q):
    return {"reviews": h.desk._list("review")}


@route("POST", "/cases")
def _create_case(h, p, q):
    return h.desk.create_case(p), 201


@route("GET", "/cases")
def _list_cases(h, p, q):
    return {"cases": h.desk._list("case")}


@route("GET", "/cases/(?P<case_id>[^/]+)")
def _get_case(h, p, q, case_id):
    return h.desk.get("case", case_id)


@route("GET", "/cases/(?P<case_id>[^/]+)/readiness")
def _case_readiness(h, p, q, case_id):
    case = h.desk.get("case", case_id)
    object_id = case.get("object_id") or case.get("site")
    return h.desk.object_readiness({"object_id": object_id, "case_id": case_id, **p})


@route("POST", "/signatures")
@route("POST", "/sign")
def _sign(h, p, q):
    return h.desk.sign(p), 201


@route("GET", "/signatures")
def _list_sigs(h, p, q):
    return {"signatures": h.desk._list("signature")}


@route("POST", "/recusals")
def _recusal(h, p, q):
    return h.desk.report_recusal(p), 201


@route("GET", "/recusals")
def _list_recusals(h, p, q):
    return {"recusals": h.desk._list("recusal")}


@route("GET", "/dispositions")
@route("GET", "/actions")
def _list_dispositions(h, p, q):
    return {"dispositions": h.desk.list_dispositions(site=p.get("site"),
                                                     status=p.get("status"))}


@route("POST", "/dispositions")
@route("POST", "/actions")
def _create_disposition(h, p, q):
    return h.desk.create_disposition(p), 201


@route("GET", "/dispositions/(?P<ident>[^/]+)")
def _get_disposition(h, p, q, ident):
    return h.desk.get("disposition", ident)


@route("POST", "/admin/registers/import")
@route("POST", "/registers/import")
@route("POST", "/fixtures/load")
def _import(h, p, q):
    registers = p.get("registers") if isinstance(p.get("registers"), dict) else p
    return h.desk.import_registers(registers)


@route("POST", "/admin/seed-fixtures")
def _seed_fixtures(h, p, q):
    path = ROOT / "fixtures" / "registers.json"
    registers = json.loads(path.read_text("utf-8"))
    if p.get("reset"):
        return h.desk.reset(registers)
    return h.desk.import_registers(registers)


@route("GET", "/fixtures/registers.json")
def _fixture_file(h, p, q):
    return json.loads((ROOT / "fixtures" / "registers.json").read_text("utf-8"))


@route("POST", "/admin/reset")
@route("POST", "/reset")
def _reset(h, p, q):
    registers = p.get("registers")
    return h.desk.reset(registers)


@route("POST", "/admin/clock")
def _clock(h, p, q):
    return h.desk.set_clock(p)


@route("GET", "/admin/state")
def _state(h, p, q):
    return h.desk.export_state()


@route("GET", "/audit")
def _audit(h, p, q):
    return {"audit": h.desk.audit_trail()}


# 操作名别名：让 RPC 风格 /api/<name> 也能覆盖常见命名
ACTION_ALIASES = {
    "readiness": "object_readiness",
    "review_status": "object_readiness",
    "get_readiness": "object_readiness",
    "object_review": "object_readiness",
    "workbench": "worker_workbench",
    "my_tasks": "worker_workbench",
    "actionable": "worker_workbench",
    "todo": "worker_workbench",
    "donor_overview": "donor_view",
    "dispatch_view": "donor_view",
    "recipient_overview": "recipient_view",
    "review_object": "object_readiness",
    "ingest_evidence": "add_evidence",
    "evidence": "fetch_partner_evidence",
    "partner_evidence": "fetch_partner_evidence",
    "pull_evidence": "fetch_partner_evidence",
    "replace": "replace_duty",
    "replacement": "replace_duty",
    "assign": "create_check",
    "dispatch": "create_dispatch",
    "check_in": "record_arrival",
    "arrival": "record_arrival",
    "import_registers": "import_registers",
    "seed": "import_registers",
    "load_fixtures": "import_registers",
}


# ---------------------------------------------------------------------------
# 服务器装配 / CLI
# ---------------------------------------------------------------------------


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, desk: Desk):
        self.desk = desk
        super().__init__(address, DeskRequestHandler)


def create_server(host: str = "127.0.0.1", port: int = 8000, *,
                  db_path: str | Path = ":memory:", desk: Desk | None = None,
                  partner_url: str | None = None,
                  seed_registers: str | Path | dict | None = None) -> DeskServer:
    if desk is None:
        desk = Desk(db_path, partner_url=partner_url)
        if seed_registers is not None:
            if isinstance(seed_registers, (str, Path)):
                seed_registers = json.loads(Path(seed_registers).read_text("utf-8"))
            desk.import_registers(seed_registers)
    return DeskServer((host, int(port)), desk)


def create_app(*args, **kwargs) -> DeskServer:
    """``create_server`` 的常见别名。"""
    return create_server(*args, **kwargs)


def run_server(host: str = "127.0.0.1", port: int = 8000, **kwargs):
    server = create_server(host, port, **kwargs)
    print(f"区域联动台服务监听 http://{host}:{server.server_port}", flush=True)
    server.serve_forever()
    return server


# 供 ``from regional_desk import app`` 一类引用：app 是服务工厂
app = create_server
application = DeskRequestHandler
Server = DeskServer
Handler = DeskRequestHandler
RequestHandler = DeskRequestHandler


def _default_db_path() -> Path:
    import os
    return Path(os.environ.get("DESK_DB", ROOT / "regional_desk.sqlite3"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="区域联动台服务端")
    parser.add_argument("port_pos", nargs="?", type=int, help="端口（也可用 --port）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(__import__("os").environ.get("PORT", "8000")))
    parser.add_argument("--db", default=str(_default_db_path()),
                        help="SQLite 台账路径；:memory: 表示内存")
    parser.add_argument("--partner", default=None, help="采证对端地址")
    parser.add_argument("--seed", default=str(ROOT / "fixtures" / "registers.json"),
                        help="启动时导入的台账 JSON；传空字符串可关闭")
    parser.add_argument("--empty", action="store_true", help="不自动导入 fixtures 台账")
    args = parser.parse_args(argv)
    if args.port_pos:
        args.port = args.port_pos

    seed = None
    if not args.empty and args.seed and Path(args.seed).exists():
        seed = args.seed
    server = create_server(args.host, args.port, db_path=args.db,
                           partner_url=args.partner, seed_registers=seed)
    print(f"区域联动台服务监听 http://{args.host}:{server.server_port}", flush=True)
    print(f"台账数据库: {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.desk.close()


if __name__ == "__main__":
    main()
