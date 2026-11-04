"""Actual delivered batches cross the new-core/B/UI/annual boundary, no API."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

import fitz
import pytest

from esg_extraction.extractor import file_hash
from esg_pipeline.esgagent_core import delivered_reports,run_worker,resolve_company
from esg_pipeline.esgagent_pipeline import run_esgagent_pipeline,inspect_annual
from esg_pipeline.annual import build_collection
from review_workbench.service import Application
from review_workbench.store import Store


@pytest.fixture(scope='module')
def integrated(tmp_path_factory):
    root=tmp_path_factory.mktemp('new_esgagent')
    results=[run_esgagent_pipeline(s,root/'runs',year=2025,database=root/'db',runtime=root/'runtime') for s in delivered_reports()]
    return root,results


def test_all_delivered_batches_enter_b_and_workbench(integrated):
    root,results=integrated
    assert len(results)==3 and sum(r['candidate_count'] for r in results)==50
    app=Application(Store(root/'db'))
    try:
        for result in results:
            assert result['engine']=='esgagent-v1' and result['usage']['api_attempts']==0
            assert result['record_count']==result['candidate_count']
            assert sum(result['b_review_states'].values())==result['candidate_count']
            assert app.detail(result['workbench_run_id'])['has_pdf']
    finally:app.close()


def test_new_policy_drafts_do_not_become_official_confirmations(integrated):
    _,summaries=integrated
    collection=build_collection(summaries)
    rows=[r for r in collection['rows'] if r['year']==2025]
    assert len(rows)==3 and not collection['scoring_enabled']
    assert sum(v is not None for r in rows for f,v in r['values'].items() if f not in ('stock_code','company_name','sasb_sector','sasb_industry'))==20
    by_code={r['stock_code']:r for r in rows}
    assert by_code['00168']['states']['water_withdrawal_m3']['status']=='INFERRED_DRAFT'
    assert by_code['02460']['states']['water_recycling_rate_pct']['status']=='DERIVED_DRAFT'
    assert 'CURRENCY_INFERRED' in by_code['00168']['states']['revenue_million']['quality_flags']
    assert all(s['status']!='AUTO_VERIFIED' for r in rows for s in r['states'].values())


def test_b_rejection_also_blocks_dependent_derived_value(integrated,monkeypatch):
    _,results=integrated
    result=next(r for r in results if '饮料' in r['source_pdf'])
    from esg_pipeline import esgagent_pipeline as module
    original=module.load
    worker=original(Path(result['output_directory'])/'upstream/worker_result.json')
    cid=worker['stage1']['selections']['water_withdrawal']['candidate_id']
    def changed(path):
        data=original(path)
        if Path(path).name=='audit_report.json':
            for row in data['record_results']:
                if row['record_id']=='rec_eg_'+cid:row['review_status']='REJECTED'
        return data
    monkeypatch.setattr(module,'load',changed)
    report=inspect_annual(result)
    assert all('B_REJECTED' in o['reasons'] for o in report['observations'] if o['field'] in ('water_withdrawal_m3','water_recycling_rate_pct'))


def test_cache_only_never_falls_back_to_native_old_extractor(tmp_path):
    source=tmp_path/'00807_new.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((20,30),'New PDF not in any extraction cache')
        pdf.save(source)
    info={'_source':str(source),'_sha256':file_hash(source),'name':source.name}
    with pytest.raises(ValueError,match='CACHE_MISS_REQUIRES_MODEL'):
        run_worker(info,tmp_path/'job',year=2025,company_code='00807',runtime=tmp_path/'runtime')
    assert not (tmp_path/'job/model_usage.jsonl').exists()


def test_company_mismatch_blocked_before_model(tmp_path):
    with pytest.raises(ValueError,match='COMPANY_CODE_CONFLICT'):
        resolve_company(delivered_reports()[0],'00807')


def test_manual_revision_blocks_source_and_derived_drafts(integrated):
    root,results=integrated
    result=next(r for r in results if '饮料' in r['source_pdf'])
    data=json.loads((Path(result['output_directory'])/'upstream/worker_result.json').read_text(encoding='utf8'))
    cid=data['stage1']['selections']['water_withdrawal']['candidate_id']
    def reviewed(identity):return {'tasks':[{'record_uid':'rec_eg_'+cid,'state':'REAUDIT_REQUIRED'}],'events':[]}
    rows=build_collection([result],review_resolver=reviewed)['rows']
    row=next(r for r in rows if r['year']==2025)
    assert row['values']['water_withdrawal_m3'] is None and row['values']['water_recycling_rate_pct'] is None


def test_exclusion_cannot_be_evaded_by_exporting_only_a_cached_rerun(integrated):
    root,results=integrated
    original=next(r for r in results if '饮料' in r['source_pdf'])
    data=json.loads((Path(original['output_directory'])/'upstream/worker_result.json').read_text(encoding='utf8'))
    cid=data['stage1']['selections']['water_withdrawal']['candidate_id']
    store=Store(root/'db')
    # Simulate an opinion already saved under the older auto-review policy.
    task={'task_id':'legacy_review_'+cid,'record_uid':'rec_eg_'+cid,'state':'OPEN'}
    with store.connect() as db:db.execute('INSERT INTO tasks(id,run_id,payload) VALUES(?,?,?)',
        (task['task_id'],original['workbench_run_id'],json.dumps(task)))
    with store.connect() as db:db.execute("UPDATE tasks SET state='EXCLUDED_FROM_SCORING' WHERE id=?",(task['task_id'],))
    try:
        source=next(s for s in delivered_reports() if s['_sha256']==original['source_sha256'])
        rerun=run_esgagent_pipeline(source,root/'reruns',year=2025,database=root/'db',runtime=root/'runtime')
        collection=build_collection([rerun],review_resolver=store.review_for_export)
        row=next(r for r in collection['rows'] if r['year']==2025)
        assert row['values']['water_withdrawal_m3'] is None
        assert row['values']['water_recycling_rate_pct'] is None
        assert original['workbench_run_id'] in collection['reports'][0]['inherited_block_run_ids']
    finally:
        with store.connect() as db:db.execute("UPDATE tasks SET state=? WHERE id=?",(task['state'],task['task_id']))


@pytest.mark.parametrize('bad_json',[False,True])
@pytest.mark.parametrize('key_prefix',['sk-',''])
def test_worker_live_path_with_fake_stream_and_physical_budget(tmp_path,bad_json,key_prefix):
    """Exercise uncached extraction, not only delivery-cache projection."""
    from esg_pipeline.esgagent_core import CORE,ROOT
    source=tmp_path/'00807_new.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((20,30),'Group 2025 water withdrawal 120 m3')
        pdf.save(source)
    company=resolve_company({'name':source.name,'_sha256':file_hash(source)})
    job=tmp_path/'job';job.mkdir()
    key=tmp_path/'key.txt';key.write_text(key_prefix+'a'*32,encoding='utf8')
    request={'core':str(CORE),'directory':str(job),'runtime':str(tmp_path/'runtime'),'source':str(source),
             'sha256':file_hash(source),'name':source.name,'company':company,'year':2025,'allow_external':True,'max_calls':1,'key_file':str(key)}
    request_path=tmp_path/'request.json';request_path.write_text(json.dumps(request),encoding='utf8')
    payload={'candidates':[{'candidate_id':'c1','raw_value':'120','unit':'m3','data_year':2025,'metric_id':'water_withdrawal',
        'classification_status':'CLASSIFIED','classification_reason':'Explicit group withdrawal','confidence':0.9,
        'reporting_boundary':{'level':'GROUP'},'evidence_ids':['e1']}],
        'evidence':[{'evidence_id':'e1','source_page':1,'source_type':'TABLE','source_evidence':'Group 2025 water withdrawal 120 m3'}],
        'coverage':{'pages_scanned':[1]}}
    driver=tmp_path/'driver.py'
    driver.write_text("""
import json,runpy,sys
from pathlib import Path
from types import SimpleNamespace as NS
import openai
text=TEXT
class Fake:
    def __init__(self,**kwargs):
        assert kwargs['max_retries']==0
        self.chat=NS(completions=NS(create=self.create))
    def create(self,**kwargs):
        path=Path(sys.argv[1]).parent/'physical_calls.txt'
        with path.open('a') as f:f.write('call\\n')
        usage=NS(model_dump=lambda **kwargs:{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15})
        return iter_stream(NS(id='fake',usage=usage,choices=[NS(delta=NS(content=text,reasoning_content=None),finish_reason='stop')]))
def iter_stream(chunk):yield chunk
openai.OpenAI=Fake
sys.argv=[WORKER,sys.argv[1]]
runpy.run_path(WORKER,run_name='__main__')
""".replace('TEXT',repr('not JSON' if bad_json else json.dumps(payload))).replace('WORKER',repr(str(ROOT/'scripts/esgagent_worker.py'))),encoding='utf8')
    result=subprocess.run([sys.executable,str(driver),str(request_path)],capture_output=True,timeout=30)
    assert (tmp_path/'physical_calls.txt').read_text().count('call')==1
    if bad_json:
        assert result.returncode!=0 and not (job/'worker_result.json').exists()
    else:
        assert result.returncode==0,result.stderr.decode(errors='replace')
        response=json.loads((job/'worker_result.json').read_text(encoding='utf8'))
        assert not response['from_cache'] and response['stage1']['selections']['water_withdrawal']['value']=='120'
        incomplete=dict(response['report'],report_id='rp_'+'0'*31)
        (tmp_path/'runtime/reports/000_incomplete.json').write_text(json.dumps(incomplete),encoding='utf8')
        request.update(allow_external=False,max_calls=0)
        request_path.write_text(json.dumps(request),encoding='utf8')
        cached=subprocess.run([sys.executable,str(driver),str(request_path)],capture_output=True,timeout=30)
        assert cached.returncode==0,cached.stderr.decode(errors='replace')
        assert json.loads((job/'worker_result.json').read_text(encoding='utf8'))['from_cache']
        assert (tmp_path/'physical_calls.txt').read_text().count('call')==1
