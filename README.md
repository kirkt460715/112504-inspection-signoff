# 跨县支援巡检的资格代派与联合复核

本地演练资料和对端协议桩使用合成编号。采证回执会标示记录的现场采集时间与收据号。其存在不提供任务派工或独立复核资格。

`fixtures/registers.json` 保存模拟业务台账。签字时间、回避关系起始时间及补报时间分列。现场取证与已签处置单可据对象编号建立关联。

`fixtures/partner-exchanges.json` 给出合成对端调用的请求和回报示例。每个条目独立，文件顺序没有业务含义。请求正文按匹配的 `operation` 和 `request` 选择示例。对端只模拟设备或合作方的应答，不处理业务服务的规则。

使用 Python 3.11.2 和标准库，无需安装依赖。运行以下命令核验台账与对端联调协议。

```sh
python3 -m unittest -v
python3 partner_stub.py --port 8765
```

## 区域联动台服务（跨县借人 / 现场分工 / 结论签认）

`regional_desk.py` 是业务服务端（SQLite 持久化 + JSON HTTP 接口），`server.py`、
`app.py`、`main.py`、`api.py`、`coordination_server.py` 等是指向同一实现的兼容入口。
接口与规则的完整说明见 [API.md](API.md)，浏览器演练台在服务启动后访问 `/ui`。

```sh
# 一键故事线自检：自动起采证对端桩，走完委托→领取→交接→断连采证恢复→
# 签署→迟报回避→指定对象复核→处置单需复议→持久化重开，共 19 条断言
python3 demo_scenario.py

# 手工联调：终端 A 起采证对端
python3 partner_stub.py --port 8765 --journal partner-journal.sqlite3
# 终端 B 起区域联动台（默认 http://127.0.0.1:8000 ，首次启动导入 fixtures 台账）
python3 regional_desk.py --port 8000
```

服务默认把台账写入 `regional_desk.sqlite3`（`--db :memory:` 可改为内存），
重启后委托、分工、交接、签名、回避影响和处置单状态都还在，可以直接接下一次任务。
所有资格/回避判断都可按事件自带时间（请求体 `at`/`signed_at`/`reported_at`…，
或 `X-As-Of` 头）重放，因此 fixtures 中 2028 年的合成案例不受宿主机时钟影响。

三个视角的入口：

- 受援县判断某对象复核是否齐备、尚缺什么：`GET /objects/culvert-4/readiness`
- 派出方查看本单位派工与交接：`GET /donors/district-q`
- 现场人员查看自己可领/可提交事项：`GET /workers/staff-2/workbench`

## 对端桩协议（原有联调资料）

对端只监听本机。向 `POST /operations` 提交 JSON 对象，含字符串 `id`、字符串 `operation` 和对象 `request`。首次接单记入对端日志，正常返回 200，格式为 `{"id": "...", "reply": {...}}`。条目配置为 `commit_disconnect` 时，接单会写入对端日志，但首次 HTTP 连接没有响应。按该 ID 调用 `GET /operations/<id>` 查询回报，未接单返回 404。同一个 ID 和内容的重试返回原回报，复用 ID 提交其他内容返回 409。无对应条目的请求返回 422。可通过 `--exchanges` 传入同格式的其他联调资料。

重启业务服务进行联调时可保持对端进程运行。若也需要重启桩，可通过 `--journal` 指定临时 SQLite 文件保留对端接单记录。
