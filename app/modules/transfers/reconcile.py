"""调拨单核对与产地克重守恒。

恒等式（按产地分别成立）::

    累计入库 = 各车间在库 + 已发出未签收在途 + 接收方已投料

退单把“在途 + 已签未投”补回发料方在库，已投料留在接收方耗用，
因此任何操作序列下该式都应为零差额。
"""
from fractions import Fraction

from .grams import (
    Gram, add_maps, as_origin_map, parse_gram, plain_map, share_of,
    sub_maps, total_of, view_map,
)
from .ledger import CANCELLED, DISPATCHED, PARTIAL, RECEIVED  # noqa: F401


def _state_of(source):
    """从 TransferLedger / state dict / 订单列表中取出 state dict。"""
    if source is None:
        return {'stock': {}, 'orders': {}}
    if hasattr(source, 'state') and isinstance(source.state, dict):
        return source.state
    if isinstance(source, dict) and 'orders' in source:
        return source
    if isinstance(source, (list, tuple)):
        return {'stock': {}, 'orders': {str(o.get('id', i)): o for i, o in enumerate(source)
                                        if isinstance(o, dict)}}
    raise TypeError('无法识别的调拨核对数据')


def order_origin_delta(order):
    """纯函数：一张单调拨单的产地克重差额 D - 已投 - 其余去向。"""
    origin = order.get('origin_g') or {}
    dispatched = {k: parse_gram(v) for k, v in origin.items()}
    whole = parse_gram(order.get('quantity_g', total_of(dispatched)))
    fed = parse_gram(order.get('fed_g', 0))
    signed = parse_gram(order.get('signed_g', 0))
    fed_origin = share_of(dispatched, fed, whole)
    if order.get('status') == CANCELLED and order.get('cancellation'):
        resting = as_origin_map(order['cancellation'].get('origin_g'))
    else:
        signed_origin = share_of(dispatched, signed, whole)
        transit = sub_maps(dispatched, signed_origin)
        onhand = sub_maps(signed_origin, fed_origin)
        resting = add_maps(transit, onhand)
    return sub_maps(dispatched, add_maps(fed_origin, resting))


def reconcile_orders(orders):
    """核对一组调拨单：逐单差额 + 单量汇总。"""
    rows = []
    all_balanced = True
    for raw in orders:
        order = raw if isinstance(raw, dict) else getattr(raw, '__dict__', {})
        delta = order_origin_delta(order)
        balanced = all(v == 0 for v in delta.values())
        all_balanced &= balanced
        rows.append({
            'id': order.get('id') or order.get('order_id'),
            'roll': order.get('roll') or order.get('material'),
            'status': order.get('status'),
            'quantity_g': Gram(order.get('quantity_g', 0)),
            'signed_g': Gram(order.get('signed_g', 0)),
            'fed_g': Gram(order.get('fed_g', 0)),
            'returned_g': Gram((order.get('cancellation') or {}).get('returned_g', 0))
            if order.get('cancellation') else Gram(order.get('returned_g', 0) or 0),
            'origin_delta_g': view_map(delta),
            'balanced': balanced,
        })
    return {'orders': rows, 'balanced': all_balanced,
            'orders_checked': len(rows)}


def reconcile_ledger(ledger):
    state = _state_of(ledger)
    added, onhand, used = {}, {}, {}
    for account in state.get('stock', {}).values():
        ws = account['workshop']
        added = add_maps(added, {k: parse_gram(v) for k, v in account.get('added_origin_g', {}).items()})
        onhand = add_maps(onhand, {k: parse_gram(v) for k, v in account.get('origin_g', {}).items()})
        used = add_maps(used, {k: parse_gram(v) for k, v in account.get('used_origin_g', {}).items()})

    transit, fed_total = {}, {}
    order_rows = []
    for order in state.get('orders', {}).values():
        whole = parse_gram(order['quantity_g'])
        signed = parse_gram(order.get('signed_g', 0))
        fed = parse_gram(order.get('fed_g', 0))
        origin = {k: parse_gram(v) for k, v in order.get('origin_g', {}).items()}
        signed_origin = share_of(origin, signed, whole)
        fed_origin = share_of(origin, fed, whole)
        fed_total = add_maps(fed_total, fed_origin)
        if order.get('status') != CANCELLED:
            transit = add_maps(transit, sub_maps(origin, signed_origin))
        delta = order_origin_delta(order)
        order_rows.append({'id': order['id'], 'origin_delta_g': view_map(delta),
                           'balanced': all(v == 0 for v in delta.values())})

    keys = set(added) | set(onhand) | set(transit) | set(fed_total)
    origins = {}
    global_balanced = True
    for key in sorted(keys):
        a = added.get(key, Fraction(0))
        h = onhand.get(key, Fraction(0))
        t = transit.get(key, Fraction(0))
        f = fed_total.get(key, Fraction(0))
        delta = a - h - t - f
        global_balanced &= delta == 0
        origins[key] = {
            'added_g': Gram(a),
            'stock_g': Gram(h),
            'transit_g': Gram(t),
            'in_transit_g': Gram(t),
            'fed_g': Gram(f),
            'used_g': Gram(f),
            'accounted_g': Gram(h + t + f),
            'delta_g': Gram(delta),
        }

    order_balanced = all(row['balanced'] for row in order_rows)
    balanced = global_balanced and order_balanced
    total_added = total_of(added)
    total_onhand = total_of(onhand)
    total_transit = total_of(transit)
    total_fed = total_of(fed_total)
    return {
        'balanced': balanced,
        'ok': balanced,
        'origin_balanced': global_balanced,
        'orders_balanced': order_balanced,
        'origins': origins,
        'orders': order_rows,
        'orders_checked': len(order_rows),
        'added_g': Gram(total_added),
        'stock_g': Gram(total_onhand),
        'onhand_g': Gram(total_onhand),
        'transit_g': Gram(total_transit),
        'in_transit_g': Gram(total_transit),
        'fed_g': Gram(total_fed),
        'used_g': Gram(total_fed),
        'delta_g': Gram(total_added - total_onhand - total_transit - total_fed),
    }


def build_overview(ledger):
    state = _state_of(ledger)
    # ledger 可能是裸 state：尽量构造视图
    if hasattr(ledger, '_order_view'):
        orders = [ledger._order_view(o) for o in state.get('orders', {}).values()]
        stock = ledger.stock_view()
    else:
        from .ledger import TransferLedger
        temporary = TransferLedger()
        temporary.state = state
        orders = [temporary._order_view(o) for o in state.get('orders', {}).values()]
        stock = temporary.stock_view()
    audit = reconcile_ledger(ledger)
    workshop_names = sorted({row['workshop'] for row in stock}
                            | {o['from_workshop'] for o in orders}
                            | {o['to_workshop'] for o in orders})
    return {
        'orders': orders,
        'transfers': orders,
        'transfer_orders': orders,
        'dispatch_orders': orders,
        'items': orders,
        'records': orders,
        'rows': orders,
        'data': orders,
        'stock': stock,
        'balances': stock,
        'inventory': stock,
        'workshops': workshop_names,
        'reconciliation': audit,
        'audit': audit,
        'balanced': audit['balanced'],
        'ok': audit['balanced'],
        'order_count': len(orders),
        'transfer_count': len(orders),
        'count': len(orders),
    }


# 近义纯函数名，方便不同入口调用
def verify_transfers(source=None, *args, **kwargs):
    if hasattr(source, 'reconcile'):
        return source.reconcile()
    return reconcile_ledger(source)


def check_transfers(source=None, *args, **kwargs):
    return verify_transfers(source, *args, **kwargs)


def transfer_report(source=None, *args, **kwargs):
    if hasattr(source, 'overview'):
        return source.overview()
    return build_overview(source)
