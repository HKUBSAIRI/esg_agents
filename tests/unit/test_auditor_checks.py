from copy import deepcopy
from pathlib import Path

import pytest

from builders import case, clone_record, model_finding
from fake_client import FakeClient
from esg_semantic.auditor import SemanticAuditor, audit_classification
from esg_semantic.audit.common import policy_values, resources, sha, logical_key
from esg_semantic.audit.integration import integration_context
from esg_semantic.audit.prechecks import normalization_ok, precheck, report_checks


def run_case(data=None, *, client=None, updates=None, **bindings):
    data = data or case()
    policy = {**resources()["policy"], "sample_rate": 1, "visual_sample_rate": 0, **(updates or {})}
    with integration_context(fixture=True, **bindings):
        return SemanticAuditor(client if client is not None else FakeClient(), policy,
                               catalogue={"E2.1.1": {"variables": [data["records"][0]["variable"]]}} if data["records"] else {}).audit_report(**data, run={"audit_run_id":"ar_test", "created_at":"2026-10-29T00:00:00Z"})


def status(result, index=0):
    return result["record_results"][index]["review_status"]


def codes(result):
    return {f["code"] for f in result["findings"]}


def test_TC_A_01_correct_record():
    result = run_case()
    assert status(result) == "CONFIRMED"
    assert result["findings"] == []
    assert result["status"] == "PARTIAL"  # Explicitly unverified fixture contract.


def test_TC_A_02_value_missing():
    data = case(); data["evidence"][0].update(content="2025 total water withdrawal 12345 m3", numbers=[12345])
    result = run_case(data)
    assert "A-EVIDENCE-WEAK" in codes(result)
    assert status(result) == "REJECTED"


def test_TC_A_03_unit_mismatch():
    data = case(); data["evidence"][0]["unit_hint"] = "千立方米"
    assert "A-UNIT-MISMATCH" in codes(run_case(data))


def test_TC_A_04_normalization_invalid():
    data = case(); data["records"][0]["normalized_value"] = 1234000
    assert "A-NORMALIZATION-INVALID" in codes(run_case(data))


def test_TC_A_05_year_mismatch():
    data = case(); data["evidence"][0]["col_header"] = "2024"; data["evidence"][0]["year_hint"] = 2024
    assert "A-YEAR-MISMATCH" in codes(run_case(data))


def test_TC_A_06_scope_mix():
    result = run_case(client=FakeClient([model_finding("A-SCOPE-MIX")]))
    assert status(result) == "REJECTED"


def test_TC_A_07_cross_record_conflict():
    data = case(); clone_record(data, "conflict", value=2000, breakdown="Group")
    assert "A-CROSS-RECORD" in codes(run_case(data))


def test_bad_peer_normalization_or_unknown_scope_not_a_comparable_conflict():
    data=case(); other,_,_=clone_record(data,"peer",value=2000,breakdown="Group")
    other["normalized_value"]=999999
    result=run_case(data)
    assert "A-CROSS-RECORD" not in codes(result)
    assert result["record_results"][0]["review_status"]=="CONFIRMED"
    data=case(); data["records"][0]["breakdown"]=None; data["records"][0]["logical_group_key"]=logical_key(data["records"][0])
    clone_record(data,"peer",value=2000,breakdown=None)
    assert "A-CROSS-RECORD" not in codes(run_case(data))


def test_TC_A_08_duplicate():
    data = case(); clone_record(data, "duplicate", value=1234, breakdown="Group")
    assert "A-DUPLICATE-UNMERGED" in codes(run_case(data))


def test_TC_A_09_filter_overdrop():
    data = case(); data["filter_sample"] = [{"evidence_version_id":"ev_b_1", "status":"EXCLUDE", "noise_class":"NONE", "numbers":[1234]}]
    result = run_case(data)
    assert "A-FILTER-OVERDROP" in codes(result)
    assert next(f for f in result["findings"] if f["code"] == "A-FILTER-OVERDROP")["record_id"] is None


def test_TC_A_10_missing_key():
    data = case(); data["records"] = []
    data["coverage"]["by_indicator"]["E2.1.1"] = {"n_records":0, "unclassified_hints":[{"evidence_version_id":"ev_b_1"}]}
    assert "A-MISSING-KEY" in codes(run_case(data))


def test_TC_A_11_missing_signal_dropped():
    data = case(); data["coverage"] = None
    finding = model_finding("A-MISSING-KEY", record_id=None, indicator_id="E2.1.1")
    result = run_case(data, client=FakeClient([finding]))
    assert "A-MISSING-KEY" not in codes(result)
    assert result["stats"]["missing_key_without_signal"] == 1
    assert status(result) == "INSUFFICIENT"


def test_TC_A_12_invalid_code():
    result = run_case(client=FakeClient([model_finding("A-NOT-EXIST")]))
    assert result["stats"]["invalid_finding_code"] == 1
    assert status(result) == "INSUFFICIENT"


def test_TC_A_13_dangling_evidence():
    result = run_case(client=FakeClient([model_finding(evidence_version_ids=["missing"])]))
    assert result["stats"]["dangling_evidence"] == 1
    assert status(result) == "INSUFFICIENT"


def test_TC_A_14_low_confidence_block():
    result = run_case(client=FakeClient([model_finding(confidence=.5)]))
    assert status(result) == "SUSPECT"
    assert result["findings"][0]["severity"] == "WARN"
    assert result["stats"]["block_downgraded"] == 1


def test_TC_A_15_failure_not_pass():
    client = FakeClient(error=TimeoutError("secret must not appear"))
    result = run_case(client=client)
    assert status(result) == "INSUFFICIENT"
    assert len(client.calls) == 1
    assert "secret must not appear" not in str(result)


def test_TC_A_16_not_sampled():
    data = case(); data["records"][0]["indicator_id"] = "OTHER"; data["records"][0]["logical_group_key"] = logical_key(data["records"][0])
    result = run_case(data, updates={"sample_rate":0})
    assert status(result) == "NOT_REVIEWED"
    assert result["stats"]["model_calls"] == 0


def test_TC_A_17_key_coverage_and_budget():
    data = case()
    other, _, _ = clone_record(data, "other")
    other.update(indicator_id="A0"); other["logical_group_key"] = logical_key(other)
    client = FakeClient()
    result = run_case(data, client=client, updates={"max_model_calls":1})
    assert client.calls[0]["context"]["RECORDS"][0]["indicator_id"] == "E2.1.1"
    assert result["coverage"]["key_not_reviewed"] == 0
    assert result["truncated"] is True
    assert any(r["review_status"] == "INSUFFICIENT" for r in result["record_results"])


def test_TC_A_18_in_memory_input_immutable():
    data = case(); before = sha(data)
    run_case(data)
    assert sha(data) == before


def test_TC_A_19_replay_stable():
    client = FakeClient([model_finding()])
    one = run_case(client=client); two = run_case(client=client)
    assert one["findings"] == two["findings"]


def test_TC_A_21_v1_no_bbox_normal():
    data = case()
    result = run_case(data)
    assert "A-TRACE-MISSING" not in codes(result)
    assert "A-EVIDENCE-WEAK" not in codes(result)


def test_TC_A_23_explicit_context_mismatch():
    data = case(); data["records"][0]["source_ref"] = {"row_header":"consumption"}
    assert "A-EXTRACT-CONTEXT" in codes(run_case(data))


def test_TC_A_24_page_or_kind_mismatch():
    data = case(); data["records"][0]["source_kind"] = "text"
    assert "A-EXTRACT-PAGE" in codes(run_case(data))


def test_TC_A_25_v2_trace_missing():
    data = case(); data["evidence"][0]["source_format"] = "v2"
    result = run_case(data)
    assert "A-TRACE-MISSING" in codes(result)
    assert result["coverage"]["v2_records"] == 1


def test_TC_A_26_v1_extraction_mismatch_page_only():
    finding = model_finding("A-EXTRACT-CONTEXT", severity="WARN")
    result = run_case(client=FakeClient([finding]))
    assert "页级核对" in result["findings"][0]["reason"]
    assert result["findings"][0]["bbox"] is None


@pytest.mark.parametrize("value", [0, 0.1256789, 99999999999999999999])
def test_numbers_preserved(value):
    data = case(); r, e = data["records"][0], data["evidence"][0]
    r.update(raw_value=str(value), value=value, normalized_value=value)
    e.update(numbers=[value], content=f"2025集团总取水量 {value} m3")
    assert status(run_case(data)) == "CONFIRMED"


def test_not_disclosed_is_not_zero_or_bad_normalization():
    data = case(); record = data["records"][0]
    record.update(value=None, raw_value=None, normalized_value=None, value_nature="NOT_DISCLOSED", normalization=None)
    data["evidence"][0].update(content="本年度未披露集团总取水量", numbers=[])
    assert status(run_case(data)) == "CONFIRMED"


@pytest.mark.parametrize("mutate", [
    lambda d: d["records"].append(deepcopy(d["records"][0])),
    lambda d: d["evidence"].append(deepcopy(d["evidence"][0])),
    lambda d: d["decisions"].append(deepcopy(d["decisions"][0])),
    lambda d: d["records"][0].update(pid="OTHER"),
    lambda d: d["document"].pop("report_year"),
])
def test_fatal_input_identity_errors(mutate):
    data = case(); mutate(data)
    client = FakeClient()
    assert run_case(data, client=client)["status"] == "FAILED"
    assert client.calls == []


@pytest.mark.parametrize("field", ["raw_unit", "value_nature", "normalization"])
def test_partial_record_missing_field(field):
    data = case(); data["records"][0].pop(field)
    assert status(run_case(data)) == "INSUFFICIENT"


def test_alias_fallback_only_unique():
    data = case(); data["records"][0]["evidence_version_id"] = "old"
    assert status(run_case(data)) == "CONFIRMED"
    duplicate = deepcopy(data["evidence"][0]); duplicate["evidence_version_id"] = "other_version"
    data["evidence"].append(duplicate)
    assert status(run_case(data)) == "INSUFFICIENT"


def test_truncated_evidence_never_passes():
    data = case(); data["evidence"][0]["content"] *= 40
    client = FakeClient()
    assert status(run_case(data, client=client)) == "INSUFFICIENT"
    assert len(client.calls[0]["context"]["EVIDENCE"][0]["content"]) <= 1200


def test_no_model_does_not_run_or_confirm():
    data = case()
    with integration_context(fixture=True):
        result = audit_classification(**data, policy=resources()["policy"], run={"run_id":"none"}, llm=None)
    assert status(result) == "INSUFFICIENT"


def test_no_a_contract_no_implicit_fixture_or_model_call():
    data = case(); client = FakeClient()
    result = audit_classification(**data, policy=resources()["policy"], run={}, llm=client)
    assert result["status"] == "FAILED"
    assert result["problems"][0]["error_code"] == "ERR_CONTRACT_NOT_READY"
    assert client.calls == []


def test_more_than_twenty_bounded_and_all_results_present():
    data = case()
    for i in range(24): clone_record(data, "extra" + str(i), value=1234, breakdown="Scope" + str(i))
    client = FakeClient(); result = run_case(data, client=client, updates={"max_model_calls":30})
    assert len(result["record_results"]) == 25
    assert all(len(c["context"]["RECORDS"]) <= 20 for c in client.calls)


def test_client_attempts_budget_is_accounted():
    client = FakeClient(response={"text":'{"findings":[]}',"attempts":2,"tokens":30})
    assert run_case(client=client, updates={"max_model_calls":2})["stats"]["model_calls"] == 2
    assert status(run_case(client=client, updates={"max_model_calls":1})) == "INSUFFICIENT"


def test_model_mutation_of_original_input_detected():
    data = case()
    def client(prompt, *, max_attempts):
        data["records"][0]["value"] = 9
        return {"findings":[]}
    assert run_case(data, client=client)["status"] == "FAILED"


@pytest.mark.parametrize("normalization,expected", [
    (None,False), ({"status":"UNKNOWN_UNIT"},True), ({"status":"NO_UNIT"},True),
    ({"status":"NOT_APPLICABLE"},True), ({"status":"BAD"},False),
    ({"status":"OK","factor":0,"from":"m3","to":"m3"},False),
])
def test_normalization_edge_cases(normalization, expected):
    record = case()["records"][0]; record.update(normalized_value=None, normalization=normalization)
    assert normalization_ok(record) == expected
