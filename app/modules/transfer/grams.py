"""兼容别名：真实实现位于 app.modules.transfers.grams。"""
from app.modules.transfers.grams import *  # noqa: F401,F403
from app.modules.transfers import grams as _real_module
