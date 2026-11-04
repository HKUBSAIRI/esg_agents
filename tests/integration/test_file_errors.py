import json
from pathlib import Path
import shutil

import pytest

from fake_client import FakeClient
from esg_semantic.auditor import run_audit_file
from esg_semantic.audit.files import load_file
from esg_semantic.audit.integration import integration_context
from esg_semantic.audit.indexes import InputError
from test_filter_classify_audit import copy_case, POLICY


def test_jsonl_scalar_and_context_scalar_rejected(tmp_path):
    path=tmp_path/"scalar.jsonl"; path.write_text("1\n",encoding="utf-8")
    with pytest.raises(InputError): load_file(path,jsonl=True)
    directory=copy_case(tmp_path)
    (directory/"document_context.json").write_text("[]",encoding="utf-8")
    with integration_context(fixture=True):
        with pytest.raises(InputError): run_audit_file(directory,policy_path=POLICY,llm=FakeClient())


def test_missing_directory_and_large_input(tmp_path):
    with pytest.raises(InputError): run_audit_file(tmp_path/"missing",policy_path=POLICY,llm=None)
    path=tmp_path/"large.jsonl"
    with path.open("wb") as stream:
        stream.seek(32*1024*1024); stream.write(b"x")
    with pytest.raises(InputError): load_file(path,jsonl=True)


def test_atomic_stage_cleaned_on_failure(tmp_path,monkeypatch):
    directory=copy_case(tmp_path)
    import esg_semantic.audit.files as adapter
    def fail(*args,**kwargs): raise OSError("fixture disk error")
    monkeypatch.setattr(adapter,"_write_json",fail)
    with integration_context(fixture=True):
        with pytest.raises(OSError): run_audit_file(directory,policy_path=POLICY,llm=FakeClient(),run_id="fail")
    assert list((directory/"audit_runs").iterdir())==[]


def test_relative_pdf_path_preserved_on_disk_and_fingerprinted(tmp_path):
    from test_auditor_visual import pdf_fixture
    directory=copy_case(tmp_path)
    pdf_fixture(directory/"source.pdf")
    context=json.loads((directory/"document_context.json").read_text(encoding="utf-8"))
    context["pdf_path"]="source.pdf"
    (directory/"document_context.json").write_text(json.dumps(context),encoding="utf-8")
    before=(directory/"document_context.json").read_bytes()
    with integration_context(fixture=True):
        result=run_audit_file(directory,policy_path=POLICY,llm=FakeClient())
    assert before==(directory/"document_context.json").read_bytes()
    log=next((directory/"logs"/"audit"/context["pid"]).glob("*.jsonl")).read_text(encoding="utf-8")
    assert "source.pdf" in log


def test_unsafe_pid_only_affects_safe_log_directory(tmp_path):
    directory=copy_case(tmp_path)
    # Keep record linkage intact while demonstrating path-safe operational logging.
    context=json.loads((directory/"document_context.json").read_text(encoding="utf-8"))
    original=context["pid"]; context["pid"]="../"+original
    (directory/"document_context.json").write_text(json.dumps(context),encoding="utf-8")
    rows=[]
    for line in (directory/"records.jsonl").read_text(encoding="utf-8").splitlines():
        row=json.loads(line); row["pid"]=context["pid"]; row["logical_group_key"]=row["logical_group_key"].replace(original,context["pid"]); rows.append(row)
    (directory/"records.jsonl").write_text("\n".join(json.dumps(r) for r in rows),encoding="utf-8")
    with integration_context(fixture=True):
        run_audit_file(directory,policy_path=POLICY,llm=FakeClient())
    assert all(p.resolve().is_relative_to(directory.resolve()) for p in directory.rglob("*.jsonl"))
