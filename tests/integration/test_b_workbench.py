"""UI adapter tests use synthetic B outputs; no real A contract/model is claimed."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import threading
import types
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import pytest

from review_workbench.b_adapter import OUTPUTS, INPUTS, project, tasks_for
from review_workbench.contracts import ContractError, canonical, loads
from review_workbench.server import LocalServer
from review_workbench.service import Application
from review_workbench.store import Conflict, Store

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ESG_B_WORKBENCH_CONFIG", str(tmp_path / "missing.json"))
    instance = Application(Store(tmp_path / "database"))
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    instance = Application(Store(tmp_path_factory.mktemp("b_web_demo")))
    try:
        rid = instance.b.start(demo=True)
        instance.active.result(timeout=20)
        detail = instance.detail(rid)
        assert detail["status"] == "PARTIAL", detail
        return detail["bundle"]["b_source_files"]
    finally:
        instance.close()


def changed(files, mutate):
    data = {name: loads(text) if not name.endswith(".jsonl") else [loads(line) for line in text.splitlines() if line.strip()] for name, text in files.items()}
    mutate(data)
    return {name: "\n".join(canonical(r) for r in value) if name.endswith(".jsonl") else canonical(value) for name, value in data.items()}


def test_demo_uses_b_engine_and_original_outputs(app):
    run = app.b.start(demo=True)
    app.active.result(timeout=20)
    detail = app.detail(run)
    assert detail["engine"] == "b" and detail["is_demo"]
    assert detail["b_provenance"] == "demo"
    assert set(OUTPUTS) <= set(detail["bundle"]["b_source_files"])
    assert 'rec_b_unknown' in detail['review_routing']['unverified_records']
    assert not detail["handoff"]["production_ready"]
    with pytest.raises(ContractError):
        app.start(detail["bundle_id"], "offline")


def test_import_is_model_free_and_preserves_original_text(app, files, monkeypatch):
    monkeypatch.setattr("review_workbench.b_adapter.core", lambda: pytest.fail("Import must not invoke B model"))
    run = app.b.import_results(files)
    detail = app.detail(run)
    assert detail["b_provenance"] == "import_unverified"
    assert not detail["is_demo"] and not detail["handoff"]["formal_contract_bound"]
    assert detail["bundle"]["b_source_files"] == files


def test_four_outputs_only_still_show_records_and_missing_sources(app, files):
    run = app.b.import_results({n: files[n] for n in OUTPUTS})
    detail = app.detail(run)
    assert len(detail["bundle"]["records"]) == 3
    assert not detail["bundle"]["evidence"]
    assert all(r['source_available'] is False for r in detail['bundle']['records'])
    assert detail['review_routing']['unverified_records']


@pytest.mark.parametrize("status", ["INSUFFICIENT", "NOT_REVIEWED", "SUSPECT", "REJECTED"])
def test_no_finding_record_is_visible(app, files, status):
    def mutate(data):
        row = next(r for r in data["audit_results.jsonl"] if r["record_id"] == "rec_b_1")
        row["review_status"] = status
        data["audit_report.json"]["record_results"] = data["audit_results.jsonl"]
    run = app.b.import_results(changed(files, mutate))
    view=app.detail(run)
    assert not any(t['record_uid']=='rec_b_1' for t in view['tasks'])
    assert 'rec_b_1' in view['review_routing']['unverified_records']
    assert next(v['verdict'] for v in view['result']['record_verdicts'] if v['record_uid']=='rec_b_1')==status


def test_failed_empty_report_has_operational_task(files):
    report = loads(files["audit_report.json"])
    report.update(status="FAILED", findings=[], record_results=[])
    bundle, result = project({"audit_report.json": canonical(report), **{n: "" for n in OUTPUTS if n != "audit_report.json"}}, provenance="import_unverified")
    assert tasks_for("x", bundle, result)==[] and result['status']=='FAILED'


@pytest.mark.parametrize("bad", ["missing", "mixed", "report_disagreement", "queue_disagreement", "duplicate", "bad_reference", "traversal", "invalid_json", "bad_status"])
def test_bad_imports_leave_no_history(app, files, bad):
    if bad == "missing":
        payload = {k: v for k, v in files.items() if k != OUTPUTS[0]}
    elif bad == "traversal":
        payload = {**files, "../../secret.json": "{}"}
    elif bad == "invalid_json":
        payload = {**files, "audit_report.json": '{"x":NaN}'}
    else:
        def mutate(data):
            if bad == "mixed":
                data["audit_results.jsonl"][0]["audit_run_id"] = "other"
                data["audit_report.json"]["record_results"] = data["audit_results.jsonl"]
            elif bad == "report_disagreement":
                data["audit_report.json"]["findings"] = []
            elif bad == "queue_disagreement":
                data["review_items.jsonl"][0]["severity"] = "INFO"
            elif bad == "duplicate":
                data["audit_results.jsonl"].append(deepcopy(data["audit_results.jsonl"][0]))
                data["audit_report.json"]["record_results"] = data["audit_results.jsonl"]
            elif bad == "bad_reference":
                data["audit_results.jsonl"][0]["finding_ids"] = ["missing"]
                data["audit_report.json"]["record_results"] = data["audit_results.jsonl"]
            elif bad == "bad_status":
                data["audit_results.jsonl"][0]["review_status"] = []
                data["audit_report.json"]["record_results"] = data["audit_results.jsonl"]
        payload = changed(files, mutate)
    with pytest.raises(ContractError):
        app.b.import_results(payload)
    assert app.store.list_runs() == []


def test_review_zero_cas_history_export_and_restart(app, files):
    def concrete(data):
        for finding in data['audit_findings.jsonl']:
            if finding.get('record_id')=='rec_b_problem':
                finding.update(code='A-EXTRACT-VALUE',reason='记录值与引用原文数值不同，需核对。',observed='99',expected='10')
                for item in data['review_items.jsonl']:
                    if item['finding_id']==finding['finding_id']:item['code']=finding['code']
        data['audit_report.json']['findings']=data['audit_findings.jsonl']
    files=changed(files,concrete)
    run = app.b.import_results(files)
    detail = app.detail(run)
    task = next(t for t in detail["tasks"] if t["record_uid"] == "rec_b_problem")
    decision = dict(action="CORRECT_VALUE", proposed_value=0, reviewer="复核员", reason="测试零值修订", expected_version=0)
    app.review(run, task["task_id"], decision)
    with pytest.raises(Conflict):
        app.review(run, task["task_id"], decision)
    restored = Application(Store(app.store.directory))
    try:
        current = restored.detail(run)
        assert current["events"][0]["proposed_value"] == 0
        assert current["bundle"]["b_source_files"] == files
        with zipfile.ZipFile(io.BytesIO(restored.export(run))) as archive:
            for name, text in files.items():
                assert archive.read("b_original/" + name).decode("utf-8") == text
            assert "REAUDIT_REQUIRED" in archive.read("workbench/review_events.jsonl").decode("utf-8")
            assert not loads(archive.read("workbench/handoff.json").decode())["production_ready"]
    finally:
        restored.close()


def test_original_decimal_digits_are_preserved_for_browser(app, files):
    original = files["records.jsonl"]
    assert '"value": 1234' in original
    payload = {**files, "records.jsonl": original.replace('"value": 1234', '"value": 1234.1234567890123456789', 1)}
    detail = app.detail(app.b.import_results(payload))
    assert detail["display_values"]["rec_b_1"] == "1234.1234567890123456789"


def test_effective_catalogue_does_not_overwrite_uploaded_original(files):
    configured = {"E2.1.1": {"variables": ["Configured closed-set variable"]}}
    bundle, _ = project(files, provenance="formal_bound", catalogue_override=configured)
    assert bundle["b_source_files"]["catalogue.json"] == files["catalogue.json"]
    assert bundle["catalogue"]["indicators"][0]["variables"] == configured["E2.1.1"]["variables"]


def test_formal_missing_config_cannot_fallback_to_demo(app, files):
    assert not app.b.capabilities()["configured"]
    with pytest.raises(ContractError):
        app.b.start({n: files[n] for n in INPUTS})
    assert not app.store.list_runs()


def configure_factory(tmp_path, monkeypatch, factory):
    module = types.ModuleType("b_web_test_factory")
    module.build = factory
    monkeypatch.setitem(sys.modules, module.__name__, module)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"factory": module.__name__ + ":build"}), encoding="utf-8")
    monkeypatch.setenv("ESG_B_WORKBENCH_CONFIG", str(config))


def test_formal_factory_failure_is_visible_and_never_exposes_secret(app, files, tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("api_key=VERY_SECRET_TEST_VALUE")
    configure_factory(tmp_path, monkeypatch, broken)
    assert app.b.capabilities()["configured"]
    run = app.b.start({n: files[n] for n in INPUTS})
    app.active.result(timeout=20)
    detail = app.detail(run)
    assert detail["status"] == "FAILED" and not detail["tasks"]
    assert not detail["is_demo"]
    assert "VERY_SECRET_TEST_VALUE" not in canonical(detail)
    assert "VERY_SECRET_TEST_VALUE" not in app.export(run).decode("latin1")


def test_formal_bound_execution_with_test_only_validator(app, files, tmp_path, monkeypatch):
    from scripts.esg_audit_run import fake_client
    seen = []
    def build():
        return dict(llm=fake_client(), validator=lambda name, value: seen.append(name), contract_fingerprint="TEST_ONLY_NOT_A_CONTRACT")
    configure_factory(tmp_path, monkeypatch, build)
    run = app.b.start({n: files[n] for n in INPUTS + ("catalogue.json",) if n in files})
    app.active.result(timeout=20)
    detail = app.detail(run)
    assert detail["status"] in {"PARTIAL", "SUCCEEDED"}, detail
    assert detail["b_provenance"] == "formal_bound"
    assert "Record" in seen or any("Record" in n for n in seen)
    assert not detail["handoff"]["production_ready"]


def test_pdf_path_not_read_from_uploaded_json(app, files, tmp_path, monkeypatch):
    configure_factory(tmp_path, monkeypatch, lambda: pytest.fail("Must reject before factory"))
    payload = {n: files[n] for n in INPUTS}
    context = loads(payload["document_context.json"])
    context["pdf_path"] = "C:/private/report.pdf"
    payload["document_context.json"] = canonical(context)
    with pytest.raises(ContractError, match="PDF"):
        app.b.start(payload)


def test_http_import_token_review_attachment_and_export(app, files):
    server = LocalServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def post(path, data, token=True, mime="application/json"):
        headers = {"Content-Type": mime}
        if token:
            headers["X-Workbench-Token"] = server.token
        raw = data if isinstance(data, bytes) else canonical(data).encode("utf-8")
        return urlopen(Request(base + path, data=raw, headers=headers), timeout=10)
    try:
        with pytest.raises(HTTPError) as error:
            post("/api/b/import", {"files": files}, token=False)
        assert error.value.code == 403
        with post("/api/b/import", {"files": files}) as response:
            run = json.load(response)["run_id"]
        with urlopen(base + "/api/runs/" + run) as response:
            assert json.load(response)["engine"] == "b"
        pdf = b"%PDF-1.4\nSynthetic attachment test only"
        with post(f"/api/runs/{run}/pdf", pdf, mime="application/pdf") as response:
            assert response.status == 201
        with urlopen(base + f"/api/runs/{run}/pdf") as response:
            assert response.read() == pdf
        with pytest.raises(HTTPError) as error:
            post(f"/api/runs/{run}/pdf", b"not a PDF", mime="application/pdf")
        assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            post(f"/api/runs/{run}/pdf", pdf + b"different", mime="application/pdf")
        assert error.value.code == 409
        with urlopen(base + f"/api/runs/{run}/export") as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                assert "b_original/audit_results.jsonl" in archive.namelist()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_shared_execution_lock(app):
    from concurrent.futures import Future
    app.active = Future()
    try:
        with pytest.raises(Conflict):
            app.b.start(demo=True)
    finally:
        app.active.set_result(None)
