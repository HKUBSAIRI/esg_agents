"""Batch isolation, cancellation, accounting and actual cache flow; no paid calls."""
from copy import deepcopy
import json
from pathlib import Path
import threading

import fitz
import pytest

from review_workbench.contracts import ContractError
from review_workbench.pipeline import save
from review_workbench.service import Application
from review_workbench.store import Store, Conflict
from esg_pipeline.esgagent_core import delivered_reports


@pytest.fixture
def app(tmp_path):
    instance=Application(Store(tmp_path/'db'))
    yield instance
    instance.close()


def body(items,**options):
    return {'items':items,'options':{'mode':'rules_only','allow_external':False,'a_calls':0,'b_calls':0,**options},
            'pricing':dict.fromkeys(('extract_input','extract_output','audit_input','audit_output')),'price_note':''}


def item(source=None):
    source=source or delivered_reports()[0]
    return {'source_id':source['id'],'year':source['report_year'],'company_code':source['company_code']}


def fake_runner(app,monkeypatch,fail_first=False):
    calls=[]
    def execute(identity):
        calls.append(identity)
        if fail_first and len(calls)==1:
            app.pipeline.update(identity,status='FAILED',message='synthetic failure',error_code='MOCK_ERROR')
        else:
            summary={'run_id':identity,'workbench_run_id':'ar_'+identity[4:]}
            app.pipeline.update(identity,status='PARTIAL',stage='FINISHED',summary=summary,result_run_id=summary['workbench_run_id'])
    monkeypatch.setattr(app.pipeline,'_execute',execute)
    monkeypatch.setattr(app.pipeline,'collection',lambda summaries,**kw:{'reports':summaries,'only_batch_reports':True})
    return calls


def test_failure_continues_and_duplicates_skip(app,monkeypatch):
    calls=fake_runner(app,monkeypatch,fail_first=True)
    a,b=map(item,delivered_reports()[:2])
    queue=app.pipeline.batches
    identity=queue.start(body([a,b,b]));app.active.result(timeout=10)
    view=queue.public(identity)
    assert len(calls)==2
    assert view['status']=='COMPLETED_WITH_ERRORS'
    assert [r['status'] for r in view['items']]==['FAILED','PARTIAL','SKIPPED']
    assert view['items'][0]['error_code']=='MOCK_ERROR'
    assert view['usage']['calls']==0 and view['cost']['known_estimate_cny']==0
    collection=json.loads((queue.directory/identity/'annual_collection.json').read_text(encoding='utf8'))
    assert len(collection['reports'])==1
    seen=[]
    def export(collection,path):seen.append(collection);Path(path).write_bytes(b'xlsx')
    monkeypatch.setattr('esg_pipeline.workbook_export.export_workbook',export)
    assert queue.export(identity)==b'xlsx'
    assert len(seen[0]['reports'])==1


def test_preflight_bad_item_does_not_block_other_reports(app,monkeypatch):
    calls=fake_runner(app,monkeypatch)
    invalid={**item(),'source_id':'C:/secret.pdf'}
    identity=app.pipeline.batches.start(body([invalid,item()]))
    app.active.result(timeout=10)
    result=app.pipeline.batches.public(identity)
    assert len(calls)==1 and result['counts']['FAILED']==1 and result['counts']['PARTIAL']==1


@pytest.mark.parametrize('bad',[{'mode':'model','allow_external':False,'a_calls':1},
    {'allow_external':True},{'a_calls':True},{'a_calls':21}])
def test_invalid_batch_authorization_never_starts(app,bad):
    with pytest.raises(ContractError):app.pipeline.batches.start(body([item()],**bad))
    assert not list(app.pipeline.directory.glob('job_*.json'))


def test_invalid_pricing_is_rejected(app):
    value=body([item()]);value['pricing']['extract_input']=float('inf')
    with pytest.raises(ContractError):app.pipeline.batches.start(value)


def test_unexpected_single_preflight_error_does_not_orphan_queue(app,monkeypatch):
    calls=fake_runner(app,monkeypatch)
    original=app.pipeline.prepare;count=0
    def prepare(request):
        nonlocal count
        count+=1
        if count==1:raise ValueError('PRIVATE_INTERNAL_DETAIL')
        return original(request)
    monkeypatch.setattr(app.pipeline,'prepare',prepare)
    identity=app.pipeline.batches.start(body(list(map(item,delivered_reports()[:2]))))
    app.active.result(timeout=10)
    result=app.pipeline.batches.public(identity)
    assert result['counts']['FAILED']==1 and result['counts']['PARTIAL']==1 and len(calls)==1
    assert 'PRIVATE_INTERNAL_DETAIL' not in json.dumps(result)


def test_per_report_limits_names_and_pricing_snapshot(app,monkeypatch):
    fake_runner(app,monkeypatch)
    a,b=map(item,delivered_reports()[:2]);a['name']='first_selected.pdf';b['name']='second_selected.pdf'
    request=body([a,b],mode='model',allow_external=True,a_calls=2,b_calls=3)
    request['pricing']['extract_input']=1.5
    identity=app.pipeline.batches.start(deepcopy(request));app.active.result(timeout=10)
    result=app.pipeline.batches.public(identity)
    assert [x['name'] for x in result['items']]==['first_selected.pdf','second_selected.pdf']
    assert all(x['options']['a_calls']==2 and x['options']['b_calls']==3 for x in result['items'])
    assert result['pricing']['extract_input']==1.5


def test_stop_cancels_pending_and_prevents_next_call(app,monkeypatch):
    started=threading.Event();release=threading.Event();calls=[]
    def execute(identity):
        calls.append(identity);started.set();assert release.wait(10)
        app.pipeline.update(identity,status='CANCELLED',message='stopped')
    monkeypatch.setattr(app.pipeline,'_execute',execute)
    identity=app.pipeline.batches.start(body(list(map(item,delivered_reports()[:2]))))
    try:
        assert started.wait(5)
        with pytest.raises(Conflict):app.pipeline.batches.start(body([item()]))
        with pytest.raises(Conflict):app.pipeline.batches.export(identity)
        result=app.pipeline.batches.cancel(identity)
        assert result['items'][1]['status']=='CANCELLED'
    finally:release.set()
    app.active.result(timeout=10)
    assert len(calls)==1 and app.pipeline.batches.public(identity)['status']=='CANCELLED'


def test_accounting_deduplicates_attempts_and_marks_unknown_cost(app,tmp_path):
    folder=tmp_path/'job';(folder/'upstream').mkdir(parents=True)
    rows=[{'call_id':'a','stage':'esgagent_extract','phase':'start'},
          {'call_id':'a','stage':'esgagent_extract','phase':'end','usage':{'prompt_tokens':100,'completion_tokens':50,'total_tokens':150}},
          {'call_id':'b','stage':'esgagent_extract','phase':'start'}]
    (folder/'upstream/model_usage.jsonl').write_text('\n'.join(map(json.dumps,rows)),encoding='utf8')
    job={'job_id':'job_'+'0'*32,'pipeline_directory':str(folder)}
    assert app.pipeline._usage(job)['calls']==2
    price={'extract_input':10,'extract_output':20,'audit_input':None,'audit_output':None}
    result=app.pipeline.batches.cost(job,price)
    assert result['known_estimate_cny']==pytest.approx(.002)
    assert result['unknown_cost_requests']==1
    price['extract_input']=None
    assert app.pipeline.batches.cost(job,price)['unknown_cost_requests']==2


def test_restart_marks_queue_interrupted_without_resubmitting(app,monkeypatch):
    identity='batch_'+'a'*32
    app.pipeline.batches.write({'batch_id':identity,'status':'RUNNING','items':[], 'current_job_id':None})
    monkeypatch.setattr(app.executor,'submit',lambda *a,**k:pytest.fail('restart must not submit paid work'))
    app.pipeline.recover_interrupted()
    assert app.pipeline.batches.read(identity)['status']=='INTERRUPTED'


def test_real_cached_report_after_uncached_failure_and_export_scope(app,tmp_path,monkeypatch):
    # A genuine delivered cache goes through A/B/UI/annual after a cache miss.
    source=tmp_path/'00807_new.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((20,30),'Uncached ESG report');pdf.save(source)
    uploaded=app.pipeline.upload(source.read_bytes(),source.name)
    first={'source_id':uploaded['id'],'company_code':'00807','year':2025}
    identity=app.pipeline.batches.start(body([first,item()]))
    app.active.result(timeout=60)
    batch=app.pipeline.batches.public(identity)
    assert batch['counts']['FAILED']==1 and batch['counts']['PARTIAL']==1,batch
    assert batch['usage']['calls']==0
    assert batch['items'][0]['error_code']=='ESGAGENT_CACHE_MISS_REQUIRES_MODEL'
    saved=json.loads((app.pipeline.batches.directory/identity/'annual_collection.json').read_text(encoding='utf8'))
    assert len(saved['reports'])==1
    assert {r['stock_code'] for r in saved['rows']}=={delivered_reports()[0]['company_code']}
    captured=[]
    def export(collection,path):captured.append(collection);Path(path).write_bytes(b'xlsx')
    monkeypatch.setattr('esg_pipeline.workbook_export.export_workbook',export)
    app.pipeline.batches.export(identity)
    assert {r['stock_code'] for r in captured[0]['rows']}=={delivered_reports()[0]['company_code']}
