from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
from zipfile import ZipFile

import fitz
import pytest

from esg_pipeline.annual import build_collection, inspect_report, persist, select_primary
from esg_pipeline.company_master import identify,load_master
from esg_pipeline.field_rules import money,semantic_issues,standardize
from esg_pipeline.native_binding import bind,cell_facts,period_context
from esg_pipeline.profiles import load_profile
from esg_pipeline.water_template import contract,FIELDS,ROOT,sheets
from esg_pipeline.workbook_export import portable_bytes


@pytest.mark.parametrize('raw,unit,expected,currency', [('1200','HKD million','1200','HKD'),('1200',"RMB\u2019000",'1.200','CNY'),('3','CNY billion','3000','CNY'),('250','人民币万元','2.50','CNY'),('3','人民币亿元','300','CNY')])
def test_original_currency_and_magnitude(raw,unit,expected,currency):
    value,actual,error=money(raw,unit)
    assert Decimal(value)==Decimal(expected) and actual==currency and error is None


def test_ambiguous_currency_not_guessed():
    assert money('3','$ million')[2]=='CURRENCY_UNRESOLVED'
    assert money('3','HKD USD million')[2]=='CURRENCY_UNRESOLVED'


@pytest.mark.parametrize('unit,expected',[('mL','.000001'),('ML','1000'),('kL','1'),('GL','1000000')])
def test_case_sensitive_volumes(unit,expected):
    assert Decimal(standardize('water_withdrawal_m3','1',unit)[0])==Decimal(expected)


def test_water_tonnes_and_ratio_require_conditions():
    assert standardize('water_withdrawal_m3','3','t')[0] is None
    assert standardize('water_withdrawal_m3','3','t','fresh water')[2]['approximate']
    assert standardize('water_withdrawal_m3','3','t','fresh water and seawater')[0] is None
    assert standardize('water_recycling_rate_pct','.85','fraction')[0]=='85.00'
    assert standardize('water_recycling_rate_pct','85','%')[0]=='85'


def test_metric_semantics():
    assert 'CONSUMPTION_DEFINITION_UNPROVEN' in semantic_issues('water_consumption_m3','Water consumption','Water consumption')
    assert not semantic_issues('water_consumption_m3','Water consumption','Water evaporated and not returned')
    assert semantic_issues('cod_discharge_t','COD removal','removal of pollutants')
    assert semantic_issues('water_recycled_reused_m3','Purchased reclaimed water','')


def observation(value='0',**changes):
    return {'normalized_value':value,'currency':None,'period_start':'2025-01-01','period_end':'2025-12-31',
            'reasons':[],'observation_id':'x','source_bound':True,**changes}


def test_selection_zero_duplicates_conflicts_and_insufficient():
    assert select_primary([observation()])['value']=='0'
    assert select_primary([observation(),observation('0.0',observation_id='y')])['status']=='AUTO_VERIFIED'
    assert select_primary([observation(),observation('1')])['status']=='CONFLICT'
    assert select_primary([observation(reasons=['B_INSUFFICIENT'])])['value'] is None
    assert select_primary([])['value'] is None
    assert select_primary([observation(),observation('1',reasons=['B_SUSPECT'])])['status']=='CONFLICT'


def test_identity_does_not_trust_filename():
    master=load_master()
    assert identify('00807_x.pdf','Other Company',master)['status']!='MATCHED'
    assert identify('00807_x.pdf','Stock code: 807',master)['status']=='MATCHED'
    assert identify('00807_x.pdf','Stock code: 125',master)['status']!='MATCHED'


def test_period_is_not_publication_year():
    assert period_context(['Sustainability Report 2025'])['year'] is None
    assert period_context(['For the year ended 31 March 2025'])['start']=='2024-04-01'
    assert period_context(['year ended 31 March 2025 and 18-month period'])['status'].startswith('NONSTANDARD')
    assert period_context(['The reporting period is 1 April 2024 to 31 March 2025'])['start']=='2024-04-01'
    assert period_context(['Our target is 1 April 2024 to 31 March 2025'])['year'] is None


def test_native_binding_positive_and_wrong_year_or_value(tmp_path):
    path=tmp_path/'report.pdf'
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=500)
        page.insert_text((30,35),'For the year ended 31 December 2025')
        page.insert_text((350,35),'Stock code: 807')
        page.insert_text((30,60),'Total water withdrawal of the Group, all operations')
        cells=[]
        for r,row in enumerate([['Metric','Unit','2025','2024'],['Water withdrawal','m3','100','80']]):
            for c,text in enumerate(row):
                x=[30,280,360,450][c];y=100+r*40
                page.insert_text((x,y),text,fontsize=10)
                cells.append({'row':r,'column':c,'text':text,'bbox':[x-1,y-12,x+150 if c==0 else x+65,y+5],'raw_is_null':False})
        pdf.save(path)
    table={'strategy':'lines','table_id':'t1','page_no':1,'bbox':[25,80,540,160],'cells':cells,'context_before':'','context_after':''}
    facts,pages,period=cell_facts(path,[table],load_profile())
    assert len(facts)==2 and facts[0]['scope_quote']
    c={'source_kind':'native_table','table_id':'t1','row':1,'column':2,'value_decimal':'100','page_no':1,'year':2025}
    assert bind(c,facts,load_profile())['year']==2025
    assert bind({**c,'year':2024},facts,load_profile()) is None
    assert bind({**c,'value_decimal':'101'},facts,load_profile()) is None
    # Synthetic A/B fixture: a confirmed, source-bound value can enter, whereas
    # changing B to insufficient must close the gate. No production files edited.
    import hashlib
    a=tmp_path/'a';a.mkdir()
    extraction=tmp_path/'extraction';extraction.mkdir()
    (extraction/'tables.jsonl').write_text(json.dumps(table)+'\n',encoding='utf8')
    c.update(candidate_id='c1',unit='m3')
    (a/'quantitative_candidates.jsonl').write_text(json.dumps(c)+'\n',encoding='utf8')
    record={'record_id':'r1','candidate_id':'c1','profile_field':'water_withdrawal_m3','classification_status':'ASSIGNED',
            'pid':'p1','evidence_version_id':'ev1','source_page':1,'raw_value':'100','raw_unit':'m3','year':2025}
    (a/'records.jsonl').write_text(json.dumps(record)+'\n',encoding='utf8')
    document={'pdf_path':str(path),'pdf_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    (a/'document_context.json').write_text(json.dumps(document),encoding='utf8')
    manifest={'source_directory':str(extraction),'a_artifact_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in a.iterdir()}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest),encoding='utf8')
    audit=tmp_path/'audit_report.json'
    audit.write_text(json.dumps({'record_results':[{'record_id':'r1','review_status':'CONFIRMED'}]}),encoding='utf8')
    summary={'output_directory':str(tmp_path),'source_pdf':'00807_test.pdf','run_id':'synthetic','b_result':{'output_directory':str(tmp_path)}}
    inspected=inspect_report(summary,load_master())
    assert select_primary(inspected['observations'])['value']=='100',inspected['observations']
    audit.write_text(json.dumps({'record_results':[{'record_id':'r1','review_status':'INSUFFICIENT'}]}),encoding='utf8')
    assert select_primary(inspect_report(summary,load_master())['observations'])['value'] is None


def test_template_contract_and_portable_preservation(tmp_path):
    spec=contract()
    source=Path(spec['template_path']);before=source.read_bytes()
    values=dict.fromkeys(FIELDS);values.update(stock_code='00807',company_name='Company',sasb_sector='Sector',sasb_industry='Industry',water_recycling_rate_pct='85',water_withdrawal_m3='0')
    collection={'contract':spec,'rows':[{'year':2025,'values':values}]}
    raw=portable_bytes(collection)
    part=sheets(source)['2025']['part']
    with ZipFile(source) as original,ZipFile(io.BytesIO(raw)) as output:
        assert set(original.namelist())==set(output.namelist())
        for name in original.namelist():
            if name!=part:assert original.read(name)==output.read(name)
        assert b'<ns0:f' not in output.read(part)
        import xml.etree.ElementTree as ET
        declarations=dict(item for _,item in ET.iterparse(io.BytesIO(output.read(part)),events=['start-ns']))
        xml=ET.fromstring(output.read(part))
        ignorable=xml.attrib.get('{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable','').split()
        assert all(prefix in declarations for prefix in ignorable)
    out=tmp_path/'test.xlsx';out.write_bytes(raw)
    row=sheets(out)['2025']['rows'][2]
    assert row['A2']=='00807' and row['I2']=='85' and row['G2']=='0' and 'H2' not in row
    assert source.read_bytes()==before


def test_reuse_ten_reports_without_api_and_immutable_batches(tmp_path,monkeypatch):
    monkeypatch.setattr('esg_pipeline.llm.JsonClient',lambda **kw:pytest.fail('No API allowed'))
    summaries=json.loads((ROOT/'data/esg_pipeline/latest_run.json').read_text(encoding='utf8'))['reports']
    collection=build_collection(summaries)
    assert collection['statistics']['companies']==10 and len(collection['rows'])==20
    assert collection['api_calls']==0 and not collection['scoring_enabled']
    assert all(s['status']!='AUTO_VERIFIED' for row in collection['rows'] for s in row['states'].values())
    assert all(s['value'] is None or s['status']=='MODEL_REVIEWED' for row in collection['rows'] for s in row['states'].values())
    first=persist(collection,tmp_path)
    assert first.is_file()
    with sqlite3.connect(tmp_path/'water.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM company_years').fetchone()[0]==20
    with pytest.raises(FileExistsError):persist(collection,tmp_path)
