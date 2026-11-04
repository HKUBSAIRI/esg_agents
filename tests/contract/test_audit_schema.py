from copy import deepcopy
import pytest
from jsonschema import Draft202012Validator, ValidationError
from builders import case, model_finding
from fake_client import FakeClient
from draft_projection import SCHEMAS, validate_projection
from esg_semantic.audit.integration import integration_context
from esg_semantic.audit.common import resources
from esg_semantic.auditor import audit_classification
from esg_semantic.audit.review_items import build_review_items


def test_TC_A_20_B_document_projection_not_A_contract_acceptance():
    data=case()
    with integration_context(fixture=True,validator=validate_projection):
        result=audit_classification(**data,policy=resources()["policy"],run={},llm=FakeClient([model_finding()]))
    assert result["status"]=="PARTIAL"
    assert any(p["error_code"]=="CONTRACT_UNVERIFIED_FIXTURE_ONLY" for p in result["problems"])
    validate_projection("AuditOutcome",result)
    items=build_review_items(result["findings"],result["record_results"],data["records"],{"ev_b_1":data["evidence"][0]})
    for finding in result["findings"]: validate_projection("AuditFinding",finding)
    for row in result["record_results"]: validate_projection("AuditRecordResult",row)
    for item in items: validate_projection("ReviewItem",item)


@pytest.mark.parametrize("kind",list(SCHEMAS))
def test_missing_fields_rejected_by_draft_projection(kind):
    with pytest.raises(ValidationError): validate_projection(kind,{})


def test_llm_schema_and_resource_codebook_are_consistent():
    data=resources()
    Draft202012Validator.check_schema(data["schema"])
    codes=data["schema"]["properties"]["findings"]["items"]["properties"]["code"]["enum"]
    assert set(codes)==set(data["codebook"]) and len(codes)==19
    assert set(item["domain"] for item in data["codebook"].values())=={"EXTRACTION","CLASSIFICATION","NORMALIZATION","FILTER","COVERAGE"}
