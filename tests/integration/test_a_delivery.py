"""Tests for the provisional real-delivery adapter; all model calls are mocked."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError

import pytest

from esg_semantic.audit.common import canonical, logical_key
from esg_semantic.audit.delivery import adapt, prepare, run_delivery, DeliveryValidator, catalogue, iid, VERSION
from esg_semantic.audit.delivery_client import DashScopeClient, DeliveryModelError
from esg_semantic.audit.integration import integration_context
from esg_semantic.audit.prechecks import normalization_ok, precheck, record_group_checks
from esg_semantic.auditor import audit_classification


def data():
    pid = "TEST-A-DELIVERY"
    evidence, records, decisions = [], [], []
    for i, (field, raw, unit, normalized, target, status) in enumerate([
        ("water_withdrawal_m3", "1234", "m3", 1234, "m3", "ASSIGNED"),
        ("water_consumption_m3", "200", "Tonnes", 200, "m3", "ASSIGNED"),
        (None, "50", "kWh", None, None, "UNMAPPED"),
    ]):
        content = f"2025 Group data {raw} {unit}"
        evidence.append(dict(evidence_version_id=f"ev_{i}", evidence_id=f"e_{i}", pid=pid, page_no=i+1, kind="table_cell",
                             content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(), numbers=[raw], bbox=None,
                             tier="high", source_format="v1_numeric_blocks", unit_hint="%", year_hint=2026,
                             source_ref={"page_level": True}, col_header=None, row_header=None))
        records.append(dict(record_id=f"r_{i}", pid=pid, profile_id="water_v1", profile_field=field, profile_version="WATER-1.0.0",
                            evidence_version_id=f"ev_{i}", evidence_id=f"e_{i}", source_page=i+1, source_kind="table_cell",
                            classification_status=status, value_nature="REPORTED", raw_value=raw, raw_unit=unit, value=float(raw), unit=unit,
                            normalized_value=normalized, normalized_unit=target, normalization={"status": "OK" if normalized is not None else "NOT_APPLICABLE", "from_unit": unit, "to_unit": target, "factor": 1 if normalized is not None else None, "rule_id": "water-tonne-approx-v1" if i==1 else "volume-v1", "rule_version":"UNITS-3.0"},
                            logical_group_key=f"{pid}|water_v1|{field or ''}|2025||GROUP", year=2025, year_status="EXPLICIT", report_year=2025,
                            breakdown=None, scope_type="GROUP", scope_note="Group", period_start="2025-01-01", period_end="2025-12-31",
                            value_qualifier="EXACT", filter_status="ACCEPT", provenance_source="evidence_id", classification_run_id="cr_test"))
        decisions.append(dict(evidence_version_id=f"ev_{i}", status="ACCEPT", policy_version="FP-3.4.0", reason_code="R-IN-TABLE"))
    return {"records.jsonl":records, "evidence.jsonl":evidence, "semantic_filter.jsonl":decisions,
            "document_context.json":dict(pid=pid, company_name="UNKNOWN", report_year=2026, report_year_source="FILING_YEAR_FALLBACK", page_count=3, pdf_path=None),
            "identity.json":dict(company_name="Actual delivery company"),
            "filter_report.json":dict(status="SUCCEEDED", filter_run_id="fr_test"),
            "classification_report.json":dict(status="PARTIAL", classification_run_id="cr_test", input={"filter_run_id":"fr_test"}),
            "coverage.json":dict(key_fields=["water_withdrawal_m3", "water_consumption_m3", "water_discharge_m3"],
                                 by_field={"water_discharge_m3":{"n_records":0}}, unclassified_hints=[{"profile_field":"water_discharge_m3", "evidence_version_id":"ev_2"}])}


def save(directory, values):
    directory.mkdir(parents=True)
    for name, value in values.items():
        text = "\n".join(canonical(row) for row in value) if name.endswith(".jsonl") else canonical(value)
        (directory/name).write_text(text, encoding="utf-8")


def binding(**overrides):
    validator = DeliveryValidator()
    return integration_context(validator=validator, contract_fingerprint=validator.fingerprint,
                               validation_scope="local_delivery", record_format="profile_delivery_v1", catalogue=catalogue(), **overrides)


def test_projection_preserves_original_semantics_and_identity():
    original = data(); before = deepcopy(original)
    converted, report = adapt(original)
    assert original == before
    for a,b in zip(original["records.jsonl"], converted["records.jsonl"]):
        for key in ("record_id", "logical_group_key", "value", "unit", "raw_value", "normalized_value", "scope_type"):
            assert a[key] == b[key]
        assert b["indicator_id"] == iid(a["profile_field"])
    assert converted["document_context.json"]["company_name"] == "Actual delivery company"
    assert converted["document_context.json"]["report_year"] == 2026
    assert report["source_document_context"]["company_name"] == "UNKNOWN"
    assert not report["a_formal_contract_verified"]
    assert converted["coverage.json"]["by_indicator"][iid("water_discharge_m3")]["unclassified_hints"]


def test_aliases_preserve_logical_key_and_page_hints_do_not_create_false_blocks():
    converted,_=adapt(data())
    with binding():
        for row,ev,decision in zip(converted["records.jsonl"],converted["evidence.jsonl"],converted["semantic_filter.jsonl"]):
            assert logical_key(row) == row["logical_group_key"]
            checks={h["check"]:h for h in precheck(row,ev,decision,converted["document_context.json"])}
            assert checks["D1"]["value"] is True
            assert checks["D4"]["value"] is None
            assert checks["D5"]["value"] is None
        assert normalization_ok(converted["records.jsonl"][0]) is True
        assert normalization_ok(converted["records.jsonl"][1]) is None


def test_wrong_factor_is_still_rejected():
    converted,_=adapt(data())
    converted["records.jsonl"][0]["normalization"]["factor"]=1000
    with binding():
        assert normalization_ok(converted["records.jsonl"][0]) is False


def test_unmapped_records_not_grouped_into_false_duplicates():
    converted,_=adapt(data()); row=converted["records.jsonl"][2]
    other={**row,"record_id":"different"}
    with binding():
        assert dict(record_group_checks([row,other],{})) == {}


def test_grouping_separates_scope_period_and_qualifier():
    converted,_=adapt(data()); row=converted["records.jsonl"][0]
    for field,value in [("scope_type","SEGMENT"),("period_end","2024-12-31"),("value_qualifier","TARGET")]:
        other={**row,"record_id":"different",field:value}
        with binding():
            hints=record_group_checks([row,other],{"r_0":converted["evidence.jsonl"][0],"different":converted["evidence.jsonl"][0]})
        assert not any(h["value"] is True for values in hints.values() for h in values)


@pytest.mark.parametrize("bad",["hash","duplicate","unknown_field","missing_decision","cross_report"])
def test_bad_delivery_rejected(bad):
    source=data()
    if bad=="hash":source["evidence.jsonl"][0]["content"]="modified"
    if bad=="duplicate":source["records.jsonl"].append(deepcopy(source["records.jsonl"][0]))
    if bad=="unknown_field":source["records.jsonl"][0]["profile_field"]="future_unknown_field"
    if bad=="missing_decision":source["semantic_filter.jsonl"].pop()
    if bad=="cross_report":source["records.jsonl"][0]["pid"]="other"
    with pytest.raises(ValueError):adapt(source)


def test_source_tree_cannot_be_output(tmp_path):
    source=tmp_path/"source";save(source,data())
    with pytest.raises(ValueError):prepare(source,source/"output")


def test_mixed_runs_rejected(tmp_path):
    values=data();values["classification_report.json"]["input"]["filter_run_id"]="different"
    source=tmp_path/"source";save(source,values)
    with pytest.raises(ValueError,match="Mixed"):prepare(source,tmp_path/"out")


def test_rules_run_publishes_four_files_without_calling_model_and_without_source_changes(tmp_path):
    source=tmp_path/"source";save(source,data())
    before={p.name:p.read_bytes() for p in source.iterdir()}
    def no_calls(*args,**kwargs):pytest.fail("rules_only cannot call a model")
    result=run_delivery(source,tmp_path/"output",llm=no_calls)
    assert result["source_unchanged"] and result["stats"]["records_total"]==3
    assert result["stats"]["model_calls"]==0 and result["stats"]["records_reviewed"]==0
    report=json.loads((Path(result["output_directory"])/"audit_report.json").read_text(encoding="utf-8"))
    assert report["validation_scope"]=="local_delivery" and report["execution_mode"]=="rules_only"
    assert not any(r["review_status"]=="CONFIRMED" for r in report["record_results"])
    assert before=={p.name:p.read_bytes() for p in source.iterdir()}
    assert {p.name for p in Path(result["output_directory"]).iterdir()}=={"audit_report.json","audit_results.jsonl","audit_findings.jsonl","review_items.jsonl"}


def test_unknown_conversion_can_be_semantically_reviewed_but_not_confirmed(tmp_path):
    source=tmp_path/"source";save(source,data());calls=[]
    def client(prompt,*,max_attempts):
        calls.append(prompt);return {"text":'{"findings":[]}',"attempts":1,"tokens":1}
    result=run_delivery(source,tmp_path/"output",mode="model",llm=client,max_model_calls=6)
    report=json.loads((Path(result["output_directory"])/"audit_report.json").read_text(encoding="utf-8"))
    statuses={r["record_id"]:r["review_status"] for r in report["record_results"]}
    assert calls and report["stats"]["records_reviewed"]==2
    assert statuses["r_1"]=="INSUFFICIENT" and statuses["r_2"]=="INSUFFICIENT"
    assert any("DELIVERY_REPORT_YEAR_CONFLICT"==p["error_code"] for p in report["problems"])


def test_local_projection_cannot_claim_formal_schema():
    with pytest.raises(ValueError):
        with integration_context(validator=lambda *args:None,contract_fingerprint="test",record_format="profile_delivery_v1"):
            pass


def test_delivery_client_one_request_and_no_secret_leak(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY","secret-test-only")
    payloads=[]
    class Opener:
        def open(self,request,timeout):
            payloads.append(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"findings\\":[]}"}}],"usage":{"total_tokens":12}}')
    monkeypatch.setattr("esg_semantic.audit.delivery_client.build_opener",lambda *args:Opener())
    client=DashScopeClient()
    response=client("prompt",max_attempts=5)
    assert len(payloads)==1 and response["attempts"]==1 and response["tokens"]==12
    assert not payloads[0]["enable_thinking"]
    assert "secret-test-only" not in canonical(payloads)


@pytest.mark.parametrize("error",[HTTPError("https://example",401,"secret-test-only",None,None),URLError("secret-test-only")])
def test_client_errors_are_sanitized_and_recorded(monkeypatch,error):
    monkeypatch.setenv("DASHSCOPE_API_KEY","secret-test-only")
    class Opener:
        def open(self,*args,**kwargs):raise error
    monkeypatch.setattr("esg_semantic.audit.delivery_client.build_opener",lambda *args:Opener())
    client=DashScopeClient()
    with pytest.raises(DeliveryModelError) as caught:client("prompt",max_attempts=1)
    assert "secret-test-only" not in str(caught.value)
    assert len(client.failure_codes)==1


@pytest.mark.parametrize("contents",["api_key = sk-test-only-1234567890", "api_key: opaque-test-only-12345678901234567890"])
def test_explicit_key_file_overrides_environment_without_logging(monkeypatch,tmp_path,contents):
    monkeypatch.setenv("DASHSCOPE_API_KEY","wrong-test-environment-key")
    path=tmp_path/"key.txt";path.write_text(contents,encoding="utf-8")
    client=DashScopeClient(api_key_file=path)
    assert client._key not in ("", "wrong-test-environment-key")
    assert client.calls==0 and client.failure_codes==[]


def test_missing_and_ambiguous_key_configuration_rejected(monkeypatch,tmp_path):
    monkeypatch.delenv("DASHSCOPE_API_KEY",raising=False)
    with pytest.raises(DeliveryModelError):DashScopeClient()
    path=tmp_path/"key.txt";path.write_text("sk-test-only-1234567890 sk-second-test-1234567890",encoding="utf-8")
    with pytest.raises(DeliveryModelError):DashScopeClient(api_key_file=path)


def test_client_budget_and_unauthorized_requests_do_not_retry(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY","fake-key-for-unit-test")
    class Opener:
        def open(self,*args,**kwargs):raise HTTPError("https://example",401,"secret",None,None)
    monkeypatch.setattr("esg_semantic.audit.delivery_client.build_opener",lambda *args:Opener())
    client=DashScopeClient()
    with pytest.raises(DeliveryModelError):client("prompt",max_attempts=0)
    assert client.calls==0
    for _ in range(2):
        with pytest.raises(DeliveryModelError):client("prompt",max_attempts=3)
    assert client.calls==1 and client.failure_codes==["MODEL_HTTP_401"]


@pytest.mark.parametrize("body",[b"x"*2_000_001,b'{}',b'not JSON',b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}'])
def test_malformed_and_incomplete_api_responses_not_accepted(monkeypatch,body):
    monkeypatch.setenv("DASHSCOPE_API_KEY","fake-key-for-unit-test")
    class Opener:
        def open(self,*args,**kwargs):return io.BytesIO(body)
    monkeypatch.setattr("esg_semantic.audit.delivery_client.build_opener",lambda *args:Opener())
    client=DashScopeClient()
    with pytest.raises(DeliveryModelError):client("prompt",max_attempts=1)
    assert client.failure_codes


def test_api_redirects_are_disabled():
    from esg_semantic.audit.delivery_client import NoRedirect
    assert NoRedirect().redirect_request(None,None,302,"redirect",{},"https://example") is None








def test_real_delivery_shapes_readonly_when_available():
    root=Path(__file__).resolve().parents[2]/"esg_agents/esg_agents"
    if not root.is_dir():pytest.skip("User's real delivery not distributed with source code")
    count=0
    for directory in root.iterdir():
        if not directory.is_dir():continue
        values={}
        for name in ("records.jsonl","evidence.jsonl","semantic_filter.jsonl","document_context.json","identity.json","coverage.json"):
            text=(directory/name).read_text(encoding="utf-8-sig")
            values[name]=[json.loads(l) for l in text.splitlines() if l.strip()] if name.endswith(".jsonl") else json.loads(text)
        converted,_=adapt(values)
        with binding():
            for row,ev in zip(converted["records.jsonl"], [next(e for e in converted["evidence.jsonl"] if e["evidence_version_id"]==r["evidence_version_id"]) for r in converted["records.jsonl"]]):
                decision=next(d for d in converted["semantic_filter.jsonl"] if d["evidence_version_id"]==row["evidence_version_id"])
                assert precheck(row,ev,decision,converted["document_context.json"])[0]["value"] is True
                count+=1
    assert count==621
