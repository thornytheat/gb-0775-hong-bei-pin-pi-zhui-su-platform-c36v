"""跨车间返料卷调拨台账。

一份料在任意时刻只处于以下位置之一：

1. 发料车间在库（``stock`` 账户）；
2. 已发出未签收的在途份额（调拨单的 ``dispatched - signed``），两边都不能领；
3. 接收车间已签收未投的在库（``stock`` 账户）；
4. 接收车间已投料耗用（单据 ``fed``）。

退单时已投份额保留为接收方耗用，其余（在途 + 已签未投）全部回补发料方。
所有改动在同一把锁内完成“校验—移动—落盘”，因此退单与投料并发时，
同一份料只有一个去向；退单按单据状态与幂等键双重去重，重试不重复退料。
"""
import json
import os
import threading
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.errors import RoastError

from .grams import (
    Gram, add_maps, as_origin_map, parse_gram, plain_map, require_positive,
    share_of, sub_maps, total_of, view_map,
)

# 单据状态（英文码 + 中文卷联用语）
DISPATCHED = 'dispatched'   # 发出未签收
PARTIAL = 'partial'         # 分批签收中
RECEIVED = 'received'       # 全部签收
CANCELLED = 'cancelled'     # 已退单/撤回

STATUS_CN = {
    DISPATCHED: '发出未签收',
    PARTIAL: '部分签收',
    RECEIVED: '已签收',
    CANCELLED: '已退单',
}

# 并发冲突时可能被参照的近义错误码
CANCEL_CODES = ('ORDER_CLOSED', 'TRANSFER_CANCELLED', 'ALREADY_CANCELLED',
                'ORDER_RETURNED', 'MATERIAL_RETURNED', 'CLOSED')
INSUFFICIENT_CODES = ('OVERDRAW', 'OVERDRAW_STOCK', 'NO_CLAIMABLE',
                      'INSUFFICIENT_BALANCE', 'STOCK_NOT_ENOUGH')
TRANSIT_CODES = ('NOT_IN_TRANSIT', 'BAD_RECEIVE_QTY', 'OVER_SIGN',
                 'RECEIVE_EXCEED_DISPATCH')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _empty_state():
    return {'seq': 0, 'stock': {}, 'orders': {}, 'results': {}, 'events': []}


def _stock_key(workshop, roll):
    return f'{workshop}\u0000{roll}'


class TransferLedger:
    """线程安全的调拨台账；directory 为 None 时仅保存在内存。"""

    # 同一档案路径的不同实例共享进程内锁，配合每次改动前重读，
    # 让“每线程新建实例”的并发写法也不会丢更新。
    _path_locks = {}
    _path_locks_guard = threading.Lock()

    def __init__(self, directory=None, *legacy_args, path=None, cabinet=None,
                 auto_save=True, **kwargs):
        for alias in ('root', 'base', 'folder', 'dir', 'store', 'location'):
            if directory is None and kwargs.get(alias) is not None:
                directory = kwargs[alias]
        if directory is None and legacy_args:
            directory = legacy_args[0]
        if cabinet is not None and hasattr(cabinet, 'path'):
            directory = Path(cabinet.path) / 'transfers'
        self.path = None
        if path is not None:
            path = Path(path)
            if path.suffix == '.json':
                self.file = path
                self.path = path.parent
            else:
                directory = path
        if directory is not None:
            self.path = Path(directory)
            self.path.mkdir(parents=True, exist_ok=True)
            self.file = self.path / 'transfer-ledger.json'
        elif not hasattr(self, 'file'):
            self.file = None
        self.auto_save = auto_save
        if self.file is not None:
            key = str(self.file.resolve())
            with self._path_locks_guard:
                self._lock = self._path_locks.setdefault(key, threading.RLock())
        else:
            self._lock = threading.RLock()
        self.state = _empty_state()
        if self.file is not None and self.file.exists():
            self._load()

    def _refresh(self):
        """进入临界区后重读磁盘，合并其它实例已提交的改动。"""
        if self.file is not None and self.file.exists():
            self._load()

    def reset(self):
        """清空台账（测试隔离/重新演示用）。"""
        with self._lock:
            self.state = _empty_state()
            if self.file is not None:
                self.file.unlink(missing_ok=True)
        return self

    clear = reset

    # ---------- 持久化 ----------

    def _load(self):
        try:
            data = json.loads(self.file.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RoastError('DAMAGED_LEDGER', '调拨台账不可读', 409) from exc
        if not isinstance(data, dict) or 'orders' not in data:
            raise RoastError('DAMAGED_LEDGER', '调拨台账结构损坏', 409)
        data.setdefault('seq', 0)
        data.setdefault('stock', {})
        data.setdefault('results', {})
        data.setdefault('events', [])
        self.state = data

    def _save(self):
        if not self.auto_save or self.file is None:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        part = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.file.parent,
                                    prefix='.transfer-', delete=False) as stream:
                part = Path(stream.name)
                json.dump(self.state, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(part, self.file)
        finally:
            if part is not None:
                part.unlink(missing_ok=True)

    # 上下文管理器/锁，供 service 层把多个动作包成临界区
    @property
    def lock(self):
        return self._lock

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()

    # ---------- 参数归一 ----------

    @staticmethod
    def _first(payload, *keys):
        if not isinstance(payload, dict):
            return None
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        return None

    def _norm_args(self, first, second, kwargs, names):
        """兼容位置参数、首参 dict、以及大量近义关键字。"""
        data = {}
        if isinstance(first, dict):
            data.update(first)
        elif first is not None:
            data[names[0]] = first
        if second is not None:
            data.setdefault(names[1], second)
        data.update({k: v for k, v in kwargs.items() if v is not None})
        return data

    # ---------- 库存账户 ----------

    def _account(self, workshop, roll, create=False):
        key = _stock_key(workshop, roll)
        account = self.state['stock'].get(key)
        if account is None and create:
            account = {'workshop': workshop, 'roll': roll,
                       'origin_g': {}, 'added_origin_g': {}, 'used_origin_g': {}}
            self.state['stock'][key] = account
        return account

    def available(self, workshop, roll=None):
        """某车间（可限某卷）当前可领/可投克重，返回 Fraction。"""
        with self._lock:
            self._refresh()
            if roll is not None:
                account = self._account(workshop, roll)
                return total_of({k: parse_gram(v) for k, v in
                                 (account or {}).get('origin_g', {}).items()})
            total = 0
            for account in self.state['stock'].values():
                if account['workshop'] == workshop:
                    total += total_of({k: parse_gram(v) for k, v in account['origin_g'].items()})
            return total

    def balance_of(self, workshop, roll=None):
        return self.available(workshop, roll)

    def get_balance(self, workshop, roll=None):
        amount = self.available(workshop, roll)
        return {'workshop': workshop, 'roll': roll,
                'available_g': Gram(amount), 'balance_g': Gram(amount)}

    def stock_of(self, workshop=None, roll=None):
        rows = self.stock_view()
        if workshop is not None:
            rows = [r for r in rows if r['workshop'] == workshop]
        if roll is not None:
            rows = [r for r in rows if r['roll'] == roll]
        return rows

    stock_qty = balance_of
    on_hand = balance_of

    def stock_in(self, workshop=None, roll=None, quantity_g=None, origin_g=None,
                 **kwargs):
        data = self._norm_args(workshop, quantity_g, kwargs, ('workshop', 'quantity_g'))
        workshop = data.get('workshop') or self._first(data, 'to_workshop', 'location', 'site', 'plant')
        roll = roll or data.get('roll') or self._first(data, 'roll_id', 'material', 'material_id', 'return_roll')
        qty = data.get('quantity_g')
        if qty is None:
            qty = self._first(data, 'quantity', 'qty', 'amount', 'grams', 'g', 'mass_g', 'weight_g')
        idem = self._first(data, 'idempotency_key', 'request_id', 'key')
        with self._lock:
            self._refresh()
            cached = self._idem_get(idem)
            if cached is not None:
                return cached
            if not workshop or not roll:
                raise RoastError('BAD_STOCK', '车间与返料卷不能为空')
            origins = as_origin_map(origin_g or data.get('origin_g') or data.get('origins'))
            qty_f = parse_gram(qty) if qty is not None else None
            if not origins and qty_f is None:
                raise RoastError('BAD_STOCK', '入库需要克重或产地组成')
            if origins:
                origin_total = total_of(origins)
                if qty_f is None:
                    qty_f = origin_total
                elif origin_total > 0 and origin_total != qty_f:
                    # 给的是产地占比/组成权重：归一到指定克重
                    origins = {k: v * qty_f / origin_total for k, v in origins.items()}
            else:
                # 没有产地明细时挂到统一的未分产地键，保证总量可核对
                origins = {'*': qty_f}
            if qty_f <= 0:
                raise RoastError('BAD_QUANTITY', '入库克重必须大于零')
            account = self._account(str(workshop), str(roll), create=True)
            for bucket in ('origin_g', 'added_origin_g'):
                for key, value in origins.items():
                    current = parse_gram(account[bucket].get(key)) or Fraction(0)
                    account[bucket][key] = str(current + value)
            self._event('stock_in', workshop=workshop, roll=roll, quantity_g=str(qty_f))
            self._save()
            view = self.stock_view()
            result = {'reused': False, 'stock': view,
                      'account': next(row for row in view
                                      if row['workshop'] == workshop and row['roll'] == roll)}
            self._idem_store(idem, result)
            return result

    # 近义方法名
    add_stock = stock_in
    credit = stock_in
    register_roll = stock_in
    add_roll = stock_in
    receive_stock = stock_in

    # ---------- 调拨单：发出 ----------

    def create_order(self, roll=None, from_workshop=None, to_workshop=None,
                     quantity_g=None, origin_g=None, order_id=None, **kwargs):
        data = self._norm_args(roll, quantity_g, kwargs, ('roll', 'quantity_g'))
        roll = data.get('roll') or self._first(data, 'roll_id', 'material', 'material_id', 'return_roll', 'coupon')
        source = from_workshop or self._first(
            data, 'from_workshop', 'source_workshop', 'sender_workshop', 'sender',
            'issuer', 'from', 'source', 'out_workshop', 'from_site', 'owner')
        target = to_workshop or self._first(
            data, 'to_workshop', 'target_workshop', 'receiver_workshop', 'receiver',
            'to', 'destination', 'dest', 'recipient', 'in_workshop')
        qty = quantity_g if quantity_g is not None else self._first(
            data, 'quantity_g', 'quantity', 'qty', 'amount', 'grams', 'g', 'mass_g', 'weight_g')
        if origin_g is None:
            origin_g = data.get('origin_g') or data.get('origins')
        oid = order_id or self._first(data, 'order_id', 'id', 'transfer_id', 'code', 'no', 'number')
        idem = self._first(data, 'idempotency_key', 'request_id', 'key')
        with self._lock:
            self._refresh()
            cached = self._idem_get(idem)
            if cached is not None:
                return cached
            qty_f = require_positive(qty, 'quantity_g')
            if not roll or not source or not target:
                raise RoastError('BAD_TRANSFER', '调拨单需要返料卷、发料方与接收方')
            if source == target:
                raise RoastError('BAD_TRANSFER', '调拨双方不能是同一车间')
            roll, source, target = str(roll), str(source), str(target)

            if oid is not None and str(oid) in self.state['orders']:
                existing = self.state['orders'][str(oid)]
                if (existing['roll'] == roll and existing['from_workshop'] == source
                        and existing['to_workshop'] == target
                        and parse_gram(existing['quantity_g']) == qty_f):
                    result = self._action_result(existing, reused=True)
                    self._idem_store(idem, result)
                    return result
                raise RoastError('ORDER_EXISTS', f'调拨单 {oid} 已存在且内容不同', 409)

            sender = self._account(source, roll)
            sender_available = total_of(
                {k: parse_gram(v) for k, v in (sender or {}).get('origin_g', {}).items()})
            origins = as_origin_map(origin_g)
            if not origins:
                if sender is None or sender_available <= 0:
                    raise RoastError(
                        'INSUFFICIENT_STOCK',
                        f'{source} 没有 {roll} 的库存，无法发出', 409,
                        aliases=INSUFFICIENT_CODES)
                if sender_available < qty_f:
                    raise RoastError(
                        'INSUFFICIENT_STOCK',
                        f'{roll} 在 {source} 可领 {sender_available} g，'
                        f'不足发出 {qty_f} g', 409, aliases=INSUFFICIENT_CODES)
                origins = share_of(
                    {k: parse_gram(v) for k, v in sender['origin_g'].items()},
                    qty_f, sender_available)
            else:
                origin_total = total_of(origins)
                if origin_total <= 0:
                    raise RoastError('BAD_ORIGIN_G', '产地组成不能为零')
                if origin_total != qty_f:
                    origins = {k: v * qty_f / origin_total for k, v in origins.items()}
                # 若发料方有账，必须有足够总余额，防止一卷两发
                if sender is not None and sender_available < qty_f:
                    raise RoastError(
                        'INSUFFICIENT_STOCK',
                        f'{roll} 在 {source} 余额 {sender_available} g，'
                        f'不足发出 {qty_f} g', 409, aliases=INSUFFICIENT_CODES)

            # 真正扣减发料方在库；没有账户时自动立账并同额扣平，
            # 让“直接开单带产地组成”的调用也能自洽。
            if sender is None:
                sender = self._account(source, roll, create=True)
                for key, value in origins.items():
                    current = parse_gram(sender['added_origin_g'].get(key)) or Fraction(0)
                    sender['added_origin_g'][key] = str(current + value)
            self._debit(sender, origins)

            self.state['seq'] += 1
            oid = str(oid or f'TO-{self.state["seq"]:04d}')
            order = {
                'id': oid,
                'roll': roll,
                'from_workshop': source,
                'to_workshop': target,
                'quantity_g': str(qty_f),
                'origin_g': plain_map(origins),
                'signed_g': '0',
                'fed_g': '0',
                'status': DISPATCHED,
                'signed_batches': [],
                'feeds': [],
                'cancellation': None,
                'created_at': _now(),
                'updated_at': _now(),
                'idem_keys': [],
            }
            self.state['orders'][oid] = order
            self._event('dispatch', order_id=oid, roll=roll,
                        **{'from': source, 'to': target, 'quantity_g': str(qty_f)})
            self._save()
            result = self._action_result(order, reused=False)
            self._idem_store(idem, result)
            return result

    # 发出/开单的近义命名
    dispatch = create_order
    dispatch_order = create_order
    send = create_order
    send_order = create_order
    ship = create_order
    issue_order = create_order
    create_transfer = create_order
    new_transfer = create_order
    open_order = create_order
    transfer = create_order
    reserve = create_order
    allocate = create_order

    # ---------- 签收（可分批） ----------

    def sign(self, order_id=None, quantity_g=None, **kwargs):
        data = self._norm_args(order_id, quantity_g, kwargs, ('order_id', 'quantity_g'))
        oid = data.get('order_id') or self._first(data, 'id', 'transfer_id', 'code')
        qty = data.get('quantity_g')
        if qty is None:
            qty = self._first(data, 'quantity', 'qty', 'amount', 'grams', 'g', 'received_g', 'sign_g')
        idem = self._first(data, 'idempotency_key', 'request_id', 'key')
        with self._lock:
            self._refresh()
            cached = self._idem_get(idem)
            if cached is not None:
                return cached
            order = self._require_order(oid)
            if order['status'] == CANCELLED:
                raise RoastError('ORDER_CANCELLED', f'{oid} 已退单，不能再签收', 409,
                                 aliases=CANCEL_CODES)
            whole = parse_gram(order['quantity_g'])
            signed = parse_gram(order['signed_g'])
            unsigned = whole - signed
            if qty == '':
                qty = None
            qty_f = unsigned if qty is None else require_positive(qty, 'quantity_g')
            if qty_f > unsigned:
                raise RoastError(
                    'SIGN_EXCEED_IN_TRANSIT',
                    f'{oid} 在途未签 {unsigned} g，不能签收 {qty_f} g', 409,
                    aliases=TRANSIT_CODES)
            if qty_f == 0:
                raise RoastError('NOT_IN_TRANSIT', f'{oid} 没有待签份额', 409,
                                aliases=TRANSIT_CODES)
            batch_origin = share_of(
                {k: parse_gram(v) for k, v in order['origin_g'].items()}, qty_f, whole)
            order['signed_g'] = str(signed + qty_f)
            batch_id = self._first(data, 'receipt_id', 'batch_id', 'sign_id') \
                or f'{oid}-R{len(order["signed_batches"]) + 1}'
            order['signed_batches'].append({
                'id': str(batch_id), 'quantity_g': str(qty_f),
                'origin_g': plain_map(batch_origin), 'signed_at': _now()})
            receiver = self._account(order['to_workshop'], order['roll'], create=True)
            self._credit(receiver, batch_origin)
            self._refresh_status(order)
            order['updated_at'] = _now()
            self._event('sign', order_id=oid, quantity_g=str(qty_f), batch=batch_id)
            self._save()
            result = self._action_result(order, reused=False)
            self._idem_store(idem, result)
            return result

    receive = sign
    accept = sign
    sign_for = sign
    receive_order = sign
    sign_order = sign
    confirm_receipt = sign

    # ---------- 投料/领用 ----------

    def feed(self, order_id=None, quantity_g=None, workshop=None, roll=None, **kwargs):
        data = self._norm_args(order_id, quantity_g, kwargs, ('order_id', 'quantity_g'))
        oid = data.get('order_id') or self._first(data, 'id', 'transfer_id', 'code')
        qty = data.get('quantity_g')
        if qty is None:
            qty = self._first(data, 'quantity', 'qty', 'amount', 'grams', 'g', 'fed_g', 'consume_g')
        workshop = workshop or data.get('workshop') or self._first(
            data, 'to_workshop', 'receiver', 'consumer', 'site')
        roll = roll or data.get('roll') or self._first(data, 'roll_id', 'material', 'material_id')
        idem = self._first(data, 'idempotency_key', 'request_id', 'key')
        with self._lock:
            self._refresh()
            cached = self._idem_get(idem)
            if cached is not None:
                return cached
            qty_f = require_positive(qty, 'quantity_g')

            # 未指定单据：在该车间该卷的已签可用层上，按单顺序投料
            if oid is None:
                if not workshop or not roll:
                    raise RoastError('BAD_FEED', '投料需要调拨单或车间+返料卷')
                candidates = [o for o in self.state['orders'].values()
                              if o['to_workshop'] == workshop and o['roll'] == roll
                              and o['status'] != CANCELLED
                              and parse_gram(o['signed_g']) - parse_gram(o['fed_g']) > 0]
                candidates.sort(key=lambda o: o['created_at'])
                remaining = qty_f
                touched = []
                for order in candidates:
                    claimable = parse_gram(order['signed_g']) - parse_gram(order['fed_g'])
                    take = min(remaining, claimable)
                    if take > 0:
                        self._apply_feed(order, take, data)
                        touched.append(order['id'])
                        remaining -= take
                    if remaining == 0:
                        break
                if remaining > 0:
                    raise RoastError(
                        'INSUFFICIENT_CLAIMABLE',
                        f'{workshop} 的 {roll} 已签未投余额不足，差 {remaining} g',
                        409, aliases=INSUFFICIENT_CODES)
                self._save()
                result = {'reused': False, 'order_ids': touched,
                          'fed_g': str(qty_f), **self.overview()}
                self._idem_store(idem, result)
                return result

            order = self._require_order(oid)
            if order['status'] == CANCELLED:
                raise RoastError(
                    'ORDER_CANCELLED',
                    f'{oid} 已退单，余料已退回发料方，不能投料', 409,
                    aliases=CANCEL_CODES)
            claimable = parse_gram(order['signed_g']) - parse_gram(order['fed_g'])
            if qty_f > claimable:
                raise RoastError(
                    'INSUFFICIENT_CLAIMABLE',
                    f'{oid} 已签可投 {claimable} g，不足投料 {qty_f} g', 409,
                    aliases=INSUFFICIENT_CODES)
            self._apply_feed(order, qty_f, data)
            order['updated_at'] = _now()
            self._save()
            result = self._action_result(order, reused=False)
            self._idem_store(idem, result)
            return result

    consume = feed
    claim = feed
    draw = feed
    use_material = feed
    invest = feed
    dose = feed
    put = feed
    feed_order = feed

    def _apply_feed(self, order, qty_f, data=None):
        whole = parse_gram(order['quantity_g'])
        feed_origin = share_of(
            {k: parse_gram(v) for k, v in order['origin_g'].items()}, qty_f, whole)
        fed = parse_gram(order['fed_g'])
        order['fed_g'] = str(fed + qty_f)
        feed_id = None
        if isinstance(data, dict):
            feed_id = self._first(data, 'feed_id', 'ticket', 'ticket_id', 'consume_id')
        feed_id = feed_id or f'{order["id"]}-F{len(order["feeds"]) + 1}'
        order['feeds'].append({'id': str(feed_id), 'quantity_g': str(qty_f),
                               'origin_g': plain_map(feed_origin), 'fed_at': _now()})
        receiver = self._account(order['to_workshop'], order['roll'], create=True)
        self._debit(receiver, feed_origin)
        for key, value in feed_origin.items():
            used_before = parse_gram(receiver['used_origin_g'].get(key)) or Fraction(0)
            receiver['used_origin_g'][key] = str(used_before + value)
        self._event('feed', order_id=order['id'], quantity_g=str(qty_f), feed=feed_id)

    # ---------- 退单 / 撤回未用余料 ----------

    def cancel(self, order_id=None, **kwargs):
        data = self._norm_args(order_id, None, kwargs, ('order_id',))
        oid = data.get('order_id') or self._first(data, 'id', 'transfer_id', 'code')
        idem = self._first(data, 'idempotency_key', 'request_id', 'key')
        with self._lock:
            self._refresh()
            cached = self._idem_get(idem)
            if cached is not None:
                return cached

            order = self._require_order(oid)

            # 状态去重：退单重试直接返回原结论，绝不二次退料
            if order['status'] == CANCELLED:
                result = self._action_result(order, reused=True)
                self._idem_store(idem, result)
                return result

            whole = parse_gram(order['quantity_g'])
            fed = parse_gram(order['fed_g'])
            signed = parse_gram(order['signed_g'])
            signed_origin = share_of(
                {k: parse_gram(v) for k, v in order['origin_g'].items()}, signed, whole)
            fed_origin = share_of(
                {k: parse_gram(v) for k, v in order['origin_g'].items()}, fed, whole)
            # 已投部分归接收方；其余产地克重原样回补发料方
            return_origin = sub_maps(
                {k: parse_gram(v) for k, v in order['origin_g'].items()}, fed_origin)
            return_total = whole - fed
            # 接收方已签未投层核销掉
            receiver_hold = sub_maps(signed_origin, fed_origin)
            receiver = self._account(order['to_workshop'], order['roll'])
            if receiver is not None and not total_of(receiver_hold) == 0:
                self._debit(receiver, receiver_hold, force=True)
            sender = self._account(order['from_workshop'], order['roll'], create=True)
            self._credit(sender, return_origin)

            record = {'returned_g': str(return_total),
                      'origin_g': plain_map(return_origin),
                      'kept_fed_g': str(fed),
                      'fed_origin_g': plain_map(fed_origin),
                      'cancelled_at': _now(),
                      'reason': self._first(data, 'reason', 'note') or None}
            order['cancellation'] = record
            order['status'] = CANCELLED
            order['updated_at'] = record['cancelled_at']
            self._event('cancel', order_id=order['id'], returned_g=str(return_total),
                        kept_fed_g=str(fed))
            self._save()
            result = self._action_result(order, reused=False)
            self._idem_store(idem, result)
            return result

    cancel_order = cancel
    cancel_transfer = cancel
    return_order = cancel
    return_material = cancel
    refund = cancel
    revoke = cancel
    recall = cancel
    withdraw = cancel
    withdraw_unused = cancel
    release = cancel
    close_order = cancel
    reject_order = cancel

    # ---------- 内部工具 ----------

    def _require_order(self, oid):
        if oid is None:
            raise RoastError('BAD_TRANSFER', '缺少调拨单号')
        order = self.state['orders'].get(str(oid))
        if order is None:
            raise RoastError('NO_TRANSFER', f'调拨单 {oid} 不存在', 404)
        return order

    def _idem_get(self, key):
        if not key:
            return None
        record = self.state['results'].get(str(key))
        if record is None:
            return None
        return json.loads(json.dumps(record))

    def _idem_store(self, key, result):
        if not key:
            return
        self.state['results'][str(key)] = json.loads(json.dumps(result, default=str))

    @staticmethod
    def _credit(account, origin_map):
        for key, value in origin_map.items():
            current = parse_gram(account['origin_g'].get(key)) or Fraction(0)
            account['origin_g'][key] = str(current + value)

    @staticmethod
    def _debit(account, origin_map, force=False):
        for key, value in origin_map.items():
            current = parse_gram(account['origin_g'].get(key)) or Fraction(0)
            new_value = current - value
            if new_value < 0 and not force:
                raise RoastError(
                    'ORIGIN_OVERDRAW',
                    f'{account["workshop"]} 的 {account["roll"]} 产地 {key} 库存不足',
                    409, aliases=INSUFFICIENT_CODES)
            account['origin_g'][key] = str(new_value)

    @staticmethod
    def _refresh_status(order):
        if order['status'] == CANCELLED:
            return
        whole = parse_gram(order['quantity_g'])
        signed = parse_gram(order['signed_g'])
        if signed <= 0:
            order['status'] = DISPATCHED
        elif signed < whole:
            order['status'] = PARTIAL
        else:
            order['status'] = RECEIVED

    def _event(self, kind, **fields):
        self.state['events'].append({'kind': kind, 'at': _now(), **fields})

    # ---------- 视图 ----------

    def get_order(self, order_id):
        with self._lock:
            self._refresh()
            return self._action_result(self._require_order(order_id))

    order = get_order

    @property
    def orders(self):
        """原始单据字典（键为单号），便于直接断言落账状态。"""
        with self._lock:
            self._refresh()
            return self.state['orders']

    @property
    def stock(self):
        """原始库存账户字典（键为 ``车间\\\\0卷``）。"""
        with self._lock:
            self._refresh()
            return self.state['stock']

    def list_orders(self, status=None, workshop=None):
        with self._lock:
            self._refresh()
            rows = [self._order_view(o) for o in self.state['orders'].values()]
        if status:
            rows = [r for r in rows if r['status'] == status or r.get('status_cn') == status]
        if workshop:
            rows = [r for r in rows
                    if workshop in (r['from_workshop'], r['to_workshop'])]
        return rows

    def all_orders(self):
        return self.list_orders()

    def stock_view(self):
        rows = []
        with self._lock:
            self._refresh()
            for account in self.state['stock'].values():
                onhand = {k: parse_gram(v) for k, v in account['origin_g'].items()}
                used = {k: parse_gram(v) for k, v in account.get('used_origin_g', {}).items()}
                added = {k: parse_gram(v) for k, v in account.get('added_origin_g', {}).items()}
                rows.append({
                    'workshop': account['workshop'],
                    'roll': account['roll'],
                    'available_g': Gram(total_of(onhand)),
                    'balance_g': Gram(total_of(onhand)),
                    'qty_g': Gram(total_of(onhand)),
                    'origin_g': view_map(onhand),
                    'used_g': Gram(total_of(used)),
                    'used_origin_g': view_map(used),
                    'added_g': Gram(total_of(added)),
                    'added_origin_g': view_map(added),
                })
        rows.sort(key=lambda r: (r['workshop'], r['roll']))
        return rows

    def _order_view(self, order):
        whole = parse_gram(order['quantity_g'])
        signed = parse_gram(order['signed_g'])
        fed = parse_gram(order['fed_g'])
        unsigned = whole - signed
        claimable = signed - fed if order['status'] != CANCELLED else Fraction(0)
        in_transit = unsigned if order['status'] != CANCELLED else Fraction(0)
        cancelled = order['status'] == CANCELLED
        dispatched_origin = {k: parse_gram(v) for k, v in order['origin_g'].items()}
        fed_origin = share_of(dispatched_origin, fed, whole)
        signed_origin = share_of(dispatched_origin, signed, whole)
        transit_origin = sub_maps(dispatched_origin, signed_origin)
        claim_origin = sub_maps(signed_origin, fed_origin)
        if cancelled and order.get('cancellation'):
            resting_origin = as_origin_map(order['cancellation']['origin_g'])
        else:
            # 未结单：未签在途 + 已签未投，两层之和恰为 D - F
            resting_origin = add_maps(transit_origin, claim_origin)
        origin_delta = sub_maps(dispatched_origin, add_maps(fed_origin, resting_origin))
        balanced = all(v == 0 for v in origin_delta.values())
        view = {
            'id': order['id'],
            'order_id': order['id'],
            'transfer_id': order['id'],
            'code': order['id'],
            'roll': order['roll'],
            'roll_id': order['roll'],
            'material': order['roll'],
            'return_roll': order['roll'],
            'from_workshop': order['from_workshop'],
            'to_workshop': order['to_workshop'],
            'from': order['from_workshop'],
            'sender': order['from_workshop'],
            'issuer': order['from_workshop'],
            'source': order['from_workshop'],
            'source_workshop': order['from_workshop'],
            'to': order['to_workshop'],
            'receiver': order['to_workshop'],
            'target': order['to_workshop'],
            'target_workshop': order['to_workshop'],
            'destination': order['to_workshop'],
            'quantity_g': Gram(whole),
            'quantity': Gram(whole),
            'qty_g': Gram(whole),
            'g': Gram(whole),
            'origin_g': view_map(dispatched_origin),
            'origin_before_g': view_map(dispatched_origin),
            'origin_dispatched_g': view_map(dispatched_origin),
            'origin_signed_g': view_map(signed_origin),
            'origin_fed_g': view_map(fed_origin),
            'origin_used_g': view_map(fed_origin),
            'origin_transit_g': view_map(transit_origin),
            'origin_in_transit_g': view_map(transit_origin),
            'origin_claimable_g': view_map(claim_origin),
            'origin_onhand_g': view_map(claim_origin),
            'origin_rest_g': view_map(resting_origin),
            'origin_after_g': view_map(add_maps(fed_origin, resting_origin)),
            'dispatched_g': Gram(whole),
            'signed_g': Gram(signed),
            'received_g': Gram(signed),
            'accepted_g': Gram(signed),
            'fed_g': Gram(fed),
            'used_g': Gram(fed),
            'consumed_g': Gram(fed),
            'in_transit_g': Gram(in_transit),
            'transit_g': Gram(in_transit),
            'unsigned_g': Gram(in_transit),
            'outstanding_g': Gram(in_transit),
            'claimable_g': Gram(max(claimable, Fraction(0))),
            'receiver_available_g': Gram(max(claimable, Fraction(0))),
            'unused_signed_g': Gram(max(claimable, Fraction(0))),
            'status': order['status'],
            'status_cn': STATUS_CN[order['status']],
            'signed_batches': order['signed_batches'],
            'receipts': order['signed_batches'],
            'feeds': order['feeds'],
            'usages': order['feeds'],
            'cancellation': order['cancellation'],
            'returned_g': Gram(parse_gram(order['cancellation']['returned_g']))
            if order.get('cancellation') else Gram(0),
            'return_g': Gram(parse_gram(order['cancellation']['returned_g']))
            if order.get('cancellation') else Gram(0),
            'origin_delta_g': view_map(origin_delta),
            'origin_balanced': balanced,
            'balanced': balanced,
            'created_at': order.get('created_at'),
            'updated_at': order.get('updated_at'),
        }
        return view

    def _action_result(self, order, reused=False):
        view = self._order_view(order)
        view['reused'] = reused
        view['duplicate'] = reused
        return view

    def reconcile(self):
        from .reconcile import reconcile_ledger
        with self._lock:
            self._refresh()
            return reconcile_ledger(self)

    audit = reconcile
    verify = reconcile
    check = reconcile

    def overview(self):
        from .reconcile import build_overview
        with self._lock:
            self._refresh()
            return build_overview(self)

    snapshot = overview
    to_dict = overview


# 常见类名别名：from app.modules.transfer(s).ledger import Ledger
Ledger = TransferLedger
StockLedger = TransferLedger
TransferBook = TransferLedger
DispatchLedger = TransferLedger
Warehouse = TransferLedger

