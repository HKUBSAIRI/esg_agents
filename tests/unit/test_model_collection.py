from copy import deepcopy
import hashlib
import json

import pytest

from esg_pipeline.model_collection import validate_proposal,review_outcome,run_model_collection,model_observations
from esg_pipeline.water_template import contract
from esg_pipeline.annual import select_primary


def fixture():
    text='In 2025, the Group recycled and reused a total of 79,651,697.9 tonnes of water.'
    source={'native':{1:'Stock code: 807',29:text},'pages':[29],'evidence':[]}
    proposal={'field':'water_recycled_reused_m3','year':2025,'value_text':'79,651,697.9','unit':'tonnes','page':29,
              'quote':text,'group_total':True,'scope':'Group','water_type':'ordinary_water'}
    review={'decision':'ACCEPT','field':proposal['field'],'year':2025,'value_text':proposal['value_text'],'unit':'tonnes',
            'group_total':True,'year_checked':True,'period_checked':True,'scope_checked':True,'definition_checked':True,
            'ordinary_water':True,'source_quote':text}
    return source,proposal,review


def test_research_value_and_explicit_approximation():
    sources,proposal,review=fixture();spec=contract()
    item=validate_proposal(proposal,sources,spec)
    assert not item['validation_issues']
    out=review_outcome(item,review,sources,spec)
    assert out['status']=='MODEL_REVIEWED' and out['normalized_value']=='79651697.9'
    assert 'WATER_TONNE_TO_M3_APPROXIMATION' in out['quality_flags']
    assert out['normalization']['approximate']


@pytest.mark.parametrize('changes',[{'decision':'REJECT'},{'year':2024},{'value_text':'1'},{'unit':'m3'},
    {'group_total':False},{'period_checked':False},{'definition_checked':False},{'ordinary_water':False}])
def test_b_disagreement_or_insufficient_never_fills(changes):
    sources,proposal,review=fixture();spec=contract()
    item=validate_proposal(proposal,sources,spec)
    assert review_outcome(item,{**review,**changes},sources,spec)['status']=='WITHHELD'


def test_source_quote_fabrication_and_text_not_independent():
    source,p,r=fixture();spec=contract()
    assert validate_proposal({**p,'quote':'made up 79,651,697.9'},source,spec)['validation_issues']
    item=validate_proposal(p,source,spec)
    source['native'][29]='No readable text';source['evidence']=[{'page_no':29,'content':p['quote']}]
    item=validate_proposal(p,source,spec)
    assert review_outcome(item,r,source,spec)['status']=='WITHHELD'
    assert review_outcome(item,r,source,spec,has_image=True)['status']=='MODEL_REVIEWED'


def test_cached_ids_stable_and_invalid_shapes_rejected():
    s,p,r=fixture();spec=contract()
    assert validate_proposal(p,s,spec)['proposal_id']==validate_proposal(p,s,spec)['proposal_id']
    assert validate_proposal({**p,'page':[]},s,spec) is None
    assert validate_proposal({**p,'field':[]},s,spec) is None


def test_money_rebinding_requires_exact_source_equivalence():
    text='Operating revenue of RMB 7.073 billion'
    source={'native':{4:text},'pages':[4],'evidence':[]}
    p={'field':'revenue_million','year':2025,'value_text':'7073','unit':'RMB million','page':4,'quote':text,'group_total':True}
    result=validate_proposal(p,source,contract())
    assert result['value_text']=='7.073' and result['unit']=='RMB billion'
    assert result['a_original_value_text']=='7073' and not result['validation_issues']
    bad=validate_proposal({**p,'value_text':'7074'},source,contract())
    assert 'NUMBER_NOT_IN_QUOTE' in bad['validation_issues']
    review={'decision':'ACCEPT','field':'revenue_million','year':2025,'value_text':'7073','unit':'RMB million',
            'group_total':True,'year_checked':True,'period_checked':True,'scope_checked':True,'definition_checked':True,'source_quote':text}
    out=review_outcome(result,review,source,contract())
    assert out['status']=='MODEL_REVIEWED' and out['normalized_value']=='7073.000'
    assert out['money_representation_equivalent']
    assert review_outcome(result,{**review,'unit':'HKD million'},source,contract())['status']=='WITHHELD'


def test_complete_mock_a_b_sidecar_and_collection_tier(tmp_path,monkeypatch):
    source,p,review=fixture()
    pdf=tmp_path/'source.pdf';pdf.write_bytes(b'PDF fixture; not opened in text-only mock')
    sha=hashlib.sha256(pdf.read_bytes()).hexdigest()
    summary={'output_directory':str(tmp_path),'source_pdf':'00807_test.pdf','source_sha256':sha,'run_id':'test'}
    source.update(summary=summary,pdf=pdf,sha256=sha,metadata=[],candidates=[])
    monkeypatch.setattr('esg_pipeline.model_collection.source_bundle',lambda *a,**k:source)
    class Client:
        def request(self,stage,data,instruction,**kwargs):
            if stage=='collection_select':return {'data':{'proposals':[p],'missing':[]}}
            return {'data':{'reviews':[{**review,'proposal_id':data['proposals'][0]['proposal_id']}]}}
    result=run_model_collection(summary,client=Client(),a_calls=1,b_calls=1,vision=False)
    assert result['outcomes'][0]['status']=='MODEL_REVIEWED'
    observations,meta=model_observations(summary,contract()['template_sha256'])
    selected=select_primary(observations)
    assert selected['value']=='79651697.9' and selected['status']=='MODEL_REVIEWED'
    assert not result['scoring_enabled']
    pointer=tmp_path/'model_collection/latest.json'
    path=json.loads(pointer.read_text(encoding='utf8'))['path']
    from pathlib import Path
    Path(path).write_text('{}',encoding='utf8')
    with pytest.raises(ValueError,match='CHANGED'):model_observations(summary,contract()['template_sha256'])
