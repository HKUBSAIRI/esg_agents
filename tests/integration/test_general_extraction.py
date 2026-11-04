"""Metric-agnostic extraction tests. Existing PDFs read-only; no PDF authoring/API."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError

import fitz
import pytest

from esg_extraction.numbers import tokens,textual_state
from esg_extraction.vision import validate_response,AliVisionClient,NoRedirect
from esg_extraction.extractor import extract_pdf,file_hash,exclusive,mark_duplicates

ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/"ESG报告_按行业分类/Water Utilities & Services/00807_2026032702314.pdf"


@pytest.mark.parametrize("source,expected",[("RMB2,426.9 million","2426.9"),("HKD3,000","3000"),("79,651, 697.9 tonnes","79651697.9"),("(1,234.50)","-1234.50"),("−12.5%","-12.5"),("1.25e3","1.25E+3"),("0","0")])
def test_number_literals_preserve_digits(source,expected):
    found=tokens(source)
    assert len(found)==1 and found[0]["value_decimal"]==expected


def test_identifiers_not_numeric_values_and_ambiguous_locale_not_guessed():
    assert tokens("CO2 m3 ISO9001")==[]
    assert tokens("1,23")[0]["value_decimal"] is None
    assert tokens("3.6 million")[0]["magnitude_factor_hint"]=="1000000"
    assert textual_state("nil")=="TEXTUAL_ZERO_CANDIDATE"
    assert textual_state("—")=="MISSING_OR_NOT_APPLICABLE"
    assert tokens(".5%")[0]["value_decimal"]=="0.5"
    assert tokens("12–15")[0]["warning"]=="RANGE_EXPRESSION_REQUIRES_REVIEW"


def vision_data():
    return {"page_status":"EXTRACTED","regions":[{"kind":"chart","bbox":[0,0,1000,1000],"title":"Staff","context":"2025, Group",
             "observations":[{"metric_label":"Employees","value_text":"1234","unit":"people","year":2025,"scope":"Group","series":None,"category":None,"value_kind":"LABELLED","evidence_quote":None}]}],"warnings":[]}


@pytest.mark.parametrize("bad",["bbox","kind","value","year","status","root"])
def test_visual_response_rejects_bad_shapes(bad):
    data=vision_data()
    if bad=="bbox":data["regions"][0]["bbox"]=[900,0,100,1000]
    if bad=="kind":data["regions"][0]["kind"]="unknown"
    if bad=="value":data["regions"][0]["observations"][0]["value_text"]="not numeric"
    if bad=="year":data["regions"][0]["observations"][0]["year"]=True
    if bad=="status":data["page_status"]="NO_QUANTITATIVE_FOUND"
    if bad=="root":data["injected"]="value"
    with pytest.raises(ValueError):validate_response(data)


class FakeVision:
    model="TEST-ONLY-NO-API"
    def __init__(self,max_calls=1):self.calls=0;self.max_calls=max_calls
    def __call__(self,image):
        assert Path(image).is_file()
        self.calls+=1
        return {"data":vision_data(),"usage_tokens":1,"request_fingerprint":"test-only"}


@pytest.fixture(scope="module")
def source():
    if not SOURCE.is_file():pytest.skip("Local research PDF not shipped with program source")
    return SOURCE


def test_real_pdf_extracts_non_water_values_and_preserves_source(source,tmp_path):
    before=file_hash(source)
    report=extract_pdf(source,tmp_path,pages=[31])
    directory=Path(report["output_directory"])
    rows=[json.loads(line) for line in (directory/"quantitative_candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    values={r["value_decimal"] for r in rows}
    assert "1525.23" in values and "880538220.09" in values
    assert report["source_unchanged"] and file_hash(source)==before
    assert report["vision_executed"] is False and report["unselected_page_count"]==49
    assert all(row["review_status"]=="NEEDS_REVIEW" for row in rows)
    assert all("indicator_id" not in row for row in rows)
    assert report["status"]=="PARTIAL" and not report["coverage_verified"]
    evidence=[json.loads(line) for line in (directory/"evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    ids={e["evidence_version_id"] for e in evidence}
    assert all(r["evidence_version_id"] in ids for r in rows)
    assert any("2025" in e["content"] and "2024" in e["content"] for e in evidence)
    first=(directory/"quantitative_candidates.jsonl").read_bytes()
    repeated=extract_pdf(source,tmp_path,pages=[31])
    assert repeated["resumed_pages"]==1 and first==(directory/"quantitative_candidates.jsonl").read_bytes()
    checkpoint=directory/"pages/00031.json"
    corrupted=json.loads(checkpoint.read_text(encoding="utf-8"));corrupted["data"]["candidates"]=[]
    checkpoint.write_text(json.dumps(corrupted),encoding="utf-8")
    regenerated=extract_pdf(source,tmp_path,pages=[31])
    assert regenerated["resumed_pages"]==0
    assert first==(directory/"quantitative_candidates.jsonl").read_bytes()


def test_fake_vision_budget_pending_and_resume_without_extra_calls(source,tmp_path):
    client=FakeVision()
    first=extract_pdf(source,tmp_path,pages=[1,2],client=client)
    assert client.calls==1
    directory=Path(first["output_directory"])
    coverage=[json.loads(line) for line in (directory/"page_coverage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert coverage[0]["visual_status"]=="MODEL_EXTRACTED_NEEDS_REVIEW"
    assert coverage[1]["visual_status"]=="PENDING_BUDGET"
    next_client=FakeVision()
    second=extract_pdf(source,tmp_path,pages=[1,2],client=next_client)
    assert next_client.calls==1 and second["resumed_pages"]==1
    final_client=FakeVision()
    third=extract_pdf(source,tmp_path,pages=[1,2],client=final_client)
    assert third["resumed_pages"]==2 and final_client.calls==0


def test_visual_failures_are_visible_and_retried_only_explicitly(source,tmp_path):
    class Failing(FakeVision):
        def __call__(self,image):self.calls+=1;raise RuntimeError("private-key-not-for-logs")
    client=Failing()
    report=extract_pdf(source,tmp_path,pages=[1],client=client)
    directory=Path(report["output_directory"])
    assert "private-key-not-for-logs" not in (directory/"page_coverage.jsonl").read_text(encoding="utf-8")
    retry=Failing();assert extract_pdf(source,tmp_path,pages=[1],client=retry)["resumed_pages"]==1 and retry.calls==0
    retry=Failing();extract_pdf(source,tmp_path,pages=[1],client=retry,retry_failed=True)
    assert retry.calls==1


def test_lock_and_selected_pages_guard(source,tmp_path):
    lockroot=tmp_path/"lock";lockroot.mkdir()
    with exclusive(lockroot):
        with pytest.raises(ValueError):
            with exclusive(lockroot):pass
    assert not (lockroot/".extract.lock").exists()
    with pytest.raises(ValueError):extract_pdf(source,tmp_path/"out",pages=[0])
    with pytest.raises(ValueError):extract_pdf(source,tmp_path/"out",dpi=1000)


def test_duplicate_values_marked_without_deleting_or_merging():
    values=[{"page_no":1,"value_decimal":"100","source_kind":"native_table","candidate_id":"a"},
            {"page_no":1,"value_decimal":"100","source_kind":"vision_chart","candidate_id":"b"},
            {"page_no":2,"value_decimal":"100","source_kind":"native_table","candidate_id":"c"}]
    mark_duplicates(values)
    assert len(values)==3 and values[0]["possible_duplicate_group"]==values[1]["possible_duplicate_group"]
    assert "possible_duplicate_group" not in values[2]


def test_vision_transport_uses_picture_and_respects_budget(monkeypatch,tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY","fake-key-test-only")
    image=tmp_path/"image.png";image.write_bytes(b"test-image-bytes-not-a-real-image")
    requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(json.loads(request.data));return io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(vision_data())}}],"usage":{"total_tokens":10}}).encode())
    monkeypatch.setattr("esg_extraction.vision.build_opener",lambda *args:Opener())
    client=AliVisionClient(max_calls=1)
    response=client(image)
    assert response["data"]["regions"] and client.calls==1
    assert requests[0]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "fake-key-test-only" not in json.dumps(requests)
    with pytest.raises(RuntimeError):client(image)
    assert NoRedirect().redirect_request() is None




def test_vision_unauthorized_does_not_repeat_requests(monkeypatch,tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY","fake-key-test-only")
    image=tmp_path/"test.png";image.write_bytes(b"test")
    class Opener:
        def open(self,*args,**kwargs):raise HTTPError("https://example",401,"private-message",None,None)
    monkeypatch.setattr("esg_extraction.vision.build_opener",lambda *args:Opener())
    client=AliVisionClient(max_calls=10)
    for _ in range(2):
        with pytest.raises(RuntimeError,match="VISION_HTTP_401"):client(image)
    assert client.calls==1


def test_usage_ledger_retains_tokens_for_rejected_response(monkeypatch,tmp_path):
    monkeypatch.setenv("DASHSCOPE_API_KEY","fake-key-test-only")
    image=tmp_path/"test.png";image.write_bytes(b"test")
    class Opener:
        def open(self,*args,**kwargs):
            return io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":"{}"}}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}).encode())
    monkeypatch.setattr("esg_extraction.vision.build_opener",lambda *args:Opener())
    ledger=tmp_path/"usage/test.jsonl"
    client=AliVisionClient(max_calls=1,ledger_path=ledger)
    with pytest.raises(ValueError):client(image)
    events=[json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert events[0]["phase"]=="start" and events[-1]["phase"]=="end"
    assert events[-1]["status"]=="VALIDATION_FAILED"
    assert events[-1]["usage"]["total_tokens"]==120 and client.total_tokens==120
    assert "fake-key-test-only" not in ledger.read_text(encoding="utf-8")


def test_usage_summary_deduplicates_events_and_does_not_count_unknown_as_zero(tmp_path):
    sys.path.insert(0,str(ROOT/"scripts"))
    from esg_extraction.history_tools import usage_summary
    folder=tmp_path/"usage";folder.mkdir()
    events=[{"attempt_id":"a","phase":"start","context":{"pdf_name":"test"},"usage":None},
            {"attempt_id":"a","phase":"end","context":{"pdf_name":"test"},"status":"VALIDATED","usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}},
            {"attempt_id":"b","phase":"start","context":{"pdf_name":"test"},"usage":None}]
    (folder/"test.jsonl").write_text('\n'.join(json.dumps(e) for e in events),encoding="utf-8")
    result=usage_summary(tmp_path)
    assert result["api_attempts"]==2 and result["known_total_tokens"]==120
    assert result["requests_with_unknown_usage"]==1 and not result["total_is_complete"]


def test_offline_representation_repair_does_not_invent_zero_or_change_numbers():
    sys.path.insert(0,str(ROOT/"scripts"))
    from esg_extraction.history_tools import normalize
    raw=vision_data()
    raw["regions"][0]["observations"].append({"metric_label":"Missing value","value_text":"—","unit":None,"year":None,"scope":None,"series":None,"category":None,"value_kind":"LABELLED","evidence_quote":None})
    before=deepcopy(raw)
    repaired,changes=normalize(raw)
    assert raw==before
    assert repaired["regions"][0]["observations"][0]["value_text"]=="1234"
    assert repaired["regions"][0]["observations"][1]["value_text"]=="—"
    assert repaired["regions"][0]["observations"][1]["value_kind"]=="UNCERTAIN"
    assert changes


@pytest.mark.parametrize("text",["Minimal","N/A*","Gradually reduce carbon emissions"])
def test_offline_qualitative_text_is_retained_without_a_numeric_value(text):
    sys.path.insert(0,str(ROOT/"scripts"))
    from esg_extraction.history_tools import normalize
    raw=vision_data()
    row=deepcopy(raw["regions"][0]["observations"][0])
    row.update(value_text=text,value_kind="LABELLED")
    raw["regions"][0]["observations"].append(row)
    before=deepcopy(raw)
    repaired,_=normalize(raw)
    assert raw==before
    assert repaired["regions"][0]["observations"][0]["value_text"]=="1234"
    assert repaired["regions"][0]["observations"][1]["value_text"]==text
    assert repaired["regions"][0]["observations"][1]["value_kind"]=="UNCERTAIN"


@pytest.mark.skipif(os.name!="nt",reason="Actual cmd.exe")
def test_extraction_batch_help(tmp_path):
    import sys
    result=subprocess.run([sys.executable,str(ROOT/'scripts/run_esg_pipeline.py'),'--help'],cwd=tmp_path,capture_output=True,timeout=20)
    assert result.returncode==0 and b"--plan" in result.stdout
