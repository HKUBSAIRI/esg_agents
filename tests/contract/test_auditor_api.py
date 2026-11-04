from copy import deepcopy
from dataclasses import make_dataclass

import pytest

from builders import case
from fake_client import FakeClient
from esg_semantic.auditor import SemanticAuditor, audit_classification
from esg_semantic.audit.common import mapping, policy_values, resources
from esg_semantic.audit.integration import integration_context


def test_group_and_report_have_same_record_results():
    data=case()
    with integration_context(fixture=True):
        auditor=SemanticAuditor(FakeClient(),resources()["policy"])
        run={"run_id":"same","created_at":"2026-10-29T00:00:00Z"}
        report=auditor.audit_report(**data,run=run)
        grouped=auditor.audit_group(("SYNTHETIC-B-001","E2.1.1"),data["records"],
            {e["evidence_version_id"]:e for e in data["evidence"]},
            {d["evidence_version_id"]:d for d in data["decisions"]},data["document"],run=run)
    assert report["record_results"]==grouped["record_results"]


def test_wrong_group_rejected():
    data=case()
    with integration_context(fixture=True):
        auditor=SemanticAuditor(FakeClient(),resources()["policy"])
        with pytest.raises(ValueError): auditor.audit_group(("wrong","wrong"),data["records"],{},{},data["document"],run={})


def test_empty_report_and_single_entry_function():
    data=case(); data["records"]=[]
    with integration_context(fixture=True):
        outcome=audit_classification(**data,policy=resources()["policy"],run={},llm=FakeClient())
    assert outcome["record_results"]==[] and outcome["coverage"]["records_total"]==0


def test_formal_mode_requires_external_binding():
    with pytest.raises(ValueError):
        with integration_context(): pass


def test_external_validation_and_return_factory_are_injected():
    seen=[]
    def validator(name,payload): seen.append(name)
    def factory(payload): return {"a_factory":payload}
    data=case()
    with integration_context(validator=validator,contract_fingerprint="test-double-not-A-contract",outcome_factory=factory):
        result=audit_classification(**data,policy=resources()["policy"],run={},llm=FakeClient())
    assert "a_factory" in result
    assert result["a_factory"]["status"]=="SUCCEEDED"
    assert "AuditOutcome" in seen and "AuditRecordResult" in seen
    # This test validates the bridge mechanism only; the validator is a test double, not A.


@pytest.mark.parametrize("name",["DocumentContext","EvidenceRecord","ClassificationRecord","AuditRecordResult","AuditOutcome"])
def test_external_schema_failure_cannot_publish_success(name):
    def validator(actual,payload):
        if actual==name: raise ValueError("Schema mismatch")
    data=case()
    with integration_context(validator=validator,contract_fingerprint="fixture-validator"):
        result=audit_classification(**data,policy=resources()["policy"],run={},llm=FakeClient())
    if name=="ClassificationRecord":
        assert result["record_results"][0]["review_status"]=="INSUFFICIENT"
    else:
        assert result["status"]=="FAILED"


def test_policy_constraints_and_prompt_fingerprint():
    baseline=resources()["policy"]
    for changes in [{"sample_rate":-1},{"visual_sample_rate":2},{"block_min_confidence":.5},
                    {"max_records_per_group":21},{"filter_sample_max":-1},{"max_model_calls":-1},
                    {"allow_auto_adopt":True},{"key_indicators":"wrong"},{"prompt_sha256":"wrong"},
                    {"review_policy_version":""}]:
        with pytest.raises(ValueError): policy_values({**baseline,**changes})


def test_mapping_supports_actual_model_shape_without_defining_A_types():
    Dummy=make_dataclass("FixtureModel",[("pid",str)])
    assert mapping(Dummy("x"))=={"pid":"x"}
    class PydanticLike:
        def model_dump(self,*,mode): return {"pid":"y"}
    assert mapping(PydanticLike())=={"pid":"y"}
    with pytest.raises(ValueError): mapping(1)
