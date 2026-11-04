from copy import deepcopy

import pytest

from builders import case, model_finding
from esg_semantic.audit.common import policy_values, resources
from esg_semantic.audit.postprocess import postprocess, derive_review_status
from esg_semantic.audit.prechecks import precheck
from esg_semantic.audit.review_items import build_review_items


def process(findings, *, changes=None, hint_changes=None, record_changes=None, source="v1_numeric_blocks", **kwargs):
    data = case(); record = data["records"][0]; record["_reviewed"] = True
    record.update(record_changes or {})
    data["evidence"][0]["source_format"] = source
    if source == "v2": data["evidence"][0]["bbox"] = [10,10,100,100]
    hints = precheck(record, data["evidence"][0], data["decisions"][0], data["document"])
    for key, value in (hint_changes or {}).items():
        hints = [h for h in hints if h["check"] != key] + [{"check":key,"value":value}]
    policy = policy_values({**resources()["policy"], **(changes or {})})
    output = postprocess(findings, [record], policy, evidence_by_id={"ev_b_1":data["evidence"][0]},
                         hints_by_record={"rec_b_1":hints}, run={"audit_run_id":"ar_post","created_at":"2026-10-29T00:00:00Z"},
                         document=data["document"], **kwargs)
    return output, data


@pytest.mark.parametrize("code", list(resources()["codebook"]))
def test_all_nineteen_codes_have_defined_behavior(code):
    finding = model_finding(code)
    hint_changes, record_changes, kwargs = {}, {}, {}
    if code in ("A-MISSING-KEY","A-FILTER-OVERDROP"):
        finding.update(record_id=None,indicator_id="E2.1.1")
        kwargs = {"allowed_missing":{"E2.1.1":["ev_b_1"]}, "allowed_filter":{"ev_b_1"}}
    if code == "A-CLOSED-SET-GAP":
        finding["severity"] = "INFO"; record_changes["classification_status"] = "UNMAPPED"
    if code == "A-TRACE-MISSING":
        hint_changes["D11"] = "MISSING"
    if code == "A-DUPLICATE-UNMERGED": hint_changes["D7"] = True
    if code == "A-BOUNDARY-MISSING":
        (output, data) = process([finding])
        assert not output[0]
        return
    (findings, results, stats), data = process([finding], hint_changes=hint_changes, record_changes=record_changes,
                                            source="v2" if code == "A-TRACE-MISSING" else "v1_numeric_blocks", **kwargs)
    assert len(findings) == 1
    assert findings[0]["code"] == code
    assert results[0]["review_status"] != "CONFIRMED" or code in ("A-CLOSED-SET-GAP","A-MISSING-KEY","A-FILTER-OVERDROP")


@pytest.mark.parametrize("updates,counter", [
    ({"code":None},"invalid_finding_code"), ({"code":[]},"invalid_finding_code"),
    ({"code":"A-NOT-EXIST"},"invalid_finding_code"),
    ({"review_status":"CONFIRMED"},"invalid_finding_shape"),
    ({"issue_domain":"INVENTED"},"invalid_finding_shape"),
    ({"severity":"CRITICAL"},"invalid_finding_shape"),
    ({"confidence":True},"invalid_finding_shape"),
    ({"confidence":-1},"invalid_finding_shape"),
    ({"confidence":2},"invalid_finding_shape"),
    ({"confidence":"0.9"},"invalid_finding_shape"),
    ({"reason":" "},"invalid_finding_shape"),
    ({"reason":7},"invalid_finding_shape"),
    ({"reason":"a"*513},"invalid_finding_shape"),
    ({"proposed_action":"AUTO_EDIT"},"invalid_finding_shape"),
    ({"record_id":None},"invalid_record_reference"),
    ({"record_id":"other"},"invalid_record_reference"),
    ({"record_id":{}},"invalid_finding_shape"),
    ({"evidence_version_ids":[]},"invalid_finding_shape"),
    ({"evidence_version_ids":["no"]},"dangling_evidence"),
    ({"evidence_version_ids":[None]},"invalid_finding_shape"),
    ({"evidence_span":"not in source"},"invalid_proposal"),
    ({"evidence_span":"a"*301},"invalid_finding_shape"),
    ({"proposed_value":1234},"invalid_proposal"),
    ({"proposed_action":"CORRECT_VALUE","proposed_value":None},"invalid_proposal"),
    ({"proposed_action":"CORRECT_VALUE","proposed_value":1,"evidence_span":"1,234"},"invalid_proposal"),
    ({"proposed_action":"REMAP","proposed_variable":"unlisted"},"invalid_proposal"),
    ({"proposed_action":"REMAP"},"invalid_proposal"),
])
def test_invalid_findings_are_counted_and_do_not_create_pass(updates,counter):
    finding = model_finding(**updates)
    (findings, results, stats), _ = process([finding])
    assert findings == []
    assert stats[counter] == 1
    assert results[0]["review_status"] == "INSUFFICIENT"


def test_missing_required_model_field():
    finding = model_finding(); finding.pop("confidence")
    assert process([finding])[0][2]["invalid_finding_shape"] == 1


def test_unsupported_unit_block_becomes_warn():
    (findings,results,stats),_ = process([model_finding("A-UNIT-MISMATCH")])
    assert findings[0]["severity"] == "WARN"
    assert stats["unsupported_block"] == 1
    assert results[0]["review_status"] == "SUSPECT"


@pytest.mark.parametrize("code,check,value", [
    ("A-UNIT-MISMATCH","D4",False), ("A-YEAR-MISMATCH","D5",False),
    ("A-CROSS-RECORD","D10",True), ("A-NORMALIZATION-INVALID","D3",False),
    ("A-EVIDENCE-WEAK","D6",False), ("A-EXTRACT-VALUE","D13",False),
    ("A-EXTRACT-PAGE","D14",False),
])
def test_precheck_support_allows_block(code,check,value):
    output,_ = process([model_finding(code)],hint_changes={check:value})
    assert output[0][0]["severity"] == "BLOCK"
    assert output[1][0]["review_status"] == "REJECTED"


def test_v1_trace_missing_forbidden():
    output,_ = process([model_finding("A-TRACE-MISSING")])
    assert output[0] == []
    assert output[1][0]["review_status"] == "INSUFFICIENT"


def test_closed_gap_requires_info_and_unmapped():
    assert process([model_finding("A-CLOSED-SET-GAP", severity="INFO")])[0][0] == []
    assert process([model_finding("A-CLOSED-SET-GAP")],record_changes={"classification_status":"UNMAPPED"})[0][0] == []


def test_low_confidence_block_downgrade_and_context_priority():
    output,_ = process([model_finding(confidence=.69)])
    assert output[0][0]["severity"] == "WARN"
    assert output[2]["block_downgraded"] == 1
    output,_ = process([model_finding("A-CONTEXT-INSUFFICIENT",severity="WARN")])
    assert output[1][0]["review_status"] == "INSUFFICIENT"


def test_zero_candidate_with_actual_quote_is_preserved():
    finding=model_finding(proposed_action="CORRECT_VALUE",proposed_value=1234,evidence_span="1,234")
    output,_=process([finding])
    assert output[0][0]["proposed_value"] == 1234
    # Zero is tested against real source 0, not a substring inside 2025.
    from esg_semantic.audit.postprocess import postprocess
    data=case(); data["evidence"][0]["content"]="实际值 0 m3"; data["records"][0]["_reviewed"]=True
    finding.update(proposed_value=0,evidence_span="实际值 0 m3")
    findings,_,_=postprocess([finding],data["records"],policy_values(resources()["policy"]), evidence_by_id={"ev_b_1":data["evidence"][0]},document=data["document"])
    assert findings[0]["proposed_value"] == 0


def test_remap_uses_catalogue():
    finding=model_finding(proposed_action="REMAP",proposed_variable="allowed")
    output,_=process([finding],catalogue={"E2.1.1":{"variables":["allowed"]}})
    assert output[0][0]["proposed_variable"] == "allowed"


def test_report_signals_required_and_distinct_null_records_kept():
    missing=model_finding("A-MISSING-KEY",record_id=None,indicator_id="E2.1.1")
    overdrop=model_finding("A-FILTER-OVERDROP",record_id=None,indicator_id=None)
    output,_=process([missing,overdrop])
    assert output[2]["missing_key_without_signal"]==1
    assert output[2]["overdrop_without_sample"]==1
    output,_=process([missing,overdrop],allowed_missing={"E2.1.1":["ev_b_1"]},allowed_filter={"ev_b_1"})
    assert len(output[0])==2


def test_deterministic_preferred_and_duplicates_counted():
    weak=model_finding(severity="WARN",confidence=.8,_source="DETERMINISTIC")
    strong=model_finding(confidence=.99)
    output,_=process([strong,weak])
    assert output[0][0]["severity"]=="WARN"
    assert output[0][0]["model_call_id"] is None
    assert output[2]["duplicated_findings"]==1


def test_invalid_finding_does_not_erase_valid_block():
    output,_=process([model_finding("BAD"),model_finding()])
    assert output[1][0]["review_status"]=="REJECTED"


def test_model_cross_batch_reference_rejected():
    output,_=process([model_finding(_invalid_batch_record=True,_batch_record_ids=["rec_b_1"])])
    assert output[2]["invalid_record_reference"]==1
    assert output[1][0]["review_status"]=="INSUFFICIENT"


def test_evidence_not_in_prompt_rejected():
    output,_=process([model_finding(_allowed_evidence=[])])
    assert output[2]["dangling_evidence"]==1


@pytest.mark.parametrize("code,confidence,severity,reason", [
    ("A-IND-MISMATCH",True,"BLOCK","x"), ("A-IND-MISMATCH",None,"BLOCK","x"),
    ("A-IND-MISMATCH",1,"BAD","x"), ("A-IND-MISMATCH",1,"BLOCK",None),
    ("A-IND-MISMATCH",1,"BLOCK",""),
])
def test_internal_candidate_validation_is_not_bypassed(code,confidence,severity,reason):
    output,_=process([model_finding(code,confidence=confidence,severity=severity,reason=reason,_source="DETERMINISTIC")])
    assert output[0]==[]


def test_status_truth_table():
    assert derive_review_status([], {"_insufficient":True,"_reviewed":True})=="INSUFFICIENT"
    assert derive_review_status([], {})=="NOT_REVIEWED"
    assert derive_review_status([], {"_reviewed":True})=="CONFIRMED"


def test_TC_A_27_review_items_for_v1_and_v2():
    output,data=process([model_finding()])
    findings,results,_=output
    rows=build_review_items(findings,results,data["records"],{"ev_b_1":data["evidence"][0]})
    assert len(rows)==1 and rows[0]["bbox"] is None and rows[0]["priority"]=="P0"
    output,data=process([model_finding()],source="v2")
    rows=build_review_items(output[0],output[1],data["records"],{"ev_b_1":data["evidence"][0]})
    assert rows[0]["bbox"]==[10,10,100,100]


def test_info_default_hidden_and_optional_view():
    output,data=process([model_finding("A-CLOSED-SET-GAP",severity="INFO")],record_changes={"classification_status":"UNMAPPED"})
    args=(output[0],output[1],data["records"],{"ev_b_1":data["evidence"][0]})
    assert build_review_items(*args)==[]
    assert build_review_items(*args,include_info=True)[0]["priority"]=="P2"
