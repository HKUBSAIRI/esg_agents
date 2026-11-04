"""B fixture/file integration is tested now; actual A->B integration is explicitly gated."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from fake_client import FakeClient
from draft_projection import validate_projection
from esg_semantic.auditor import run_audit_file
from esg_semantic.audit.common import resources
from esg_semantic.audit.events import capture_events
from esg_semantic.audit.files import file_sha, OUTPUTS, REQUIRED
from esg_semantic.audit.integration import integration_context
from esg_semantic.audit.indexes import InputError

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=ROOT/"tests"/"fixtures"/"audit"
POLICY=ROOT/"skills"/"audit"/"policy.json"


def copy_case(tmp_path,name="normal"):
    target=tmp_path/name
    target.mkdir()
    for path in (FIXTURES/name).iterdir():
        if path.is_file(): shutil.copy2(path,target/path.name)
    return target


@pytest.mark.parametrize("name,expected",[("normal","A-EVIDENCE-WEAK"),("bad_unit","A-UNIT-MISMATCH"),
    ("bad_normalization","A-NORMALIZATION-INVALID"),("bad_year","A-YEAR-MISMATCH"),
    ("duplicate","A-DUPLICATE-UNMERGED"),("filter_overdrop","A-FILTER-OVERDROP"),("missing_key","A-MISSING-KEY")])
def test_fixture_file_pipeline_publishes_four_valid_outputs(tmp_path,name,expected):
    directory=copy_case(tmp_path,name)
    inputs={p.name:file_sha(p) for p in directory.iterdir()}
    with integration_context(fixture=True,validator=validate_projection):
        result=run_audit_file(directory,policy_path=POLICY,llm=FakeClient(),run_id="ar_files")
    assert expected in {f["code"] for f in result["findings"]}
    published=directory/"audit_runs"/"ar_files"
    assert {p.name for p in published.iterdir()}==set(OUTPUTS)
    assert inputs=={name:file_sha(directory/name) for name in inputs}
    for path in published.iterdir():
        if path.suffix==".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines(): assert isinstance(json.loads(line),dict)
    log=(directory/"logs"/"audit"/"SYNTHETIC-B-001"/"ar_files.jsonl").read_text(encoding="utf-8")
    assert "# ESG 审查 Agent" not in log
    assert '"event":"audit_group_end"' in log
    assert "prompt_sha256" in log


def test_dry_run_has_no_model_log_or_directory_write(tmp_path):
    directory=copy_case(tmp_path)
    before=set(directory.rglob("*"))
    client=FakeClient()
    result=run_audit_file(directory,policy_path=POLICY,llm=client,dry_run=True)
    assert result["dry_run"] is True
    assert client.calls==[]
    assert set(directory.rglob("*"))==before


def test_required_file_missing_fails_before_any_write(tmp_path):
    directory=copy_case(tmp_path)
    (directory/"semantic_filter.jsonl").unlink()
    with integration_context(fixture=True):
        with pytest.raises(InputError) as exc: run_audit_file(directory,policy_path=POLICY,llm=FakeClient())
    assert exc.value.code=="ERR_AUDIT_INPUT_MISSING"
    assert not (directory/"audit_runs").exists()


def test_optional_files_missing_disable_findings(tmp_path):
    directory=copy_case(tmp_path)
    (directory/"coverage.json").unlink(); (directory/"filter_audit_sample.jsonl").unlink()
    with integration_context(fixture=True): result=run_audit_file(directory,policy_path=POLICY,llm=FakeClient())
    assert "A-MISSING-KEY" not in {f["code"] for f in result["findings"]}


def test_file_mutation_detected_and_no_confirmed_published(tmp_path):
    directory=copy_case(tmp_path)
    client=FakeClient()
    def changing(prompt,*,max_attempts):
        path=directory/"records.jsonl"
        path.write_text(path.read_text(encoding="utf-8")+"\n",encoding="utf-8")
        return client(prompt,max_attempts=max_attempts)
    with integration_context(fixture=True): result=run_audit_file(directory,policy_path=POLICY,llm=changing)
    assert result["status"]=="FAILED"
    assert all(r["review_status"]=="INSUFFICIENT" for r in result["record_results"])
    assert any(p["error_code"]=="ERR_AUDIT_MUTATED_UPSTREAM" for p in result["problems"])


def test_run_id_reuse_does_not_overwrite_and_unsafe_id_rejected(tmp_path):
    directory=copy_case(tmp_path)
    with integration_context(fixture=True):
        run_audit_file(directory,policy_path=POLICY,llm=FakeClient(),run_id="one")
        before=(directory/"audit_runs"/"one"/"audit_report.json").read_bytes()
        with pytest.raises(InputError): run_audit_file(directory,policy_path=POLICY,llm=FakeClient(),run_id="one")
        with pytest.raises(InputError): run_audit_file(directory,policy_path=POLICY,llm=FakeClient(),run_id="../escape")
        assert before==(directory/"audit_runs"/"one"/"audit_report.json").read_bytes()


def test_formal_file_entry_cannot_implicitly_use_fixtures(tmp_path):
    directory=copy_case(tmp_path)
    with pytest.raises(InputError) as exc: run_audit_file(directory,policy_path=POLICY,llm=FakeClient())
    assert exc.value.code=="ERR_CONTRACT_NOT_READY"


def test_client_failure_still_emits_all_rows_and_no_secrets(tmp_path):
    directory=copy_case(tmp_path,"llm_failure")
    with integration_context(fixture=True):
        result=run_audit_file(directory,policy_path=POLICY,llm=FakeClient(error=TimeoutError("API-KEY-SECRET")))
    assert len(result["record_results"])==3
    assert "CONFIRMED" not in {r["review_status"] for r in result["record_results"]}
    for path in directory.rglob("*.json*"):
        assert "API-KEY-SECRET" not in path.read_text(encoding="utf-8")


def test_cli_dry_run_and_no_implicit_fake(tmp_path):
    directory=copy_case(tmp_path)
    def command(*args):
        return subprocess.run([sys.executable,str(ROOT/"scripts"/"esg_audit_run.py"),"--input-dir",str(directory),*args],capture_output=True,timeout=15)
    assert command("--dry-run").returncode==0
    assert command().returncode!=0
    result=command("--fixture-mode","--run-id","cli")
    assert result.returncode==0, result.stderr.decode("utf-8")
    assert b"PARTIAL" in result.stdout


@pytest.mark.joint
def test_actual_A_filter_classify_audit_joint_acceptance():
    bridge=os.environ.get("ESG_AUDIT_A_BRIDGE")
    if not bridge:
        pytest.skip("A code/contracts/bridge not delivered; B fixtures are not joint acceptance.")
    module,sep,name=bridge.partition(":")
    assert sep
    setup=getattr(importlib.import_module(module),name)()
    assert setup["a_source_revision"] and callable(setup["run_filter_classify"])
    # Actual A implementation is called by its bridge, not replaced by B's fixture builders.
    data=setup["run_filter_classify"]()
    from esg_semantic.audit.common import mapping,sha
    from esg_semantic.auditor import audit_classification
    before=sha({k:[mapping(x) for x in data[k]] for k in ("records","evidence","decisions")})
    with integration_context(**setup["integration"]):
        assert not setup["integration"].get("fixture",False)
        result=mapping(audit_classification(**data,policy=setup["policy"],run={"run_id":"joint_acceptance"},llm=setup["llm"]))
    assert result["status"] in ("SUCCEEDED","PARTIAL")
    assert len(result["record_results"])==len(data["records"])
    assert not any(p["error_code"]=="CONTRACT_UNVERIFIED_FIXTURE_ONLY" for p in result["problems"])
    assert before==sha({k:[mapping(x) for x in data[k]] for k in ("records","evidence","decisions")})
