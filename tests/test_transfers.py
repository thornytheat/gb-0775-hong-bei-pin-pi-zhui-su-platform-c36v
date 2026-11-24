"""跨车间调拨：在途冻结、分批签收、投料/退单竞争、幂等重试与产地守恒。

这些测试逐条对应需求：
- 发出未签收的料两边都领不到；
- 分批签收时接收方只领已签部分，签多少立刻能投多少；
- 投过料再退单，已投部分算接收方用料，剩余退回发料方；
- 并发退单/投料，同一份料只有一个去向；
- 退单重试不重复退料；
- 调拨前后产地克重对得上。
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import pytest

from app.errors import RoastError
from app.modules.transfers import TransferLedger, seed_demo


@pytest.fixture
def book(tmp_path):
    ledger = TransferLedger(tmp_path / 'transfer')
    ledger.add_stock(workshop='打卷车间', roll='RT-A', quantity_g='300',
                     origin_g={'ET-01': '180', 'BR-08': '120'})
    return ledger


def dispatch(book, oid='DB-1', qty='100'):
    return book.create_order(order_id=oid, roll='RT-A',
                             from_workshop='打卷车间', to_workshop='包装车间',
                             quantity_g=qty)


# ---------- 发出未签收：两头领不到 ----------

def test_dispatched_qty_leaves_sender_balance(book):
    assert book.available('打卷车间', 'RT-A') == 300
    dispatch(book, qty='100')
    assert book.available('打卷车间', 'RT-A') == 200


def test_in_transit_material_unclaimable_by_receiver(book):
    dispatch(book)
    assert book.available('包装车间', 'RT-A') == 0
    with pytest.raises(RoastError) as info:
        book.feed('DB-1', '1')
    assert info.value.code in ('INSUFFICIENT_CLAIMABLE', 'ORDER_CANCELLED')


def test_sender_cannot_double_dispatch_same_balance(book):
    dispatch(book, oid='DB-1', qty='300')
    with pytest.raises(RoastError) as info:
        book.create_order(order_id='DB-2', roll='RT-A',
                          from_workshop='打卷车间', to_workshop='二车间',
                          quantity_g='1')
    assert info.value.code == 'INSUFFICIENT_STOCK'


def test_neither_side_can_claim_in_transit_via_http(client):
    client.post('/api/transfers/stock',
                json={'workshop': 'A', 'roll': 'R', 'quantity_g': '10'})
    oid = client.post('/api/transfers',
                      json={'roll': 'R', 'from': 'A', 'to': 'B', 'quantity_g': '10'}).json['id']
    # 接收方无单投料也失败
    response = client.post('/api/transfers/feed',
                           json={'workshop': 'B', 'roll': 'R', 'quantity_g': '1'})
    assert response.status_code == 409
    # 发料方余额已冻结
    assert int(Fraction(client.get('/api/stock').json['stock'][0]['available_g'])) == 0


# ---------- 分批签收：只领已签，签多少投多少 ----------

def test_partial_sign_only_signed_is_claimable(book):
    dispatch(book)
    book.sign('DB-1', '40')
    row = book.get_order('DB-1')
    assert (row['signed_g'], row['in_transit_g'], row['claimable_g']) == ('40', '60', '40')
    # 超签在途被拒
    with pytest.raises(RoastError):
        book.sign('DB-1', '61')
    # 已签 40 可立刻全投
    fed = book.feed('DB-1', '40')
    assert fed['fed_g'] == '40'
    assert book.get_order('DB-1')['claimable_g'] == '0'
    # 再投一分都不行
    with pytest.raises(RoastError):
        book.feed('DB-1', '1')


def test_multiple_sign_batches_accumulate(book):
    dispatch(book)
    book.sign('DB-1', '30')
    book.sign('DB-1', '30')
    row = book.get_order('DB-1')
    assert row['signed_g'] == '60' and len(row['signed_batches']) == 2
    assert row['status'] == 'partial'
    book.sign('DB-1')  # 空数量 = 签完剩余 40
    assert book.get_order('DB-1')['status'] == 'received'
    assert book.get_order('DB-1')['in_transit_g'] == '0'


# ---------- 投料后退单：已投留接收方，剩余退发料方 ----------

def test_fed_portion_stays_receiver_rest_returns_sender(book):
    dispatch(book, qty='100')
    book.sign('DB-1', '80')
    book.feed('DB-1', '30')
    result = book.cancel('DB-1')
    assert result['status'] == 'cancelled'
    assert result['fed_g'] == '30'
    assert result['returned_g'] == '70'
    # 发料方：300 - 100 发出 + 70 退回 = 270
    assert book.available('打卷车间', 'RT-A') == 270
    # 接收方已签未投 50 被收回，在库为 0；已投 30 保留在耗用账
    assert book.available('包装车间', 'RT-A') == 0
    stock = next(r for r in book.stock_view() if r['workshop'] == '包装车间')
    assert stock['used_g'] == '30'


def test_cancel_before_any_feed_returns_full_dispatch(book):
    dispatch(book, qty='100')
    book.sign('DB-1', '40')
    result = book.cancel('DB-1')
    assert result['returned_g'] == '100'
    assert book.available('打卷车间', 'RT-A') == 300


def test_cancelled_order_cannot_feed(book):
    dispatch(book)
    book.sign('DB-1', '100')
    book.cancel('DB-1')
    with pytest.raises(RoastError) as info:
        book.feed('DB-1', '1')
    assert info.value.code == 'ORDER_CANCELLED'


# ---------- 退单重试幂等 ----------

def test_cancel_retry_does_not_double_return(book):
    dispatch(book)
    book.sign('DB-1', '100')
    book.feed('DB-1', '20')
    first = book.cancel('DB-1')
    balance_after_first = book.available('打卷车间', 'RT-A')
    second = book.cancel('DB-1')
    third = book.cancel_order('DB-1')
    assert first['reused'] is False
    assert second['reused'] is True and third['reused'] is True
    assert second['returned_g'] == first['returned_g'] == '80'
    assert book.available('打卷车间', 'RT-A') == balance_after_first


def test_cancel_http_retry_is_idempotent(client):
    client.post('/api/transfers/stock',
                json={'workshop': 'A', 'roll': 'R', 'quantity_g': '50'})
    oid = client.post('/api/transfers',
                      json={'roll': 'R', 'from_workshop': 'A',
                            'to_workshop': 'B', 'quantity_g': '50'}).json['id']
    client.post(f'/api/transfers/{oid}/sign', json={'quantity_g': '50'})
    first = client.post(f'/api/transfers/{oid}/cancel')
    second = client.post(f'/api/transfers/{oid}/cancel')
    assert first.status_code == 200 and second.status_code == 200
    assert first.json['returned_g'] == second.json['returned_g'] == '50'
    stock = client.get('/api/stock').json['stock']
    sender = next(r for r in stock if r['workshop'] == 'A')
    assert Fraction(sender['available_g']) == 50


# ---------- 并发：同一份料只有一个去向 ----------

def test_parallel_cancel_retries_return_once(tmp_path):
    ledger = TransferLedger(tmp_path / 'par')
    ledger.add_stock(workshop='A', roll='R', quantity_g='100')
    ledger.create_order(order_id='T', roll='R', from_workshop='A',
                        to_workshop='B', quantity_g='100')
    ledger.sign('T', '40')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ledger.cancel('T'), range(16)))
    assert sum(not r['reused'] for r in results) == 1
    assert ledger.available('A', 'R') == 100
    assert ledger.reconcile()['balanced']


def test_concurrent_feed_and_cancel_single_destination(tmp_path):
    ledger = TransferLedger(tmp_path / 'race')
    ledger.add_stock(workshop='A', roll='R', quantity_g='100')
    ledger.create_order(order_id='T', roll='R', from_workshop='A',
                        to_workshop='B', quantity_g='100')
    ledger.sign('T', '100')

    barrier = threading.Barrier(2)
    outcomes = {}

    def feed():
        barrier.wait()
        try:
            ledger.feed('T', '100')
            outcomes['feed'] = 'won'
        except RoastError:
            outcomes['feed'] = 'rejected'

    def cancel():
        barrier.wait()
        result = ledger.cancel('T')
        outcomes['cancel'] = 'reused' if result['reused'] else 'effective'

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(feed)
        pool.submit(cancel)

    order = ledger.get_order('T')
    fed, returned = Fraction(order['fed_g']), Fraction(order['returned_g'])
    # 投料与退料之和恒等于调出量：同一份料没有两个去向
    assert fed + returned == 100
    assert ledger.available('A', 'R') == returned
    assert ledger.available('B', 'R') == 0
    assert ledger.reconcile()['balanced']
    assert outcomes['feed'] in ('won', 'rejected')


def test_many_race_iterations_always_conserve(tmp_path):
    for index in range(60):
        ledger = TransferLedger(tmp_path / f'race-{index}')
        ledger.add_stock(workshop='A', roll='R', quantity_g='100')
        ledger.create_order(order_id='T', roll='R', from_workshop='A',
                            to_workshop='B', quantity_g='100')
        ledger.sign('T', '100')
        barrier = threading.Barrier(5)

        def worker(kind):
            barrier.wait()
            try:
                if kind == 'feed':
                    ledger.feed('T', '25')
                else:
                    ledger.cancel('T')
            except RoastError:
                pass

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(worker, ['feed', 'feed', 'feed', 'feed', 'cancel']))
        order = ledger.get_order('T')
        assert Fraction(order['fed_g']) + Fraction(order['returned_g']) == 100
        assert ledger.reconcile()['balanced']


# ---------- 产地克重调拨前后对得上 ----------

def test_origin_grams_conserve_through_split_sign_feed_cancel(book):
    dispatch(book, qty='120')  # ET 72, BR 48
    book.sign('DB-1', '60')
    book.feed('DB-1', '30')
    book.cancel('DB-1')
    audit = book.reconcile()
    assert audit['balanced']
    # 初始 ET180/BR120；本单最终：ET 投18+退54，BR 投12+退36
    et = audit['origins']['ET-01']
    assert et['added_g'] == '180' and et['fed_g'] == '18' and et['stock_g'] == '162'
    br = audit['origins']['BR-08']
    assert br['added_g'] == '120' and br['fed_g'] == '12' and br['stock_g'] == '108'


def test_fractional_origin_grams_conserve_exactly(tmp_path):
    ledger = TransferLedger(tmp_path / 'frac')
    # 3 克按 1/3 分摊会产生分数克，必须用 Fraction 精确守恒
    ledger.add_stock(workshop='A', roll='RT-X', quantity_g='1',
                     origin_g={'O1': '1/3', 'O2': '2/3'})
    ledger.create_order(order_id='T', roll='RT-X', from_workshop='A',
                        to_workshop='B', quantity_g='1')
    ledger.sign('T', '1/3')
    ledger.feed('T', '1/3')
    ledger.cancel('T')
    audit = ledger.reconcile()
    assert audit['balanced'] and audit['delta_g'] == '0'
    for row in audit['origins'].values():
        assert row['delta_g'] == '0'


# ---------- 持久化与页面 ----------

def test_ledger_reload_keeps_state(tmp_path):
    directory = tmp_path / 'keep'
    book = TransferLedger(directory)
    book.add_stock(workshop='A', roll='R', quantity_g='10')
    book.create_order(order_id='T', roll='R', from_workshop='A',
                      to_workshop='B', quantity_g='10')
    TransferLedger(directory).sign('T', '4')
    reloaded = TransferLedger(directory)
    assert reloaded.get_order('T')['signed_g'] == '4'
    assert reloaded.available('B', 'R') == 4


def test_balance_page_lists_transfer_order(client):
    client.post('/api/transfers/demo')
    page = client.get('/ledger/balance?demo=1').get_data(as_text=True)
    assert '跨车间调拨单核对' in page and 'TO-DEMO-4' in page
    assert '发出未签收' in page and '退单重试不重复退料' in page


def test_demo_seed_is_idempotent(tmp_path):
    book = TransferLedger(tmp_path / 'demo')
    first = seed_demo(book)
    second = seed_demo(book)
    assert first['reused'] is False and second['reused'] is True
    assert len(book.list_orders()) == 4
    assert book.reconcile()['balanced']


def test_gram_values_compare_as_numbers_and_strings(book):
    dispatch(book)
    row = book.get_order('DB-1')
    assert row['quantity_g'] == '100' and row['quantity_g'] == 100
    assert Fraction(row['quantity_g']) == 100
