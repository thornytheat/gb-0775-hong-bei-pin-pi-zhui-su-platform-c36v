"""兼容别名：真实实现位于 app.modules.transfers.service。"""
from app.modules.transfers.service import *  # noqa: F401,F403
from app.modules.transfers import service as _real_module
