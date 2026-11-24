"""兼容别名：真实实现位于 app.modules.transfers.ledger。"""
from app.modules.transfers.ledger import *  # noqa: F401,F403
from app.modules.transfers import ledger as _real_module
