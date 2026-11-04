from dataclasses import make_dataclass
from decimal import Decimal
import pytest
from builders import case
from fake_client import FakeClient
from esg_semantic.audit.common import number, number_tokens, read_json, unit
from esg_semantic.audit.context import parse_audit_response
from esg_semantic.audit.prechecks import report_checks, normalization_ok
from test_auditor_checks import run_case, status


@pytest.mark.parametrize("raw,valid",[
    ('{"findings":[]}',True),('```json\n{"findings":[]}\n```',True),('answer {"findings":[]} done',True),
    ('no json',False),('{bad json}',False),(None,False),('x'*1_000_001,False),
    ({"findings":{},},False),({"findings":[],"review_status":"CONFIRMED"},False),
    ({"findings":[None]},False),({"findings":[{}]*201},False),
    ('{"findings":[],"findings":[]}',False),
])
def test_strict_json_and_one_bounded_recovery(raw,valid):
    findings,errors=parse_audit_response(raw,("pid","ind"),{})
    assert ("ERR_MODEL_OUTPUT_INVALID" not in errors)==valid


@pytest.mark.parametrize("raw",['NaN','{"value":1e9999}','{"value":1,"value":2}'])
def test_json_rejects_ambiguous_or_nonfinite(raw):
    with pytest.raises(ValueError): read_json(raw)


def test_numeric_tokens_not_substrings_and_parenthesis():
    assert number(True) is None
    assert number("(1,234)")==Decimal(-1234)
    assert number("not disclosed") is None
    assert Decimal(1234) not in number_tokens("value 12345 m3")
    assert number(None) is None
    assert unit(None) is None
    assert unit("m³")==unit("m3")
    assert Decimal(1234) in number_tokens("总取水量1234立方米")
    assert Decimal("-.5") in number_tokens("变化量-.5")
    assert Decimal(-1234) in number_tokens("变化量(1,234)")
    assert Decimal(3) not in number_tokens("m3")


@pytest.mark.parametrize("response",['{bad json}','```json\n{"findings":[]}\n```', {"text":'{"findings":[]}',"attempts":0}])
def test_model_parse_paths(response):
    result=run_case(client=FakeClient(response=response))
    if response=='```json\n{"findings":[]}\n```':
        assert status(result)=="CONFIRMED"
        assert any(p["error_code"]=="MODEL_JSON_RECOVERED" for p in result["problems"])
    else:
        assert status(result)=="INSUFFICIENT"


def test_report_checks_drop_dangling_sample_and_layout_noise():
    data=case()
    samples=[{"evidence_version_id":"missing","status":"EXCLUDE","numbers":[1]},
             {"evidence_version_id":"ev_b_1","status":"EXCLUDE","numbers":[1234],"noise_class":"PAGE_NUMBER"}]
    hints,problems,reviewed=report_checks(samples,None,{"ev_b_1":data["evidence"][0]},[],{"filter_sample_max":30,"key_indicators":["E2.1.1"]})
    assert hints==[] and reviewed==1 and problems


def test_global_coverage_hints_and_actual_count_prevent_false_missing():
    data=case(); coverage={"by_indicator":{"E2.1.1":{"n_records":0}},"unclassified_hints":[{"indicator_id":"E2.1.1","evidence_version_id":"ev_b_1"}]}
    p={"filter_sample_max":30,"key_indicators":["E2.1.1"]}
    assert report_checks([],coverage,{"ev_b_1":data["evidence"][0]},data["records"],p)[0]==[]
    assert report_checks([],coverage,{"ev_b_1":data["evidence"][0]},[],p)[0][0]["code"]=="A-MISSING-KEY"


def test_normalization_factor_from_to_and_unknown_target():
    data=case(); r=data["records"][0]
    r["normalization"]["from"]="kg"
    assert not normalization_ok(r)
    r["normalization"].update(from_unit="m3",to="kg")
    assert not normalization_ok(r)
    r["normalization"]={"status":"OK","from":"m3","to":"m3","factor":1000}
    assert not normalization_ok(r)


def test_unknown_conversion_does_not_create_false_block():
    data=case(); r=data["records"][0]
    r.update(raw_unit="customA",unit="customB",normalized_unit="customB",normalized_value=2468,
             normalization={"status":"OK","from":"customA","to":"customB","factor":2})
    data["evidence"][0]["unit_hint"]="customA"
    result=run_case(data)
    assert status(result)=="INSUFFICIENT"
    assert "A-NORMALIZATION-INVALID" not in {f["code"] for f in result["findings"]}


def test_partial_malformed_records_do_not_crash_grouping():
    data=case(); data["records"][0]["indicator_id"]=[]
    result=run_case(data)
    assert status(result)=="INSUFFICIENT"


def test_filter_status_conflict_is_not_silently_rewritten():
    data=case(); data["records"][0]["filter_status"]="REVIEW"
    assert status(run_case(data))=="INSUFFICIENT"
    assert data["records"][0]["filter_status"]=="REVIEW"


def test_cross_report_evidence_is_rejected():
    data=case(); data["evidence"][0]["pid"]="OTHER"
    assert run_case(data)["status"]=="FAILED"


def test_opaque_nonjson_object_and_nonfinite_inputs_fail_before_call():
    data=case(); data["records"][0]["value"]=float("inf")
    client=FakeClient(); assert run_case(data,client=client)["status"]=="FAILED"; assert client.calls==[]


def test_plain_exception_failed_client_and_bad_batch_reference():
    assert status(run_case(client=FakeClient(error=RuntimeError("secret"))))=="INSUFFICIENT"
    from builders import model_finding
    client=FakeClient(response={"findings":[model_finding(record_id="not_this_batch")]})
    result=run_case(client=client)
    assert status(result)=="INSUFFICIENT" and result["stats"]["invalid_record_reference"]==1


def test_oversized_context_never_sent_to_client():
    from esg_semantic.auditor import SemanticAuditor
    from esg_semantic.audit.integration import integration_context
    from esg_semantic.audit.common import resources
    data=case(); client=FakeClient()
    with integration_context(fixture=True):
        result=SemanticAuditor(client,resources()["policy"],catalogue={"E2.1.1":{"definition":"x"*180_001}}).audit_report(**data,run={})
    assert client.calls==[] and status(result)=="INSUFFICIENT"
    assert any(p["error_code"]=="ERR_MODEL_CONTEXT_LIMIT" for p in result["problems"])


def test_invalid_document_object_returns_structured_failure():
    data=case(); data["document"]=None
    assert run_case(data)["status"]=="FAILED"


def test_model_cannot_inject_internal_deterministic_metadata():
    from builders import model_finding
    client=FakeClient([model_finding(_source="DETERMINISTIC")])
    result=run_case(client=client)
    assert result["stats"]["invalid_finding_shape"]==1
    assert status(result)=="INSUFFICIENT"
