from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile
import subprocess
import sys
from unittest.mock import patch

from review_workbench.auditor import audit
from review_workbench.contracts import (BUNDLE, SCHEMAS, ContractError, canonical, digest, loads, validate, validate_bundle)
from review_workbench.make_demo import build
from review_workbench.providers import HTTPProvider, ModelFailure, OfflineProvider, ReplayProvider, Reply
from review_workbench.server import LocalServer
from review_workbench.service import Application, build_tasks, handoff
from review_workbench.store import Conflict, Store


class FixedProvider:
    name, model = "test", "fixture"
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def review(self, context, max_tokens):
        self.calls += 1
        return Reply(self.fn(context), {})


def confirmed(context):
    return {"record_verdicts": [{"record_uid": r["record_uid"], "verdict": "CONFIRMED", "reason": "原文与记录一致"}
                                for r in context["indicator_group"]["records"]], "findings": []}


def single():
    bundle, _ = build()
    bundle["records"] = [bundle["records"][-1]]
    bundle["evidence"] = [next(e for e in bundle["evidence"] if e["evidence_id"] == "demo_e_zero")]
    bundle["catalogue"]["indicators"] = [bundle["catalogue"]["indicators"][1]]
    return bundle


class ContractTests(unittest.TestCase):
    def test_demo_valid(self):
        validate_bundle(build()[0])

    def test_zero_is_valid(self):
        self.assertEqual(validate_bundle(single())["records"][0]["value"], 0)

    def test_bool_not_number(self):
        bundle = single(); bundle["records"][0]["value"] = True
        with self.assertRaises(ContractError): validate_bundle(bundle)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ContractError): loads('{"a": 1, "a":2}')

    def test_nonfinite_rejected(self):
        with self.assertRaises(ContractError): loads('{"a": NaN}')

    def test_overflow_number_rejected_in_extensions(self):
        with self.assertRaises(ContractError): loads('{"extensions": {"a":1e9999}}')

    def test_unknown_field_rejected(self):
        bundle = single(); bundle["invented"] = 1
        with self.assertRaises(ContractError): validate_bundle(bundle)

    def test_page_zero_rejected(self):
        bundle = single(); bundle["evidence"][0]["page_no"] = 0
        with self.assertRaises(ContractError): validate_bundle(bundle)

    def test_invalid_bbox_rejected(self):
        bundle = single(); bundle["evidence"][0]["bbox"] = [100, 10, 20, 30]
        with self.assertRaises(ContractError): validate_bundle(bundle)

    def test_duplicate_record_rejected(self):
        bundle = single(); bundle["records"] *= 2
        with self.assertRaises(ContractError): validate_bundle(bundle)

    def test_required_schema_fields_defined(self):
        def walk(schema):
            if schema.get("additionalProperties") is False:
                self.assertFalse(set(schema.get("required", [])) - set(schema["properties"]))
            for child in schema.get("properties", {}).values(): walk(child)
            if "items" in schema: walk(schema["items"])
        for schema in SCHEMAS.values(): walk(schema)

    def test_exported_schemas_match_canonical_contracts(self):
        for name, schema in SCHEMAS.items():
            saved = loads((Path(__file__).parent.parent / "review_workbench" / "schemas" / (name + ".schema.json")).read_text(encoding="utf-8"))
            saved.pop("$schema")
            self.assertEqual(schema, saved)


class AuditorTests(unittest.TestCase):
    def test_demo_readonly_and_expected_findings(self):
        bundle, responses = build(); before = digest(bundle)
        result = audit(bundle, ReplayProvider(responses), "test_run")
        self.assertEqual(before, digest(bundle))
        self.assertEqual(len(result["findings"]), 6)
        self.assertEqual(result["stats"]["CONFIRMED"], 1)
        self.assertIn("A-MISSING-KEY", {f["code"] for f in result["findings"]})
        self.assertEqual(len(build_tasks("test_run", bundle, result)), 6)

    def test_offline_never_passes(self):
        result = audit(single(), OfflineProvider(), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")
        self.assertEqual(result["status"], "PARTIAL")

    def test_zero_can_be_confirmed(self):
        result = audit(single(), FixedProvider(confirmed), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "CONFIRMED")

    def test_unknown_code_does_not_pass(self):
        def reply(c):
            data = confirmed(c)
            data["findings"] = [{"record_uid": "demo_r_zero", "code": "A-INVENTED", "severity": "BLOCK", "confidence": .9,
                                 "evidence_ids": ["demo_e_zero"], "proposed_action": "KEEP", "reason": "测试"}]
            return data
        result = audit(single(), FixedProvider(reply), "test_run")
        self.assertEqual(result["issues"][0]["code"], "MODEL_CONTRACT_INVALID")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")

    def finding(self, context):
        data = confirmed(context)
        data["findings"] = [{"record_uid": "demo_r_zero", "code": "A-UNIT-MISMATCH", "severity": "BLOCK", "confidence": .6,
                             "evidence_ids": ["demo_e_zero"], "proposed_action": "ESCALATE", "reason": "测试需核对单位"}]
        return data

    def test_low_confidence_block_downgraded(self):
        result = audit(single(), FixedProvider(self.finding), "test_run")
        self.assertEqual(result["findings"][0]["severity"], "WARN")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "SUSPECT")

    def test_dangling_reference_rejected(self):
        def reply(c):
            data = self.finding(c); data["findings"][0]["evidence_ids"] = ["made_up"]
            return data
        result = audit(single(), FixedProvider(reply), "test_run")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "PARTIAL")

    def test_proposed_variable_outside_catalogue_rejected(self):
        def reply(c):
            data = self.finding(c); data["findings"][0]["proposed_variable"] = "imaginary"
            return data
        result = audit(single(), FixedProvider(reply), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")

    def test_correction_requires_actual_quote(self):
        def reply(c):
            data = self.finding(c)
            data["findings"][0].update(proposed_action="CORRECT_VALUE", proposed_value=0, evidence_span="伪造原文")
            return data
        self.assertEqual(audit(single(), FixedProvider(reply), "test_run")["status"], "PARTIAL")

    def test_omitted_record_verdict_rejected(self):
        result = audit(single(), FixedProvider(lambda c: {"record_verdicts": [], "findings": []}), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")

    def test_missing_source_never_confirmed(self):
        bundle = single(); bundle["evidence"] = []
        result = audit(bundle, FixedProvider(confirmed), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")

    def test_truncation_never_confirmed(self):
        bundle = single(); bundle["evidence"][0]["content"] *= 50
        result = audit(bundle, FixedProvider(confirmed), "test_run")
        self.assertEqual(result["record_verdicts"][0]["verdict"], "INSUFFICIENT")
        self.assertTrue(result["model_calls"][0]["request"]["indicator_group"]["evidence"][0]["truncated"])

    def test_unresolved_provenance_never_confirmed(self):
        bundle = single(); bundle["records"][0]["provenance_source"] = "unresolved"
        self.assertEqual(audit(bundle, FixedProvider(confirmed), "test_run")["record_verdicts"][0]["verdict"], "INSUFFICIENT")

    def test_budget_exhaustion_keeps_all_records(self):
        bundle, responses = build(); bundle["review_policy"]["max_model_calls"] = 1
        result = audit(bundle, ReplayProvider(responses), "test_run")
        self.assertEqual(len(result["record_verdicts"]), len(bundle["records"]))
        self.assertEqual(len(result["model_calls"]), 1)
        self.assertIn("MODEL_BUDGET_EXHAUSTED", {i["code"] for i in result["issues"]})

    def test_retry_counts_toward_budget(self):
        bundle = single(); bundle["review_policy"]["max_model_calls"] = 2
        class Broken(FixedProvider):
            def review(self, context, max_tokens):
                raise ModelFailure("MODEL_NETWORK_FAILURE", True)
        result = audit(bundle, Broken(confirmed), "test_run")
        self.assertEqual(len(result["model_calls"]), 2)
        self.assertEqual(result["status"], "PARTIAL")

    def test_groups_over_20_are_complete_and_conservative(self):
        bundle = single()
        for i in range(24):
            record = deepcopy(bundle["records"][0]); record["record_uid"] = "r_" + str(i); bundle["records"].append(record)
        provider = FixedProvider(confirmed)
        result = audit(bundle, provider, "test_run")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(result["record_verdicts"]), 25)
        self.assertTrue(all(len(c["request"]["indicator_group"]["records"]) <= 20 for c in result["model_calls"]))
        self.assertTrue(all(v["verdict"] == "INSUFFICIENT" for v in result["record_verdicts"]))

    def test_empty_records_still_check_missing(self):
        bundle = single(); bundle["records"] = []
        provider = FixedProvider(confirmed); audit(bundle, provider, "test_run")
        self.assertEqual(provider.calls, 1)

    def test_empty_evidence_does_not_finish_missing_check(self):
        bundle = single(); bundle["records"] = []; bundle["evidence"] = []
        result = audit(bundle, FixedProvider(confirmed), "test_run")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(build_tasks("test_run", bundle, result)), 1)

    def test_candidate_value_must_be_in_quote(self):
        def reply(c):
            data = self.finding(c)
            data["findings"][0].update(proposed_action="CORRECT_VALUE", proposed_value=888, evidence_span="实际排放量为 0 t")
            return data
        self.assertEqual(audit(single(), FixedProvider(reply), "test_run")["status"], "PARTIAL")

    def test_duplicate_finding_cannot_hide_block(self):
        def reply(c):
            data = self.finding(c)
            weak = deepcopy(data["findings"][0]); weak.update(severity="WARN", confidence=.8)
            data["findings"].insert(0, weak); data["findings"][1]["confidence"] = .9
            return data
        result = audit(single(), FixedProvider(reply), "test_run")
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["severity"], "BLOCK")

    def test_extensions_not_sent_to_model(self):
        bundle = single(); bundle["records"][0]["extensions"] = {"private_note": "not-needed-for-review"}
        bundle["report_context"]["extensions"] = {"private_note": "not-needed-for-review"}
        result = audit(bundle, FixedProvider(confirmed), "test_run")
        self.assertNotIn("not-needed-for-review", canonical(result["model_calls"]))

    def test_provider_cannot_mutate_input_or_call_receipt(self):
        bundle = single(); before = digest(bundle)
        def reply(context):
            context["review_policy"]["policy_version"] = "tampered"
            context["indicator_group"]["variables"].append("tampered")
            return confirmed(context)
        result = audit(bundle, FixedProvider(reply), "readonly_check")
        self.assertEqual(before, digest(bundle))
        self.assertNotEqual(result["model_calls"][0]["request"]["review_policy"]["policy_version"], "tampered")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(self.temp.name); self.app = Application(self.store)
        self.run = self.app.demo(); self.app.active.result(timeout=10)

    def tearDown(self):
        self.app.close(); self.temp.cleanup()

    def decision(self, action="KEEP", version=0):
        return {"action": action, "expected_version": version, "reviewer": "测试研究员", "reason": "已核对原文及披露范围"}

    def test_demo_identity_cannot_be_forged(self):
        bundle, _ = build(); bundle["records"][0]["value"] = 201
        identity = self.app.import_data(bundle)
        with self.assertRaises(ContractError): self.app.start(identity, "demo_replay")

    def test_known_demo_import_stays_synthetic(self):
        identity = self.app.import_data(build()[0])
        self.assertTrue(self.store.get_bundle(identity)["is_demo"])

    def test_browser_display_preserves_input_digits(self):
        bundle = single(); bundle["records"][0]["value"] = 123456789012345678901
        identity = self.app.import_data(bundle)
        run = self.app.start(identity, "offline"); self.app.active.result(timeout=10)
        self.assertEqual(self.app.detail(run)["display_values"]["demo_r_zero"], "123456789012345678901")

    def test_input_and_results_immutable_after_review(self):
        before = self.app.detail(self.run); task = before["tasks"][0]
        self.app.review(self.run, task["task_id"], self.decision())
        after = self.app.detail(self.run)
        self.assertEqual(before["bundle"], after["bundle"])
        self.assertEqual(before["result"], after["result"])
        self.assertEqual(after["tasks"][0]["version"], 1)

    def test_keep_only_advances_to_s5_validation(self):
        detail = self.app.detail(self.run); task = detail["tasks"][0]
        self.app.review(self.run, task["task_id"], self.decision())
        detail = self.app.detail(self.run)
        row = next(r for r in detail["handoff"]["records"] if r["record_uid"] == task["record_uid"])
        self.assertEqual(row["state"], "READY_FOR_S5")
        self.assertTrue(row["s5_validation_required"])

    def test_stale_review_rejected(self):
        task = self.app.detail(self.run)["tasks"][0]
        self.app.review(self.run, task["task_id"], self.decision())
        with self.assertRaises(Conflict): self.app.review(self.run, task["task_id"], self.decision())

    def test_parallel_review_only_one_succeeds(self):
        task = self.app.detail(self.run)["tasks"][0]
        def submit():
            try:
                self.app.review(self.run, task["task_id"], self.decision()); return True
            except Conflict: return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(len(self.app.detail(self.run)["events"]), 1)

    def test_zero_correction_requires_reaudit_and_cannot_be_cleared(self):
        task = self.app.detail(self.run)["tasks"][0]
        self.app.review(self.run, task["task_id"], {**self.decision("CORRECT_VALUE"), "proposed_value": 0})
        detail = self.app.detail(self.run)
        row = next(r for r in detail["handoff"]["records"] if r["record_uid"] == task["record_uid"])
        self.assertEqual(row["state"], "REAUDIT_REQUIRED")
        with self.assertRaises(Conflict): self.app.review(self.run, task["task_id"], self.decision(version=1))

    def test_group_cannot_correct_nonexistent_record(self):
        task = next(t for t in self.app.detail(self.run)["tasks"] if t["record_uid"] is None)
        with self.assertRaises(ContractError):
            self.app.review(self.run, task["task_id"], {**self.decision("CORRECT_VALUE"), "proposed_value": 1})

    def test_exclusion_preserves_record_and_does_not_pass(self):
        task = self.app.detail(self.run)["tasks"][0]
        self.app.review(self.run, task["task_id"], self.decision("EXCLUDE_FROM_SCORING"))
        detail = self.app.detail(self.run)
        self.assertEqual(len(detail["bundle"]["records"]), 6)
        self.assertEqual(detail["handoff"]["records"][0]["state"], "EXCLUDED_FROM_SCORING")

    def test_defer_preserves_open_state(self):
        task = self.app.detail(self.run)["tasks"][0]
        self.app.review(self.run, task["task_id"], self.decision("DEFER"))
        self.assertEqual(self.app.detail(self.run)["tasks"][0]["state"], "OPEN")

    def test_export_has_integrity_and_blocking_state(self):
        exported = self.app.export(self.run)
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            manifest = loads(archive.read("manifest.json").decode())
            for filename, expected in manifest["file_sha256"].items():
                self.assertEqual(hashlib.sha256(archive.read(filename)).hexdigest(), expected)
            downstream = loads(archive.read("s5_handoff.json").decode())
            self.assertIn("REVIEW_REQUIRED", {r["state"] for r in downstream["records"]})
            self.assertEqual(manifest["input_sha256"], digest(loads(archive.read("input.bundle.json").decode())))

    def test_restart_interrupts_only_running_work(self):
        bundle = self.app.import_data(single()); unfinished = self.store.create_run(bundle, "offline")
        self.store.recover()
        self.assertEqual(self.app.detail(unfinished)["status"], "INTERRUPTED")
        self.assertEqual(self.app.detail(self.run)["status"], "SUCCEEDED")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(self.temp.name); self.app = Application(self.store)
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.app.close(); self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        request = Request(self.url + path, data=canonical(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        return urlopen(request, timeout=10)

    def test_html_and_bootstrap(self):
        with self.request("/") as response:
            self.assertIn(b"app.js", response.read()); self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        with self.request("/api/bootstrap") as response:
            self.assertFalse(loads(response.read().decode())["http_model_configured"])

    def test_cross_site_post_rejected(self):
        with self.assertRaises(HTTPError) as cm:
            self.request("/api/demo", {}, {"Origin": "http://evil.example", "X-Workbench-Token": self.server.token})
        self.assertEqual(cm.exception.code, 403)

    def test_missing_token_rejected(self):
        with self.assertRaises(HTTPError) as cm: self.request("/api/demo", {})
        self.assertEqual(cm.exception.code, 403)

    def test_path_traversal_not_served(self):
        with self.assertRaises(HTTPError) as cm: self.request("/../review_workbench.local.json")
        self.assertEqual(cm.exception.code, 404)

    def test_real_http_end_to_end_demo_review_export(self):
        headers = {"X-Workbench-Token": self.server.token}
        with self.request("/api/demo", {}, headers) as response:
            self.assertEqual(response.status, 202); run = loads(response.read().decode())["run_id"]
        self.app.active.result(timeout=10)
        with self.request(f"/api/runs/{run}") as response: detail = loads(response.read().decode())
        task = detail["tasks"][0]
        decision = {"action": "CORRECT_VALUE", "proposed_value": 0, "reviewer": "HTTP测试", "reason": "测试修订零值", "expected_version": 0}
        with self.request(f"/api/runs/{run}/tasks/{task['task_id']}/review", decision, headers) as response:
            self.assertEqual(response.status, 200)
        with self.request(f"/api/runs/{run}/export") as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertIn("REAUDIT_REQUIRED", archive.read("s5_handoff.json").decode())

    def test_model_credentials_not_in_browser(self):
        provider = HTTPProvider("https://example.invalid/chat/completions", "SECRET-TEST-KEY", "model")
        self.app.http_provider = provider
        with self.request("/api/bootstrap") as response:
            raw = response.read().decode(); self.assertNotIn("SECRET-TEST-KEY", raw)
        self.assertIn("model", raw)


class ProviderTests(unittest.TestCase):
    def test_http_payload_and_response_without_external_call(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum):
                return canonical({"choices": [{"finish_reason": "stop", "message": {
                    "content": canonical({"record_verdicts": [], "findings": []})}}], "usage": {"total_tokens": 20}}).encode()
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response()
        opener = Opener()
        with patch("review_workbench.providers.build_opener", return_value=opener):
            result = HTTPProvider("https://example.invalid/chat/completions", "private-key", "test-model").review({"test": 1}, 4000)
        payload = loads(opener.request.data.decode())
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_tokens"], 4000)
        self.assertEqual(result.data["findings"], [])
        self.assertNotIn("private-key", canonical(payload))

    def test_incomplete_model_response_fails_closed(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, maximum): return b'{"choices":[{"finish_reason":"length"}]}'
        class Opener:
            def open(self, *args, **kwargs): return Response()
        with patch("review_workbench.providers.build_opener", return_value=Opener()):
            with self.assertRaises(ModelFailure) as cm:
                HTTPProvider("https://example.invalid/chat/completions", "private-key", "test-model").review({}, 4000)
        self.assertEqual(cm.exception.code, "MODEL_OUTPUT_INCOMPLETE")

    def test_reject_insecure_endpoint(self):
        with self.assertRaises(ValueError): HTTPProvider("http://example.invalid", "key", "model")


class InstanceLockTests(unittest.TestCase):
    def test_second_server_cannot_take_running_directory(self):
        from review_workbench.instance_lock import InstanceLock
        with tempfile.TemporaryDirectory() as directory:
            with InstanceLock(Path(directory)):
                with self.assertRaises(ValueError):
                    with InstanceLock(Path(directory)): pass
            with InstanceLock(Path(directory)): pass


class CLITests(unittest.TestCase):
    def test_pack_check_import_and_startup_preserve_source(self):
        root = Path(__file__).parent.parent
        bundle, _ = build()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            files = {"metadata.json": canonical({k:v for k,v in bundle.items() if k not in ("records", "evidence")}),
                     "records.jsonl": "\n".join(canonical(r) for r in bundle["records"]),
                     "evidence.jsonl": "\n".join(canonical(e) for e in bundle["evidence"])}
            for name, text in files.items(): (folder / name).write_text(text, encoding="utf-8")
            before = {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in files}
            def run(*args):
                return subprocess.run([sys.executable, "-m", "review_workbench", *map(str,args)], cwd=root,
                                      capture_output=True, timeout=20)
            packed = folder / "input.bundle.json"
            args = ["pack", "--metadata",folder/"metadata.json", "--evidence",folder/"evidence.jsonl", "--records",folder/"records.jsonl", "--out",packed]
            self.assertEqual(run(*args).returncode, 0)
            self.assertNotEqual(run(*args).returncode, 0)  # existing output never overwritten
            self.assertEqual(run("check", packed).returncode, 0)
            self.assertEqual(run("--data-dir",folder/"db", "import", packed).returncode, 0)
            store = Store(folder/"db")
            detail = store.detail(store.list_runs()[0]["run_id"])
            self.assertTrue(detail["is_demo"])
            self.assertEqual(detail["provider"], "offline")
            self.assertTrue(detail["tasks"])
            self.assertEqual(before, {name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in files})
            # Exercise the same entry point used by the batch launcher, without opening a user window.
            process = subprocess.Popen([sys.executable,"-m","review_workbench","--data-dir",str(folder/"db"),"--port","0"],
                                       cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                import time
                deadline = time.monotonic() + 10
                while not (folder/"db"/"server.lock").exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertIsNone(process.poll())
                self.assertTrue((folder/"db"/"server.lock").exists())
            finally:
                process.terminate(); process.communicate(timeout=10)

    def test_public_schemas_with_independent_validator_if_available(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("Optional independent jsonschema validator not installed")
        for schema in SCHEMAS.values(): Draft202012Validator.check_schema(schema)
        bundle, responses = build()
        Draft202012Validator(BUNDLE).validate(bundle)
        result = audit(bundle, ReplayProvider(responses), "independent_schema_check")
        for finding in result["findings"]: Draft202012Validator(SCHEMAS["audit_finding"]).validate(finding)
        for group in result["groups"]: Draft202012Validator(SCHEMAS["audit_outcome"]).validate(group)


if __name__ == "__main__":
    unittest.main()
