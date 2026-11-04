"""Unified workbench HTTP and stage execution tests. All models are forbidden/mocked."""
from concurrent.futures import Future
from copy import deepcopy
import io
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import fitz
import pytest

from review_workbench.service import Application
from review_workbench.store import Store, Conflict, NotFound
from review_workbench.contracts import ContractError
from review_workbench.pipeline import save
from review_workbench.server import LocalServer


def pdf_bytes():
    with fitz.open() as pdf:
        page = pdf.new_page(width=600, height=400)
        page.insert_text((30,40), "Sustainability Report 2025")
        for x in (30,300,390,480,570):
            page.draw_line((x,70),(x,190))
        for y in (70,110,150,190):
            page.draw_line((30,y),(570,y))
        for y, row in ((95,["Metric","Year","Unit","Value"]),(135,["Total water withdrawal","2025","m3","1,234"]),(175,["Recycled water used","2024","m3","120"])):
            for x,text in zip((40,310,400,490),row):
                page.insert_text((x,y),text,fontsize=10)
        return pdf.tobytes()


@pytest.fixture
def app(tmp_path):
    app = Application(Store(tmp_path / "db"))
    yield app
    app.close()


def options(source, **overrides):
    return {"engine":"legacy","source_id":source,"mode":"rules_only","vision":False,"allow_external":False,
            "vision_calls":0,"a_calls":0,"b_calls":0,**overrides}


@pytest.mark.parametrize("bad",[{"mode":"invalid"},{"vision":True},{"allow_external":True},{"a_calls":True},
    {"vision_calls":101},{"mode":"model","allow_external":False},{"source_id":"C:/secret.pdf"}])
def test_bad_options_or_untrusted_paths_never_start_jobs(app,bad):
    upload=app.pipeline.upload(pdf_bytes(),"sample.pdf")
    with pytest.raises((ContractError, NotFound)):
        app.pipeline.start(options(upload["id"],**bad))
    assert not list(app.pipeline.directory.glob('job_*.json'))


def test_full_uploaded_pdf_local_pipeline_persistence_export_and_images(app,monkeypatch):
    def forbidden(*args,**kwargs):
        pytest.fail("Local workflow attempted a paid API client")
    monkeypatch.setattr('esg_pipeline.runner.JsonClient',forbidden)
    upload=app.pipeline.upload(pdf_bytes(),'新报告.pdf')
    job=app.pipeline.start(options(upload['id']))
    app.active.result(timeout=30)
    view=app.pipeline.public_job(job)
    assert view['status']=='PARTIAL',view
    assert view['usage']['calls']==0 and view['result_run_id']
    assert {'EXTRACT','FILTER','CLASSIFY','AUDIT','VALIDATE','IMPORT_REVIEW'} <= set(view['stages'])
    run=view['result_run_id']
    detail=app.detail(run)
    assert detail['pipeline_available'] and detail['has_pdf']
    assert detail['bundle']['report_context']['company_name']=='新报告'
    extracted=app.pipeline.results(run)
    assert extracted['available'] and extracted['total']>0
    classified=app.pipeline.results(run,'classify')
    assert classified['total']>0
    assert app.pipeline.page_image(run,1).startswith(b'\x89PNG')
    with pytest.raises(ContractError):app.pipeline.results(run,offset=-1)
    with zipfile.ZipFile(io.BytesIO(app.pipeline.export(run))) as archive:
        assert 'a/quantitative_candidates.jsonl' in archive.namelist()
        assert 'review_workbench.zip' in archive.namelist()
    assert Path(app.pipeline.job(job)['source']['_source']).is_file()
    other=Application(Store(app.store.directory))
    try:
        other.pipeline.recover_interrupted()
        assert other.pipeline.public_job(job)['status']=='PARTIAL'
        assert other.pipeline.results(run)['total']==extracted['total']
    finally:other.close()


def test_shared_execution_lock_blocks_pipeline_and_upload(app):
    upload=app.pipeline.upload(pdf_bytes(),'sample.pdf')
    app.active=Future()
    try:
        with pytest.raises(Conflict):app.pipeline.start(options(upload['id']))
        with pytest.raises(Conflict):app.pipeline.upload(pdf_bytes(),'second.pdf')
    finally:app.active.set_result(None)


def test_model_collection_job_consent_budget_and_reuses_run(app,monkeypatch):
    upload=app.pipeline.upload(pdf_bytes(),'00807_fixture.pdf')
    identity=app.pipeline.start(options(upload['id']));app.active.result(timeout=30)
    run=app.pipeline.public_job(identity)['result_run_id']
    body={'run_id':run,'a_calls':1,'b_calls':1,'vision':True,'allow_external':True}
    with pytest.raises(ContractError):app.pipeline.start_collection({**body,'allow_external':False})
    with pytest.raises(ContractError):app.pipeline.start_collection({**body,'a_calls':True})
    seen={}
    def model(summary,**kwargs):
        seen['run']=summary['workbench_run_id'];seen['vision']=kwargs['vision']
        kwargs['progress']('COLLECTION_A',{'pipeline_directory':str(app.store.directory),'message':'mock'})
        return {'directory':str(app.store.directory)}
    monkeypatch.setattr('esg_pipeline.model_collection.run_model_collection',model)
    job=app.pipeline.start_collection(body);app.active.result(timeout=30)
    result=app.pipeline.public_job(job)
    assert result['status']=='PARTIAL' and result['result_run_id']==run
    assert seen=={'run':run,'vision':True}
    assert result['usage']['calls']==0


def test_interruption_and_cancel_are_persisted_without_retry(app,monkeypatch):
    upload=app.pipeline.upload(pdf_bytes(),'sample.pdf')
    monkeypatch.setattr(app.executor,'submit',lambda *args:Future())
    identity=app.pipeline.start(options(upload['id']))
    app.pipeline.cancel(identity)
    assert app.pipeline.public_job(identity)['cancel_requested']
    app.pipeline._run(identity)
    assert app.pipeline.public_job(identity)['status']=='CANCELLED'
    app.active.set_result(None)
    path=app.pipeline.directory/'job_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'
    interrupted=app.pipeline.job(identity)
    interrupted.update(job_id=path.stem,status='RUNNING',cancel_requested=False)
    save(path,interrupted)
    app.pipeline.recover_interrupted()
    assert app.pipeline.public_job(path.stem)['status']=='INTERRUPTED'


def test_failure_keeps_upload_and_safe_message(app,monkeypatch):
    upload=app.pipeline.upload(pdf_bytes(),'sample.pdf')
    def broken(*args,**kwargs):raise RuntimeError('NEVER_ECHO_SECRET')
    monkeypatch.setattr('esg_extraction.extractor.extract_pdf',broken)
    identity=app.pipeline.start(options(upload['id']))
    app.active.result(timeout=15)
    view=app.pipeline.public_job(identity)
    assert view['status']=='FAILED' and 'NEVER_ECHO_SECRET' not in json.dumps(view)
    assert Path(app.pipeline.job(identity)['source']['_source']).is_file()


def test_model_mode_routes_vision_a_and_b_to_bounded_mock_clients(app,monkeypatch):
    seen={}
    monkeypatch.setattr(app.pipeline,'capabilities',lambda:{'available':True,'api_key_configured':True})
    class Vision:
        model='MOCK-NO-NETWORK'
        def __init__(self,**kwargs):
            seen['vision_budget']=kwargs['max_calls'];self.calls=0;self.max_calls=kwargs['max_calls']
        def __call__(self,image):
            self.calls+=1
            return {'data':{'page_status':'EXTRACTED','warnings':[],'regions':[{'kind':'table','bbox':[0,0,1000,1000],'context':'Group 2025 water use',
                'observations':[{'metric_label':'Water use','value_text':'100','unit':'m3','year':2025,'scope':'Group','value_kind':'LABELLED'}]}]},'usage_tokens':0}
    class Semantic:
        def __init__(self,*args,**kwargs):seen['semantic_budget']=kwargs['max_calls']
        def request(self,stage,data,prompt):
            seen['a_stage']=stage
            return {'data':{'assignments':[{'candidate_id':c['candidate_id'],'field':'water_consumption_m3','status':'ASSIGNED','reason':'mock','quote':c['label']} for c in data['candidates']]},'call_id':'mock','usage':{}}
        def review(self,prompt,**kwargs):
            seen['b_called']=True
            return {'findings':[]}
    monkeypatch.setattr('esg_extraction.vision.AliVisionClient',Vision)
    monkeypatch.setattr('esg_pipeline.runner.JsonClient',Semantic)
    uploaded=app.pipeline.upload(pdf_bytes(),'model-mock.pdf')
    identity=app.pipeline.start(options(uploaded['id'],mode='model',vision=True,allow_external=True,vision_calls=1,a_calls=1,b_calls=1))
    app.active.result(timeout=30)
    assert app.pipeline.public_job(identity)['status']=='PARTIAL'
    assert seen=={'vision_budget':1,'semantic_budget':2,'a_stage':'classify','b_called':True}


def test_http_upload_run_poll_and_no_arbitrary_paths(app):
    server=LocalServer(('127.0.0.1',0),app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def post(path,body,mime='application/json',token=None):
        raw=json.dumps(body).encode() if mime=='application/json' else body
        req=Request(base+path,data=raw,headers={'Content-Type':mime,'X-Workbench-Token':token or server.token,'X-Filename':'sample.pdf'})
        with urlopen(req,timeout=15) as response:return json.load(response)
    try:
        with pytest.raises(HTTPError) as error:post('/api/pipeline/upload',pdf_bytes(),'application/pdf','bad-token')
        assert error.value.code==403
        with pytest.raises(HTTPError) as error:post('/api/pipeline/upload',b'%PDF-broken','application/pdf')
        assert error.value.code==400
        uploaded=post('/api/pipeline/upload',pdf_bytes(),'application/pdf')
        identity=post('/api/pipeline/jobs',options(uploaded['id']))['job_id']
        app.active.result(timeout=30)
        with urlopen(base+'/api/pipeline/jobs/'+identity) as response:view=json.load(response)
        assert view['status']=='PARTIAL'
        with urlopen(base+'/api/pipeline/runs/'+view['result_run_id']+'?limit=1') as response:result=json.load(response)
        assert len(result['rows'])==1
        with urlopen(base+'/api/pipeline/runs/'+view['result_run_id']+'/annual') as response:annual=json.load(response)
        assert annual['api_calls']==0 and not annual['scoring_enabled']
        with urlopen(base+'/api/pipeline/runs/'+view['result_run_id']+'/water-2025.xlsx',timeout=30) as response:
            raw=response.read()
            assert response.headers['Content-Type'].startswith('application/vnd.openxmlformats')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:assert 'xl/workbook.xml' in archive.namelist()
        with pytest.raises(HTTPError) as error:urlopen(base+'/api/pipeline/runs/not-a-run/annual')
        assert error.value.code==404
        with pytest.raises(HTTPError) as error:urlopen(base+'/api/pipeline/runs/'+view['result_run_id']+'?offset=bad')
        assert error.value.code==400
        with urlopen(base+'/pipeline.js') as response:assert response.status==200
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
