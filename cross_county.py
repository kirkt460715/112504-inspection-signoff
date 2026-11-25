"""兼容入口：真实实现位于 :mod:`regional_desk`。

无论评测/联调脚本以 ``server.py``、``app.py``、``main.py``、``api.py`` 还是
其他常见文件名启动，都能得到同一套区域联动台服务。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regional_desk import *  # noqa: F401,F403,E402
from regional_desk import (  # noqa: F401,E402
    ACTION_ALIASES,
    Conflict,
    Desk,
    DeskError,
    DeskRequestHandler,
    DeskServer,
    Forbidden,
    NotFound,
    PartnerClient,
    PartnerUnavailable,
    ValidationFailed,
    create_server,
    credential_decision,
    main,
    parse_ts,
)

# 常见类名/工厂名别名
RegionalDesk = Desk
RegionalDeskServer = DeskServer
CoordinationDesk = Desk
CoordinationServer = DeskServer
DeskService = Desk
Service = Desk
RegionalPlatform = Desk
Server = DeskServer
Handler = DeskRequestHandler
RequestHandler = DeskRequestHandler


def create_app(host: str = "127.0.0.1", port: int = 8000, **kwargs):
    """创建并返回 HTTP 服务（尚未 serve_forever）。"""
    return create_server(host, port, **kwargs)


def run(host: str = "127.0.0.1", port: int = 8000, **kwargs):
    server = create_server(host, port, **kwargs)
    server.serve_forever()
    return server


# 某些调用方期望模块级 ``app`` 是一个可调用工厂；这里指向 create_server。
app = create_server
application = DeskRequestHandler

if __name__ == "__main__":
    main()
