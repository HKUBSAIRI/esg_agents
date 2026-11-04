"""A semantics and extraction/A/B integration. No external requests in these tests."""
from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from esg_pipeline.profiles import load_profile
from esg_pipeline.validation import normalize
from esg_pipeline.llm import JsonClient, usage_summary
from esg_semantic.filter import filter_evidence
from esg_semantic.classifier import classify, rule_assignment, model_assignments
from esg_semantic.audit.delivery import adapt, DeliveryValidator, catalogue
from esg_semantic.audit.integration import integration_context
from esg_semantic.auditor import audit_classification
from esg_extraction.extractor import extract_pdf
from esg_pipeline.runner import run_pipeline
from esg_pipeline.inputs import load_extraction

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ESG报告_按行业分类/Water Utilities & Services/00807_2026032702314.pdf"


def candidate(label="Total water withdrawal", **updates):
    return {"candidate_id": "c1", "pid": "p1", "evidence_version_id": "ev1", "source_kind": "vision_table",
            "metric_label": label, "value_text": "1,234.50", "value_decimal": "1234.50", "page_no": 1,
            "unit": "m3", "year": 2025, "scope": "Group", "value_kind": "LABELLED", **updates}


def evidence():
    import hashlib
    content = "2025 Group Total water withdrawal 1,234.50 m3"
    return {"evidence_version_id": "ev1", "evidence_id": "e1", "pid": "p1", "page_no": 1, "kind": "table_cell",
            "content": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest(), "numbers": ["1234.50"],
            "bbox": [0, 0, 100, 100], "source_format": "v2", "tier": "unverified", "unit_hint": None,
            "source_ref": {"table_id": "t1", "independent_binding_verified": False}}


def document():
    return {"pid": "p1", "report_year": 2025, "page_count": 1, "pdf_path": None}


@pytest.mark.parametrize("label,field",[("Total Water Consumption", "water_consumption_m3"),
    ("Water withdrawal", "water_withdrawal_m3"), ("Wastewater discharge", "water_discharge_m3"),
    ("Recycled Water Used", "water_recycled_reused_m3")])
def test_distinct_water_fields(label, field):
    assert rule_assignment(candidate(label), load_profile())["field"] == field


@pytest.mark.parametrize("label",["Water treatment capacity", "Recycled water volume increase target by 2030",
                                 "Water consumption intensity", "COD removal", "Ammonia Gas"])
def test_capacity_target_intensity_and_ammonia_gas_are_not_discharge(label):
    assert rule_assignment(candidate(label, unit="tonnes"), load_profile())["field"] is None


def test_decimal_units_no_water_mass_conversion_or_rounding():
    value, unit, rule = normalize("9007199254740993.12345", "kg", "t")
    assert value == "9007199254740.99312345"
    assert rule["factor"] == "0.001"
    assert normalize("500", "Tonnes", "m3")[0] is None
    assert normalize("2", "ML", "m3")[0] == "2000"
    assert normalize("2", "mL", "m3")[0] == "0.000002"


def test_filter_never_calls_model_for_protected_table_and_figure():
    class Forbidden:
        def request(self, *args):
            raise AssertionError("Protected evidence should use hard keep")
    ev = evidence()
    assert filter_evidence([ev, {**ev, "kind": "figure"}], llm=Forbidden())[0]["status"] == "ACCEPT"


def test_filter_failure_retains_unprotected_evidence():
    class Failure:
        def request(self, *args):
            raise RuntimeError()
    ev = {**evidence(), "kind": "text", "numbers": []}
    assert filter_evidence([ev], llm=Failure())[0]["status"] == "REVIEW"


def test_classification_preserves_every_candidate_and_exact_value_unknown_year_scope():
    rows = [candidate(year=None, scope=None), candidate("Header", candidate_id="c2", value_role="HEADER_OR_LABEL_CANDIDATE")]
    before = deepcopy(rows)
    rec, outcomes, _ = classify(rows, [evidence()], filter_evidence([evidence()]), document(), load_profile(), "cr1", "fr1")
    assert rows == before and len(outcomes) == 2 and len(rec) == 1
    assert rec[0]["value"] == "1234.50" and rec[0]["raw_value"] == "1,234.50"
    assert rec[0]["year"] is None and rec[0]["scope_type"] == "UNKNOWN"
    assert rec[0]["classification_status"] == "INSUFFICIENT_CONTEXT" and not rec[0]["record_usable"]


@pytest.mark.parametrize("mutation",["id", "field", "value", "quote", "duplicate"])
def test_model_cannot_inject_ids_fields_values_or_unfounded_quotes(mutation):
    c = candidate("Water use")
    a = {"candidate_id": "c1", "field": "water_consumption_m3", "status": "ASSIGNED", "reason": "明确披露", "quote": "Water use"}
    if mutation == "id": a["candidate_id"] = "other"
    if mutation == "field": a["field"] = "fake_field"
    if mutation == "value": a["value"] = 999
    if mutation == "quote": a["quote"] = "invented quote"
    class Fake:
        def request(self, *args):
            return {"data": {"assignments": [a, a] if mutation == "duplicate" else [a]}, "call_id": "mock"}
    assignments = {"c1": rule_assignment(c, load_profile())}
    before = deepcopy(assignments)
    receipts = model_assignments([c], assignments, load_profile(), {"ev1": evidence()}, Fake())
    assert assignments == before and receipts[0]["status"] == "FAILED_RETAINED"


def test_model_valid_assignment_does_not_rewrite_observation():
    c = candidate("Water use")
    before = deepcopy(c)
    class Fake:
        def request(self, *args):
            return {"data": {"assignments": [{"candidate_id": "c1", "field": "water_consumption_m3", "status": "ASSIGNED", "reason": "需复核", "quote": "Water use"}]}, "call_id": "mock"}
    records, _, _ = classify([c], [evidence()], filter_evidence([evidence()]), document(), load_profile(), "cr1", "fr1", llm=Fake())
    assert c == before and records[0]["field_source"] == "LLM" and records[0]["value"] == "1234.50"


def test_bad_id_does_not_discard_an_independent_valid_sibling():
    c1, c2 = candidate("Water use"), candidate("Water use", candidate_id="c2")
    class Fake:
        def request(self, *args):
            base = {"field": "water_consumption_m3", "status": "ASSIGNED", "reason": "需复核", "quote": "Water use"}
            return {"data": {"assignments": [{**base, "candidate_id":"c1"}, {**base, "candidate_id":"typo"}]}, "call_id":"mock"}
    records, _, receipts = classify([c1,c2], [evidence()], filter_evidence([evidence()]), document(), load_profile(), "cr1", "fr1", llm=Fake())
    assert records[0]["field_source"] == "LLM" and records[1]["field_source"] == "RULE"
    assert receipts[0]["status"] == "PARTIAL" and receipts[0]["unknown_id_count"] == 1


def test_b_accepts_v2_and_never_self_confirms_model_transcription():
    ev = evidence()
    records, _, _ = classify([candidate()], [ev], filter_evidence([ev]), document(), load_profile(), "cr1", "fr1")
    source = {"document_context.json": document(), "evidence.jsonl": [ev], "records.jsonl": records,
              "semantic_filter.jsonl": filter_evidence([ev])}
    converted, _ = adapt(source)
    validator = DeliveryValidator()
    calls = []
    def model(prompt, **kwargs):
        calls.append(prompt)
        return {"findings": []}
    with integration_context(validator=validator, contract_fingerprint=validator.fingerprint,
                             validation_scope="local_delivery", record_format="profile_delivery_v1", catalogue=catalogue()):
        outcome = audit_classification(converted["records.jsonl"], converted["evidence.jsonl"], converted["semantic_filter.jsonl"],
                                       [], converted["coverage.json"], converted["document_context.json"], llm=model,
                                       policy={**json.loads((ROOT / "skills/audit/policy.json").read_text(encoding="utf-8")), "sample_rate":1.0}, run={"audit_run_id":"test-local-v2"})
    assert outcome["status"] != "FAILED"
    assert calls and outcome["record_results"][0]["review_status"] == "INSUFFICIENT"
    assert any(p["error_code"] == "INDEPENDENT_SOURCE_REVIEW_REQUIRED" for p in outcome["problems"])
    assert outcome["precheck_counts"].get("D1") is not None


def test_gateway_counts_invalid_json_usage_and_does_not_retry(tmp_path, monkeypatch):
    key = tmp_path / "key.txt"; key.write_text("test_" + "a" * 30)
    client = JsonClient(tmp_path / "run", key_file=key, max_calls=1)
    envelope = {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, "choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]}
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    class Opener:
        def open(self, *args, **kwargs): return Response(json.dumps(envelope).encode())
    monkeypatch.setattr("esg_pipeline.llm.build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError): client.request("classify", {}, "JSON")
    with pytest.raises(RuntimeError): client.request("classify", {}, "JSON")
    assert client.calls == 1 and usage_summary(tmp_path / "run")["known_total_tokens"] == 12


def test_gateway_shared_cache_has_no_new_request_or_usage(tmp_path, monkeypatch):
    key = tmp_path / "key.txt"; key.write_text("test_" + "b" * 30)
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
    class Opener:
        def open(self, *args, **kwargs):
            return Response(json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}, "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}).encode())
    monkeypatch.setattr("esg_pipeline.llm.build_opener", lambda *args: Opener())
    first = JsonClient(tmp_path / "run1", key_file=key, max_calls=1)
    first.request("classify", {"id": 1}, "JSON")
    second = JsonClient(tmp_path / "run2", key_file=key, max_calls=1)
    assert second.request("classify", {"id": 1}, "JSON")["cache_hit"]
    assert second.calls == 0 and usage_summary(tmp_path / "run2")["api_attempts"] == 0
    with pytest.raises(RuntimeError):
        blocked = JsonClient(tmp_path / "run3", key_file=key, max_calls=2, stage_budgets={"classify":0})
        blocked.request("classify", {"id":2}, "JSON")
    assert usage_summary(tmp_path / "run3")["api_attempts"] == 0


def test_real_pdf_extraction_a_b_and_workbench_without_api(tmp_path):
    if not SOURCE.is_file(): pytest.skip("Research PDF is not shipped in source")
    class Vision:
        model="TEST-NO-API"
        max_calls=1
        calls=0
        def __call__(self, image):
            self.calls += 1
            return {"data": {"page_status": "EXTRACTED", "warnings": [], "regions": [{"kind": "table", "bbox": [0,0,1000,1000], "context": "test only",
                    "observations": [{"metric_label": "Water withdrawal", "value_text": "100", "unit": "m3", "year": 2025, "scope": "Group", "value_kind": "LABELLED"}]}]}, "usage_tokens": 0}
    extraction = extract_pdf(SOURCE, tmp_path / "extract", pages=[1], client=Vision())
    result = run_pipeline(extraction["output_directory"], tmp_path / "pipeline", database=tmp_path / "db")
    assert result["source_unchanged"] and result["usage"]["api_attempts"] == 0
    assert result["record_count"] == 1 and result["workbench_run_id"]
    a = Path(result["output_directory"]) / "a"
    assert len((a / "candidate_outcome.jsonl").read_text(encoding="utf-8").splitlines()) == result["candidate_count"]
    from review_workbench.service import Application
    from review_workbench.store import Store
    app = Application(Store(tmp_path / "db"))
    try:
        detail = app.detail(result["workbench_run_id"])
        assert detail["has_pdf"] and detail["bundle"]["records"][0]["value"] == "100"
    finally:
        app.close()
    original = a / "records.jsonl"
    text = original.read_text(encoding="utf-8")
    assert '"value":"100"' in text



