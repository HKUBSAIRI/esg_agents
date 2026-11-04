from copy import deepcopy
from review_workbench.review_policy import plan
from review_workbench.b_adapter import tasks_for


def inputs():
    return {'records':[{'record_uid':'r1','indicator_id':'water_v1:water_discharge_m3','value':'560','unit':'m3'}],'evidence':[]}, {
        'status':'PARTIAL','findings':[],'record_verdicts':[{'record_uid':'r1','verdict':'INSUFFICIENT'}],
        'b_report':{'problems':[{'record_id':'r1','error_code':'INDEPENDENT_SOURCE_REVIEW_REQUIRED','message':'未独立核对原页'},
                               {'record_id':'r1','error_code':'LOGICAL_KEY_MISMATCH','message':'适配格式不一致'}]}}


def test_unverified_560_is_not_a_manual_task_and_never_becomes_confirmed():
    b,r=inputs();original=deepcopy(r)
    assert tasks_for('ar_test',b,r)==[]
    tasks,state=plan(b,r)
    assert tasks==[] and state['unverified_records']==['r1'] and state['system_problems']
    assert r==original


def test_real_discrepancy_has_reason_question_and_evidence():
    b,r=inputs()
    r['findings']=[{'record_uid':'r1','finding_id':'f1','code':'A-EXTRACT-VALUE','severity':'BLOCK',
        'reason':'原表为560，记录为5600，数量级相差10倍。','observed':'5600','expected':'560','evidence_span':'560.00','page_no':35}]
    tasks=tasks_for('ar_test',b,r)
    assert len(tasks)==1 and tasks[0]['review_reason'] and tasks[0]['review_question']
    assert tasks[0]['review_evidence'][0]['page']==35 and tasks[0]['priority']=='P0'


def test_generic_weakness_is_not_treated_as_a_data_discrepancy():
    b,r=inputs()
    r['findings']=[{'record_uid':'r1','finding_id':'f1','code':'A-EVIDENCE-WEAK','severity':'WARN','reason':'尚未独立核实'}]
    assert tasks_for('ar_test',b,r)==[]
    r['findings'][0].update(observed='5600',expected='560',evidence_span='560.00')
    assert len(tasks_for('ar_test',b,r))==1


def test_mislabeled_extract_error_without_a_discrepancy_is_not_manual():
    b,r=inputs()
    r['findings']=[{'record_uid':'r1','finding_id':'f1','code':'A-EXTRACT-VALUE','severity':'BLOCK','reason':'提取候选尚未独立核对原页'}]
    assert tasks_for('ar_test',b,r)==[]


def test_group_conflict_requires_review_but_different_scope_does_not():
    b,r=inputs()
    first=b['records'][0]
    first.update(profile_field='water_discharge_m3',scope_type='GROUP',year=2025,normalized_value='560')
    second={**first,'record_uid':'r2','normalized_value':'5600'}
    b['records'].append(second)
    r['record_verdicts'].append({'record_uid':'r2','verdict':'INSUFFICIENT'})
    tasks=tasks_for('ar_test',b,r)
    assert len(tasks)==1 and '5600' in tasks[0]['review_reason']
    second['scope_type']='UNKNOWN'
    assert tasks_for('ar_test',b,r)==[]
