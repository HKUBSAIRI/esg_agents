"""Qualitative applicability must remove noise without hiding actual problems."""
from copy import deepcopy
import pytest
from review_workbench.b_adapter import tasks_for,BWorkbench,qualitative_outside_numeric_review


def example():
    row={'record_uid':'r1','indicator_id':'water_v1:UNMAPPED','variable':'UNMAPPED','pid':'p1',
         'profile_id':'water_v1','profile_field':None,'year':2025,'scope_type':'GROUP',
         'logical_group_key':'p1|water_v1|UNKNOWN|2025|GROUP','value':None,'normalized_value':None,
         'upstream_metric_id':'water_policy','upstream':{'classification_status':'CLASSIFIED',
            'raw_value':None,'raw_text':'The Group has a water management policy.','evidence_ids':['e1'],'ambiguity_flags':[]}}
    bundle={'records':[row],'evidence':[{'evidence_id':'e1','content':'The Group has a water management policy.'}],
            'b_provenance':'import_unverified','b_source_files':{}}
    result={'status':'PARTIAL','findings':[],'record_verdicts':[{'record_uid':'r1','verdict':'INSUFFICIENT'}],
            'b_report':{'problems':[{'record_id':'r1','error_code':'LOGICAL_KEY_MISMATCH'}]}}
    return bundle,result


def test_qualitative_policy_has_no_automatic_numeric_review_task():
    bundle,result=example()
    assert tasks_for('ar1',bundle,result)==[]
    assert bundle['records'][0]['numeric_audit_applicability']=='NOT_APPLICABLE'
    assert result['record_verdicts'][0]['verdict']=='INSUFFICIENT'


@pytest.mark.parametrize('case',['numeric','unknown','ambiguous','no_evidence','flag','wrong_key','finding','failed','specific_problem'])
def test_actual_issues_and_other_unmapped_records_are_not_exempt(case):
    bundle,result=example();row=bundle['records'][0]
    if case=='numeric':row['value']='12';row['upstream']['raw_value']='12'
    elif case=='unknown':row['upstream_metric_id']=None
    elif case=='ambiguous':row['upstream']['classification_status']='NEEDS_REVIEW'
    elif case=='no_evidence':bundle['evidence']=[]
    elif case=='flag':row['upstream']['ambiguity_flags']=['YEAR_CONFLICT']
    elif case=='wrong_key':row['logical_group_key']='wrong'
    elif case=='finding':result['findings']=[{'record_uid':'r1','finding_id':'f1','severity':'WARN'}]
    elif case=='failed':result['status']='FAILED'
    else:result['b_report']['problems'].append({'record_id':'r1','error_code':'EVIDENCE_PID_MISMATCH'})
    assert not qualitative_outside_numeric_review(row,bundle,result)


def test_historical_open_noise_is_hidden_but_review_history_is_preserved():
    bundle,result=example()
    detail={'bundle':bundle,'result':result,'run_id':'ar1','events':[],
            'tasks':[{'task_id':'t1','record_uid':'r1','state':'OPEN'}]}
    historical=deepcopy(detail)
    current=BWorkbench(None).detail(detail)
    assert current['tasks']==[]
    assert current['numeric_review_applicability']['automatic_tasks_suppressed']==1
    historical['events']=[{'task_id':'t1','action':'DEFER'}]
    view=BWorkbench(None).detail(historical)
    assert view['tasks']==[] and len(view['archived_nonactionable_tasks'])==1 and len(view['events'])==1
