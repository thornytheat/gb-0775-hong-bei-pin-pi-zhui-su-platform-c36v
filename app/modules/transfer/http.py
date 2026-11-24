"""兼容别名：真实实现位于 app.modules.transfers.http。"""
from app.modules.transfers.http import *  # noqa: F401,F403
from app.modules.transfers import http as _real_module
