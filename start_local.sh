#!/usr/bin/env bash
# 一键起本地采证对端(8765) + 区域联动台(默认 8000)。
# 用法：bash start_local.sh            # 前台运行联动台，Ctrl-C 一起退出
#       PORT=9000 bash start_local.sh
set -euo pipefail
cd "$(dirname "$0")"

PARTNER_PORT="${PARTNER_PORT:-8765}"
PORT="${PORT:-8000}"

python3 partner_stub.py --port "${PARTNER_PORT}" \
  --journal partner-journal.sqlite3 &
PARTNER_PID=$!
cleanup() { kill "${PARTNER_PID}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

# 等对端端口起来（最多约 5 秒）
python3 - "$PARTNER_PORT" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(50):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.2):
            sys.exit(0)
    except OSError:
        time.sleep(0.1)
sys.exit(1)
PY

exec python3 regional_desk.py --port "${PORT}" \
  --partner "http://127.0.0.1:${PARTNER_PORT}"
