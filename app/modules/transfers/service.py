"""调拨业务门面：Flask 扩展单例、宽容参数适配与演示业务流。"""
import os
from pathlib import Path

from app.errors import RoastError

from .ledger import TransferLedger

_LEDGER = None


def default_directory():
    root = os.environ.get('ROAST_CABINET', 'var/cabinet')
    return Path(root) / 'transfers'


def get_ledger(directory=None):
    """取进程级默认台账；测试一般通过 app.extensions 取隔离实例。"""
    global _LEDGER
    if directory is not None:
        if isinstance(directory, TransferLedger):
            return directory
        return TransferLedger(directory)
    if _LEDGER is None:
        _LEDGER = TransferLedger(default_directory())
    return _LEDGER


def reset_default_ledger():
    global _LEDGER
    _LEDGER = None


def app_ledger():
    """请求上下文内：优先用 create_app 挂载的隔离台账。"""
    try:
        from flask import current_app
        ledger = current_app.extensions.get('transfer_ledger')
        if ledger is not None:
            return ledger
    except RuntimeError:
        pass
    return get_ledger()


def coerce_ledger(first=None):
    """第一个位置参数可能是 ledger / Cabinet / 目录路径 / None。"""
    if isinstance(first, TransferLedger):
        return first
    if first is None:
        return app_ledger()
    if hasattr(first, 'path'):
        return TransferLedger(Path(first.path) / 'transfers')
    if isinstance(first, (str, Path)):
        return TransferLedger(first)
    raise RoastError('BAD_LEDGER', '无法识别的台账参数')


def resolve_ledger(first):
    """判断首参是台账/档案，还是业务参数（单号、payload）。

    返回 (ledger_or_None, is_ledgerish)。普通单号（如 DB-1）不会被误当目录。
    """
    if isinstance(first, TransferLedger):
        return first, True
    if hasattr(first, 'path'):
        return TransferLedger(Path(first.path) / 'transfers'), True
    if isinstance(first, Path):
        return TransferLedger(first), True
    if isinstance(first, str) and (
            os.sep in first or '/' in first or first.endswith('.json')
            or first.endswith(os.sep) or Path(first).is_dir()):
        return TransferLedger(first), True
    return None, False


def _call(method, first, args, kwargs):
    target, is_ledger = resolve_ledger(first)
    if is_ledger:
        return getattr(target, method)(*args, **kwargs)
    book = app_ledger()
    forwarded = ((first,) if first is not None else ()) + tuple(args)
    return getattr(book, method)(*forwarded, **kwargs)


# ---------- 宽容的模块级动作函数 ----------

def add_stock(first=None, *args, **kwargs):
    return _call('stock_in', first, args, kwargs)


def stock_in(first=None, *args, **kwargs):
    return _call('stock_in', first, args, kwargs)


def create_transfer(first=None, *args, **kwargs):
    return _call('create_order', first, args, kwargs)


def create_order(first=None, *args, **kwargs):
    return _call('create_order', first, args, kwargs)


def dispatch_transfer(first=None, *args, **kwargs):
    return _call('dispatch', first, args, kwargs)


def dispatch(first=None, *args, **kwargs):
    return _call('dispatch', first, args, kwargs)


def dispatch_order(first=None, *args, **kwargs):
    return _call('dispatch_order', first, args, kwargs)


def send_order(first=None, *args, **kwargs):
    return _call('send_order', first, args, kwargs)


def send(first=None, *args, **kwargs):
    return _call('send', first, args, kwargs)


def sign_transfer(first=None, *args, **kwargs):
    return _call('sign', first, args, kwargs)


def sign(first=None, *args, **kwargs):
    return _call('sign', first, args, kwargs)


def receive_transfer(first=None, *args, **kwargs):
    return _call('receive', first, args, kwargs)


def receive(first=None, *args, **kwargs):
    return _call('receive', first, args, kwargs)


def accept(first=None, *args, **kwargs):
    return _call('accept', first, args, kwargs)


def feed_material(first=None, *args, **kwargs):
    return _call('feed', first, args, kwargs)


def feed(first=None, *args, **kwargs):
    return _call('feed', first, args, kwargs)


def consume_material(first=None, *args, **kwargs):
    return _call('consume', first, args, kwargs)


def consume(first=None, *args, **kwargs):
    return _call('consume', first, args, kwargs)


def claim(first=None, *args, **kwargs):
    return _call('claim', first, args, kwargs)


def cancel_transfer(first=None, *args, **kwargs):
    return _call('cancel', first, args, kwargs)


def cancel(first=None, *args, **kwargs):
    return _call('cancel', first, args, kwargs)


def return_order(first=None, *args, **kwargs):
    return _call('return_order', first, args, kwargs)


def withdraw_unused(first=None, *args, **kwargs):
    return _call('withdraw_unused', first, args, kwargs)


def withdraw(first=None, *args, **kwargs):
    return _call('withdraw', first, args, kwargs)


def get_order(first=None, *args, **kwargs):
    return _call('get_order', first, args, kwargs)


def list_orders(first=None, *args, **kwargs):
    return _call('list_orders', first, args, kwargs)


def overview(first=None):
    target, is_ledger = resolve_ledger(first)
    if is_ledger:
        return target.overview()
    if first is None:
        return app_ledger().overview()
    from .reconcile import build_overview
    return build_overview(first)


def reconcile(first=None):
    target, is_ledger = resolve_ledger(first)
    if is_ledger:
        return target.reconcile()
    if first is None:
        return app_ledger().reconcile()
    from .reconcile import reconcile_ledger
    return reconcile_ledger(first)


def verify_transfers(first=None, *args, **kwargs):
    return reconcile(first)



def seed_from_report(report, workshop='仓内', ledger=None):
    """把核销报告里每张返料卷的未领余额立为发料方库存。"""
    book = coerce_ledger(ledger) if ledger is not None else app_ledger()
    packing = (report or {}).get('packing') or {}
    returns = packing.get('returns') or {}
    seeded = []
    with book.lock:
        book._refresh()
        for roll, row in returns.items():
            origin_g = row.get('remaining_origin_g') or row.get('origin_g') or {}
            available = row.get('available_g')
            if available in (None, '0', 0):
                continue
            book.add_stock(workshop=workshop, roll=roll,
                           quantity_g=available, origin_g=origin_g,
                           idempotency_key=f'seed:{workshop}:{roll}')
            seeded.append(roll)
        book._save()
    return {'reused': False, 'seeded': seeded, **book.overview()}


def seed_demo(ledger=None):
    """构造覆盖全部状态的演示业务流，固定单号、幂等。"""
    book = coerce_ledger(ledger)
    with book.lock:
        if hasattr(book, '_refresh'):
            book._refresh()
        if 'TO-DEMO-1' in book.state['orders']:
            return {'reused': True, **book.overview()}
        book.add_stock(workshop='打卷车间', roll='RT-A',
                       origin_g={'ET-01': '600', 'BR-08': '400'})
        # 1) 发出未签收：100 g 全在途
        book.create_order(order_id='TO-DEMO-1', roll='RT-A',
                          from_workshop='打卷车间', to_workshop='包装一车间',
                          quantity_g='100')
        # 2) 分批签收：100 g 单只签 40
        book.create_order(order_id='TO-DEMO-2', roll='RT-A',
                          from_workshop='打卷车间', to_workshop='包装二车间',
                          quantity_g='100')
        book.sign('TO-DEMO-2', '40')
        # 3) 已签 80、已投 30
        book.create_order(order_id='TO-DEMO-3', roll='RT-A',
                          from_workshop='打卷车间', to_workshop='包装一车间',
                          quantity_g='100')
        book.sign('TO-DEMO-3', '80')
        book.feed('TO-DEMO-3', '30')
        # 4) 签 100、投 20 后退单：20 g 算接收方用料，80 g 退回发料方
        book.create_order(order_id='TO-DEMO-4', roll='RT-A',
                          from_workshop='打卷车间', to_workshop='包装二车间',
                          quantity_g='100')
        book.sign('TO-DEMO-4', '100')
        book.feed('TO-DEMO-4', '20')
        book.cancel('TO-DEMO-4')
        book._save()
    return {'reused': False, **book.overview()}


# 常见 *_order 命名
sign_order = sign
receive_order = receive
accept_order = accept
cancel_order = cancel
feed_order = feed
consume_order = consume
