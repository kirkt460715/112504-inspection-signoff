# 区域联动台服务端接口

仅依赖 Python 3.11 标准库。核心实现：`regional_desk.py`（SQLite 持久化 + 规则 + HTTP 服务）。
`server.py` / `app.py` / `main.py` / `api.py` / `coordination*.py` 等文件只是指向同一实现的兼容入口。

## 启动

```sh
# 1) 本地采证对端（另一个终端，默认 127.0.0.1:8765）
python3 partner_stub.py --port 8765 --journal partner-journal.sqlite3

# 2) 区域联动台（默认 127.0.0.1:8000，自动导入 fixtures/registers.json 到持久台账）
python3 regional_desk.py --port 8000
# 等价：python3 server.py / python3 app.py / python3 main.py

# 一键故事线自检（自动起对端桩与临时台账，19 条断言）
python3 demo_scenario.py

# 全部单元/HTTP 测试（含原有对端协议测试）
python3 -m unittest -v
```

常用参数：`--db regional_desk.sqlite3`（默认持久文件，可用 `:memory:`）、
`--partner http://127.0.0.1:8765`、`--empty`（不自动导入 fixture）、`PORT` 环境变量。
重启服务后 SQLite 台账仍在，即“下一次任务接着用”。

浏览器打开 <http://127.0.0.1:8000/ui> 是三个视角的本地演练台。

## 时间约定（重要）

所有判断都按**业务事件自带时间**重放，而不是服务器当前时钟：

- 可在请求体传 `at` / `as_of` / `timestamp`，或使用专用字段
  （`signed_at`、`claimed_at`、`reported_at`、`replacement_at`、`arrived_at` …）；
- 也可用请求头 `X-As-Of: 2028-07-03T10:00:00+08:00`；
- 纯日期（如 `2028-06-20`）按 +08:00 零点处理；省略时间才使用宿主机当前时间。

## 规则模型（与题目逐条对应）

1. **委托 mandate/commission**：`issuer`（受援县）、`donor`（派出县）、
   `worksites/sites`（地点）、`scopes/categories`（对象类别）、
   `allowed_actions/allowed_dispositions`（允许的处置）、`from/valid_from`、`to/valid_to`。
   领取/执行类动作必须落在时段与范围内；建单分工本身不被时段卡死。
2. **资质**：worker 的 `credential`/`credentials` + `valid_through` 等；
   `suspensions:[{from,until,reason}]` 或 `POST /workers/<id>/suspend` 表示临时停用。
   资质是否有效以**行为发生时刻**判定（到期/停用区间），历史结论不随后来到期而改变。
3. **回避**：`POST /recusals`，字段 `worker`、`site/object_id`、
   `relationship_since`、`reported_at`。
   - `reported_at <= signed_at`：签署时已有回避证据 → 签名直接判
     `signature_not_admissible`（`recusal_on_record_at_signing`）。
   - 事后补报且关系区间覆盖签署时刻：原签名不删除，置 `status:"affected"`，
     案件置 `needs_review`，生成指定对象的 `review` 程序，关联处置单置
     `status:"reconsideration_required"` 并带 `needs_reconsideration:true`。
   - 关系始于签署之后（不覆盖）：不影响旧结论。
4. **领取与替换**：`POST /checks/<id>/claim` 只允许 `unstarted` 事项，
   且资质覆盖类别、角色匹配、委托有效、无已报回避。已开始事项返回
   `409 check_already_started`。`POST /replacements` 只换值守名册，不划走在办事项；
   `POST /checks/<id>/handover` 才把在办事项连同记录移交，历史记录的 `by` 仍是原采集人。
5. **替补独立性**：对同一对象，承接了**未结主检**的人不能作独立复核签字
   （`substitute_dual_role`）；已作有效独立签字的人也不能再承接/签主检。
   主检已签完后的值守替换不构成“承接未结主检”，不影响此前的独立签字。
6. **采证**：`POST /evidence` 直接登记带 `captured_at` 与
   `signature_receipt` 的材料；`POST /integrations/evidence` 调对端 `evidence`
   操作，内置 `commit_disconnect` 恢复（断连后按同一操作 ID `GET /operations/<id>`
   拉回报，或幂等重提）。证据入库**不会**把任务改派给 observer，也**不会**
   充当/补齐独立复核签名。`GET /objects/<id>/readiness` 的 `evidence[].usable`
   给出“同对象已独立取得且仍有效”的记录（如 staff-3 的 check-32）。
7. **新委托不得复开旧范围**：`POST /checks/<id>/reopen` 校验委托仍 open、
   在有效期内且覆盖范围；旧委托过期/被 `replaces` 替代（superseded）时返回
   `old_mandate_closed`。用新委托打开不在其清单内的旧地点/类别同样被拒。
8. **处置边界**：处置单 `action_type/type` 必须属于委托 `allowed_actions`
   （未限定 `allowed_actions` 或未给类型时不限制）。

## 三个视角

| 角色 | 接口 |
| --- | --- |
| 派出方 | `GET /donors/<district>`（=`/districts/<d>/donor-view`、`/dispatch/<d>`）：本单位人员、在办、历史、交出/承接交接、派工单、到场 |
| 受援县 | `GET /recipient/<district>` 全县总览；`GET /objects/<object>/readiness`（别名 `/objects/<o>/review`、`/review-status`、`GET /mandates/<id>/readiness`）判断齐备性与 `missing/missing_labels_zh` |
| 现场人员 | `GET /workers/<id>/workbench`（别名 `/tasks`、`/actionable`、RPC `my_tasks/todo`）：`claimable` 可领、`submittable` 可提交、`blocked` 及原因 |

`readiness` 响应要点：`ready`、`missing`
（`qualified_lead_signature` / `independent_review_signature` /
`independent_roles_conflicted` / `conclude_initiated_review`）、
`requirements.lead.state` 与 `requirements.independent_review.state`
（`valid/missing/tainted/rejected`）、`open_reviews`、`evidence`、`dispositions`、
中文 `summary_zh`。

## 常用 REST 端点

```
GET  /health ; GET /ui ; GET /admin/state ; GET /audit
POST /admin/seed-fixtures [{"reset": true}]
POST /admin/registers/import        # body 即 registers.json 内容
POST /admin/reset
POST /admin/clock                   # {"now": "2028-07-04T09:00:00+08:00"}

GET/POST /districts ; GET/POST /workers
GET  /workers/<id> ; POST /workers ; POST /workers/<id>/suspend|resume|arrival
GET/POST /dispatches ; POST /arrivals ; GET /arrivals

GET/POST /mandates  (= /commissions) ; GET /mandates/<id> ; POST /mandates/<id>/close
POST /checks (=/tasks) ; GET /checks ; GET /checks/<id>
POST /checks/<id>/claim|submit|records|handover|reopen
POST /claims ; POST /handovers ; POST /replacements

GET/POST /evidence ; POST /integrations/evidence ; POST /partner/evidence
GET/POST /cases ; GET /cases/<id>
POST /signatures (=/sign) ; GET /signatures
POST /recusals ; GET /recusals
GET  /reviews ; POST /objects/<id>/reviews
GET/POST /dispositions (=/actions) ; GET /dispositions/<id>
```

### RPC 风格（动作名即 Desk 方法）

```sh
curl -s localhost:8000/rpc -d '{"action":"object_readiness","object_id":"culvert-4"}'
curl -s localhost:8000/api/worker_workbench -d '{"worker_id":"staff-2","at":"2028-07-03T10:00:00+08:00"}'
```

`POST /api/<method>`、`POST /rpc {"action": ...}` 会分派到 `Desk` 的公开方法；
常见别名：`readiness/review_status`、`workbench/my_tasks/todo/actionable`、
`donor_overview/dispatch_view`、`recipient_overview`、`seed/load_fixtures`、
`pull_evidence`、`replace`、`check_in` 等。

## curl 速览（启动并 seed 后）

```sh
curl -s localhost:8000/objects/culvert-4/readiness | python3 -m json.tool
curl -s localhost:8000/donors/district-q | python3 -m json.tool
curl -s 'localhost:8000/workers/staff-2/workbench?at=2028-07-03T10:00:00%2B08:00'
curl -s -X POST localhost:8000/integrations/evidence \
  -H 'Content-Type: application/json' \
  -d '{"site":"culvert-4","observer":"staff-3","submission":"check-32"}'
```

错误统一为 JSON：`{"ok": false, "error": "<code>", "message": "...", "detail": {...}}`，
状态码用语义化的 400/403/404/409/422/502。
