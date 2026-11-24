from flask import Blueprint,current_app,jsonify,request

from app.errors import RoastError
from app.modules.proof.service import verify_and_export
from app.modules.runs.service import seal_run
from app.pipeline import PHASES,compute
from app.source import receive

api=Blueprint('roast_api',__name__,url_prefix='/api')


def cabinet():
    return current_app.extensions['lot_cabinet']


def transfer_ledger():
    return current_app.extensions.get('transfer_ledger')


@api.route('/lots/transfers', methods=['GET', 'POST'])
@api.route('/lots/transfer', methods=['GET', 'POST'])
def check_transfers():
    from app.modules.transfers.service import app_ledger
    ledger = transfer_ledger() or app_ledger()
    body = request.get_json(silent=True) if request.method == 'POST' else None
    if isinstance(body, dict) and body.get('action'):
        action = body.pop('action')
        if action not in ('sign', 'feed', 'cancel', 'receive', 'consume', 'dispatch'):
            raise RoastError('NO_ACTION', '未知调拨动作', 404)
        return jsonify(getattr(ledger, action)(**body))
    return jsonify(ledger.overview())


@api.route('/lots/<phase>',methods=['GET','POST'])
def check(phase):
    if phase not in PHASES:
        raise RoastError('NO_PHASE','未知称签模块',404)
    run=request.args.get('run')
    if request.method=='GET' and run:
        result=cabinet().read(run)['reports']
    else:
        body=request.get_json(silent=True) if request.method=='POST' else {'sample':request.args.get('sample','dirty')}
        result=compute(receive(body),phase)
    return jsonify(result[phase])


@api.get('/runs')
def files():
    return jsonify(runs=cabinet().recent())


@api.post('/runs')
def new_run():
    result=seal_run(cabinet(),receive(request.get_json(silent=True)))
    return jsonify(result),200 if result['reused'] else 201


@api.get('/runs/<key>')
def original(key):
    data=cabinet().read(key)
    return jsonify(id=key,manifest=data['manifest'],balance=data['reports']['balance'])


@api.post('/runs/<key>/rerun')
def rerun(key):
    old=cabinet().read(key)
    return jsonify(seal_run(cabinet(),old['files'],old['manifest']))


@api.get('/export/<key>')
def export(key):
    proof=verify_and_export(cabinet().read(key))
    response=jsonify(proof)
    response.headers['Content-Disposition']=f'attachment; filename="roast-proof-{key[:12]}.json"'
    return response
