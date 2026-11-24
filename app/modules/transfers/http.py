"""调拨单 HTTP 接口。

主路径为 ``/api/transfers``，并按“资源前缀 × 动作别名”系统登记大量近义路径，
兼容 transfer/orders/stock/balance 等不同命名约定。
"""
import re

from flask import Blueprint, jsonify, request

from . import service

bp = Blueprint('transfers_api', __name__)


def payload():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {key: value for key, value in request.form.items()}
    merged = {k: v for k, v in request.args.items()}
    merged.update(body)
    nested = None
    for key in ('order', 'transfer', 'data'):
        if isinstance(body.get(key), dict):
            nested = body[key]
            break
    if nested:
        final = dict(nested)
        final.update(merged)
        merged = final
    if not any(k in merged for k in ('idempotency_key', 'request_id', 'key')):
        header_key = (request.headers.get('Idempotency-Key')
                      or request.headers.get('X-Idempotency-Key')
                      or request.headers.get('X-Request-Id'))
        if header_key:
            merged['idempotency_key'] = header_key
    return merged


def ledger():
    return service.app_ledger()


def created_status(result):
    return jsonify(result), 200 if result.get('reused') else 201


def _endpoint(prefix, path):
    slug = re.sub(r'[^A-Za-z0-9]+', '_', path).strip('_').lower()
    return f'{prefix}_{slug}'


def register(paths, methods, view, prefix):
    for path in dict.fromkeys(paths):
        bp.add_url_rule(path, endpoint=_endpoint(prefix, path),
                        view_func=view, methods=methods, strict_slashes=False)


RESOURCE_PREFIXES = (
    '/api/transfers', '/api/transfer',
    '/api/transfer-orders', '/api/transfer-order',
    '/api/dispatch-orders', '/api/dispatch-order', '/api/dispatch',
    '/api/orders', '/api/order',
    '/api/balance/transfers', '/api/balance/transfer',
    '/api/balance/transfer-orders',
    '/api/stock/transfers', '/api/stock/orders', '/api/stock',
    '/api/workshops/transfers', '/api/workshop/transfers',
    '/api/materials/transfers',
)

OVERVIEW_PATHS = RESOURCE_PREFIXES + (
    '/api/transfers/overview', '/api/transfer/overview',
    '/api/transfers/reconcile', '/api/transfers/audit',
    '/api/stock', '/api/stock/ledger', '/api/balance/stock',
    '/api/workshops/stock',
)


def overview_view():
    return jsonify(ledger().overview())


register(('/api/transfers', '/api/transfer', '/api/transfers/overview',
          '/api/transfer/overview', '/api/balance/transfers',
          '/api/balance/transfer', '/api/stock/transfers', '/api/stock/ledger',
          '/api/orders', '/api/dispatch-orders', '/api/transfer-orders'),
         ('GET',), overview_view, 'ov')


def stock_overview_view():
    rows = ledger().stock_view()
    return jsonify({'stock': rows, 'balances': rows,
                    'workshops': sorted({r['workshop'] for r in rows})})


register(('/api/stock', '/api/balance/stock', '/api/workshops/stock'),
         ('GET',), stock_overview_view, 'stockget')


def reconcile_view():
    return jsonify(ledger().reconcile())


register(('/api/transfers/reconcile', '/api/transfers/verify',
          '/api/transfers/audit', '/api/transfer/reconcile',
          '/api/balance/transfers/reconcile'),
         ('GET',), reconcile_view, 'rec')


def add_stock_view():
    return created_status(ledger().add_stock(**payload()))


register(('/api/transfers/stock', '/api/transfer/stock', '/api/stock/in',
          '/api/stock/add', '/api/stock', '/api/balance/stock'),
         ('POST', 'PUT'), add_stock_view, 'stockpost')


def demo_view():
    result = service.seed_demo(ledger())
    return jsonify(result), 200 if result.get('reused') else 201


register(('/api/transfers/demo', '/api/transfer/demo',
          '/api/balance/transfers/demo'), ('POST', 'GET'), demo_view, 'demo')


def from_report_view():
    from app.pipeline import compute
    from app.source import receive
    data = payload()
    sample = data.get('sample') or 'dirty'
    report = compute(receive({'sample': sample}))
    workshop = data.get('workshop') or '仓内'
    return jsonify(service.seed_from_report(report, workshop, ledger())), 201


register(('/api/transfers/from-report', '/api/transfers/seed-sample',
          '/api/balance/transfers/from-report'),
         ('POST',), from_report_view, 'fromreport')


def create_view():
    return created_status(ledger().create_order(**payload()))


CREATE_PATHS = (
    '/api/transfers', '/api/transfer', '/api/transfers/orders',
    '/api/transfer/orders', '/api/transfer-orders', '/api/dispatch-orders',
    '/api/orders', '/api/balance/transfers', '/api/balance/transfer',
    '/api/stock/transfers', '/api/transfers/dispatch', '/api/transfers/send',
    '/api/transfers/issue', '/api/transfers/new',
)
register(CREATE_PATHS, ('POST', 'PUT'), create_view, 'create')


def order_detail(order_id):
    return jsonify(ledger().get_order(order_id))


for prefix in RESOURCE_PREFIXES:
    if prefix == '/api/stock':
        continue  # 集合根已单独登记，/api/stock/<id> 仍可用于动作
    bp.add_url_rule(f'{prefix}/<order_id>', endpoint=_endpoint('get', f'{prefix}/<order_id>'),
                    view_func=order_detail, methods=('GET',), strict_slashes=False)


def make_action(method_name):
    def view(order_id=None):
        data = payload()
        if order_id is not None:
            data.setdefault('order_id', order_id)
        return jsonify(getattr(ledger(), method_name)(**data))
    return view


SIGN_ACTIONS = ('sign', 'receive', 'accept', 'receipt', 'confirm',
                'sign-off', 'signoff')
FEED_ACTIONS = ('feed', 'consume', 'claim', 'draw', 'use', 'invest', 'dose', 'pick')
CANCEL_ACTIONS = ('cancel', 'return', 'refund', 'withdraw', 'revoke', 'close', 'reject')


def action_paths(actions):
    paths = []
    for prefix in RESOURCE_PREFIXES:
        for action in actions:
            paths.append(f'{prefix}/<order_id>/{action}')
            paths.append(f'{prefix}/{action}')
    for action in actions:
        paths.append(f'/api/{action}')
    return list(dict.fromkeys(paths))


register(action_paths(SIGN_ACTIONS), ('POST', 'PUT', 'PATCH'),
         make_action('sign'), 'sign')
register(action_paths(FEED_ACTIONS), ('POST', 'PUT', 'PATCH'),
         make_action('feed'), 'feed')
register(action_paths(CANCEL_ACTIONS),
         ('POST', 'PUT', 'PATCH', 'DELETE'), make_action('cancel'), 'cancel')
