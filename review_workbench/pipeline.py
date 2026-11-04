"""Workbench application boundary for PDF -> extraction -> A -> B.

Only opaque report/upload identities cross HTTP. Files, credentials and stage
implementations remain behind this boundary; the CLI keeps the same engine.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import sys
import io
import zipfile
import uuid

from .contracts import ContractError
from .store import Conflict, NotFound

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"PARTIAL", "FAILED", "CANCELLED", "INTERRUPTED"}
PDF_LOCK = threading.RLock()


class Stopped(RuntimeError):
    pass


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def lines(path):
    return [json.loads(s) for s in Path(path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]


class PipelineWorkbench:
    def __init__(self, app, *, root=ROOT):
        self.app, self.root = app, Path(root)
        self.directory = app.store.directory / "pipeline_jobs"
        self.output = app.store.directory / "pipeline_results"
        self.lock = threading.RLock()
        self.recovered = False
        self.owned_job = None
        from .batch_queue import BatchQueue
        self.batches = BatchQueue(self)

    def capabilities(self):
        import importlib.util
        available = all(importlib.util.find_spec(name) for name in ("fitz", "jsonschema", "pydantic", "openai", "jinja2", "dotenv"))
        return {"available": bool(available), "api_key_configured": (self.root / "阿里api key").is_file(),
                "max_pdf_mb": 64, "model": "qwen3.8-flash", "engine":"esgagent-v1", "audit_model":"qwen3.7-plus",
                "message": "新版EsgAgent已接入；默认仅复用新版缓存，新PDF需授权模型读取。" if available else "请先运行安装全流程环境.bat，再重启工作台。"}

    def recover_interrupted(self):
        if self.recovered:
            return
        with self.lock:
            for path in self.directory.glob("job_*.json"):
                job = load(path)
                if job["status"] not in TERMINAL:
                    job.update(status="INTERRUPTED", message="上次运行已中断；已保存的提取缓存和结果保留，可重新启动。")
                    save(path, job)
            self.recovered = True
            self.batches.recover()

    def reports(self):
        """Trusted local registry, never a caller-supplied filesystem path."""
        index = self.root / "data/ten_pdf_visual_20261102/results_summary.json"
        latest = self.root / "data/esg_pipeline/latest_run.json"
        results = load(latest).get("reports", []) if latest.is_file() else []
        out = []
        if index.is_file():
            for report in load(index)["reports"]:
                extraction = Path(report["output_directory"])
                if not (extraction / "extraction_report.json").is_file():
                    continue
                context = load(extraction / "extraction_report.json")
                source = Path(context["source_pdf"])
                result = next((r for r in results if r["source_sha256"] == context["source_sha256"]), None)
                existing_run = result.get("workbench_run_id") if result else None
                if existing_run:
                    try:
                        self.app.store.detail(existing_run)
                    except NotFound:
                        existing_run = None
                out.append({"id": "report_" + context["source_sha256"], "name": source.name,
                            "pages": context["page_count"], "has_extraction": True,
                            "workbench_run_id": existing_run,
                            "_source": str(source), "_sha256": context["source_sha256"], "_extraction": str(extraction),
                            "_recovered": str(index.parent / "recovered" / source.stem)})
        sys.path.insert(0,str(ROOT/"src"))
        from esg_pipeline.esgagent_core import delivered_reports
        new_index=self.root/"data/esgagent_pipeline/latest_run.json"
        new_results=load(new_index).get("reports",[]) if new_index.is_file() else []
        # Web/batch completions live in job files rather than the CLI index.
        # Register them too, so a restart does not hide their cached results.
        for path in sorted(self.directory.glob("job_*.json"),key=lambda p:p.stat().st_mtime):
            job=load(path)
            if job.get("kind")=="esgagent" and job.get("summary") and job.get("result_run_id"):
                new_results.append(job["summary"])
        for item in delivered_reports():
            previous=next((s for s in new_results if s["source_sha256"]==item["_sha256"]),None)
            run=previous.get("workbench_run_id") if previous else None
            if run:
                try:self.app.store.detail(run)
                except NotFound:run=None
            out=[r for r in out if r["_sha256"]!=item["_sha256"]]
            out.append({**item,"has_extraction":True,"workbench_run_id":run})
        for summary in new_results:
            worker=Path(summary["output_directory"])/"upstream/worker_result.json"
            if not worker.is_file():continue
            native=load(worker)
            source=Path(native["source_pdf"])
            if not source.is_file():continue
            run=summary.get("workbench_run_id")
            try:self.app.store.detail(run)
            except NotFound:continue
            digest=summary["source_sha256"]
            item=next((r for r in out if r["_sha256"]==digest),None)
            if item is None:
                meta=self.app.store.directory/"pipeline_uploads"/(digest+".json")
                name=load(meta)["name"] if meta.is_file() else native["report"]["source_filename"]
                item={"id":"report_"+digest,"name":name,"_source":str(source),"_sha256":digest,
                      "pages":native["report"]["page_count"]}
                out.append(item)
            item.update(engine="esgagent-v1",has_extraction=True,has_esgagent_cache=True,
                workbench_run_id=run,company_code=native["company"]["hkex_code"].split(".")[0],
                report_year=native["report"]["report_year"])
        return out

    def public_reports(self):
        return [{k:v for k,v in r.items() if not k.startswith("_")} for r in self.reports()]

    def upload(self, raw, name):
        if self.app.active and not self.app.active.done():
            raise Conflict("已有任务运行中，请等待结束后上传。")
        if not 5 <= len(raw) <= 64 * 1024 * 1024 or not raw.startswith(b"%PDF-"):
            raise ContractError("请选择完整的PDF，文件不能超过64 MB。")
        try:
            import fitz
            with PDF_LOCK, fitz.open(stream=raw, filetype="pdf") as pdf:
                if pdf.needs_pass or not 1 <= len(pdf) <= 2000:
                    raise ValueError()
                count = len(pdf)
        except ImportError:
            raise ContractError("缺少PDF依赖，请先运行安装全流程环境.bat。") from None
        except Exception:
            raise ContractError("无法读取PDF，可能已损坏、加密或超过2000页。") from None
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.app.store.directory / "pipeline_uploads"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + ".pdf")
        try:
            with target.open("xb") as stream:
                stream.write(raw)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise Conflict("已有上传文件校验失败。")
        name = str(name).replace("\\", "/").rsplit("/", 1)[-1][:160]
        if not name.lower().endswith(".pdf"):
            name = "上传报告.pdf"
        info = {"id": "upload_" + digest, "name": name, "pages": count, "_source": str(target), "_sha256": digest}
        known=next((r for r in self.reports() if r["_sha256"]==digest),None)
        if known:
            for field in ("company_code","report_year","has_esgagent_cache"):
                if field in known:info[field]=known[field]
        # A per-upload metadata object is not a path supplied by the browser.
        save(directory / (digest + ".json"), info)
        return {k:v for k,v in info.items() if not k.startswith("_")}

    def source(self, identity):
        if not isinstance(identity, str):
            raise ContractError("请选择报告或先上传PDF。")
        report = next((r for r in self.reports() if r["id"] == identity), None)
        if report:
            return report
        if re.fullmatch(r"upload_[a-f0-9]{64}", identity):
            path = self.app.store.directory / "pipeline_uploads" / (identity[7:] + ".json")
            if path.is_file():
                source = load(path)
                # Reuse a previously paid extraction only for identical PDF bytes.
                existing = next((r for r in self.reports() if r["_sha256"] == identity[7:]), None)
                return {**(existing or {}), **source}
        raise NotFound("报告不存在，请重新选择或上传。")

    def prepare(self, body):
        fields = {"source_id", "mode", "vision", "allow_external", "vision_calls", "a_calls", "b_calls"}
        if not isinstance(body, dict) or not fields.issubset(body) or set(body)-fields-{"engine","year","company_code"}:
            raise ContractError("流程参数不完整，请刷新页面后重试。")
        if body["mode"] not in ("rules_only", "model") or any(type(body[k]) is not bool for k in ("vision", "allow_external")):
            raise ContractError("运行模式无效。")
        for name, low, high in (("vision_calls", 0, 100), ("a_calls", 0, 20), ("b_calls", 0, 20)):
            if type(body[name]) is not int or not low <= body[name] <= high:
                raise ContractError("调用上限无效：视觉0至100次，A、B分别0至20次。")
        if body["mode"] == "rules_only" and (body["vision"] or body["allow_external"]):
            raise ContractError("本地模式不调用API，请关闭视觉与外发授权。")
        if body["mode"] == "model" and (not body["allow_external"] or body["a_calls"] + body["b_calls"] < 1):
            raise ContractError("模型模式需勾选外发授权，并给A或B设置正数调用上限。")
        if body["vision"] and body["vision_calls"] < 1:
            raise ContractError("启用视觉时需设置正数调用上限。")
        if not self.capabilities()["available"]:
            raise ContractError("请先安装全流程环境。")
        if body["mode"] == "model" and not self.capabilities()["api_key_configured"]:
            raise ContractError("本机未配置阿里API密钥。")
        source = self.source(body["source_id"])
        # The new core is the default at every entry point. Historical callers
        # must explicitly request the compatibility engine.
        engine=body.get("engine","esgagent-v1")
        if engine not in ("legacy","esgagent-v1"):raise ContractError("处理引擎无效。")
        if engine=="esgagent-v1":
            year=body.get("year",source.get("report_year",2025))
            if type(year) is not int or not 2000<=year<=2100:raise ContractError("请填写正确的报告财年。")
            if source.get("report_year") and year!=source["report_year"]:raise ContractError("输入财年与已登记PDF缓存不一致，请核对报告年度。")
            code=body.get("company_code") or source.get("company_code")
            if code is not None and (not isinstance(code,str) or not re.fullmatch(r"\d{5}",code)):
                raise ContractError("股票代码须为5位数字。")
            from esg_pipeline.esgagent_core import resolve_company
            try:code=resolve_company(source,code)["hkex_code"].split(".")[0]
            except ValueError:raise ContractError("请核对股票代码；未登记的公司须先补充主数据。") from None
            body={**body,"year":year,"company_code":code}
        return body,source,engine

    def create_job(self,body,source,engine,status="RUNNING",batch_id=None):
        identity="job_"+uuid.uuid4().hex
        job={"job_id":identity,"kind":"esgagent" if engine=="esgagent-v1" else "legacy",
             "status":status,"stage":"QUEUED","name":source["name"],
             "created_at":datetime.now(timezone.utc).isoformat(),"options":body,
             "message":"等待队列处理。" if status=="QUEUED" else "任务已创建，正在准备提取。",
             "cancel_requested":False,"result_run_id":None,"source":source,"stages":[],"page":0,"pages":source["pages"]}
        if batch_id:job["batch_id"]=batch_id
        save(self.directory/(identity+".json"),job)
        return identity

    def start(self,body):
        body,source,engine=self.prepare(body)
        with self.app.lock:
            if self.app.active and not self.app.active.done():
                raise Conflict("已有任务运行中，请等待结束或先停止后续步骤。")
            identity=self.create_job(body,source,engine)
            self.owned_job = identity
            self.app.active = self.app.executor.submit(self._execute, identity)
        return identity

    def job(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"job_[a-f0-9]{32}", identity):
            raise NotFound("未找到任务。")
        path = self.directory / (identity + ".json")
        if not path.is_file():
            raise NotFound("未找到任务。")
        return load(path)

    def start_collection(self, body):
        fields={"run_id","a_calls","b_calls","vision","allow_external"}
        if not isinstance(body,dict) or set(body)!=fields:
            raise ContractError("年度模型请求参数不完整。")
        if body["allow_external"] is not True or type(body["vision"]) is not bool:
            raise ContractError("请明确授权所选报告的证据及可选原页发送至阿里模型。")
        if not isinstance(body["run_id"],str) or len(body["run_id"])>100:
            raise ContractError("报告运行ID无效。")
        if any(type(body[k]) is not int or not 1<=body[k]<=8 for k in ("a_calls","b_calls")):
            raise ContractError("年度A、B调用上限分别为1至8次。")
        if not self.capabilities()["available"] or not self.capabilities()["api_key_configured"]:
            raise ContractError("请检查全流程依赖与本机阿里密钥配置。")
        summary=self.summary_for_run(body["run_id"])
        if not summary:raise NotFound("请先选择已有提取/A/B结果的报告。")
        if summary.get("engine")=="esgagent-v1":raise ContractError("新版EsgAgent已完成归类与填表；无需运行旧版二次筛选。")
        with self.app.lock:
            if self.app.active and not self.app.active.done():raise Conflict("已有任务运行中，请等待或停止后再试。")
            identity="job_"+uuid.uuid4().hex
            job={"job_id":identity,"kind":"collection","status":"RUNNING","stage":"QUEUED",
                 "name":summary["source_pdf"]+" · 年度LLM填表","created_at":datetime.now(timezone.utc).isoformat(),
                 "options":{**body,"mode":"model"},"message":"正在复用已有提取结果。","cancel_requested":False,
                 "result_run_id":None,"stages":[],"page":0,"pages":0,"base_summary":summary}
            save(self.directory/(identity+".json"),job)
            self.owned_job=identity
            self.app.active=self.app.executor.submit(self._execute,identity)
        return identity

    def _run_collection(self, identity):
        sys.path.insert(0,str(ROOT/"src"))
        from esg_pipeline.model_collection import run_model_collection
        job=self.job(identity); options=job["options"]
        def check():
            if self.job(identity)["cancel_requested"]:raise Stopped()
        def progress(stage,detail):
            check()
            current=self.job(identity)
            self.update(identity,stage=stage,stages=current["stages"]+[stage],
                        message=detail.get("message","处理中"),pipeline_directory=detail["pipeline_directory"])
        try:
            result=run_model_collection(job["base_summary"],key_file=self.root/"阿里api key",
                a_calls=options["a_calls"],b_calls=options["b_calls"],vision=options["vision"],
                progress=progress,cancel_check=check,pdf_lock=PDF_LOCK)
            self.update(identity,status="PARTIAL",stage="FINISHED",summary=job["base_summary"],
                result_run_id=options["run_id"],pipeline_directory=result["directory"],
                message="年度LLM筛选与B原页核对已执行，可查看填表状态并导出；研究值不自动用于评分。")
        except Stopped:
            self.update(identity,status="CANCELLED",message="已停止后续年度调用，已有响应和用量保留。")
        except Exception:
            self.update(identity,status="FAILED",message="年度模型流程未完成，旧结果仍保留；请检查预算与API状态。")

    def update(self, identity, **values):
        with self.lock:
            job = self.job(identity)
            job.update(values)
            save(self.directory / (identity + ".json"), job)
            return job

    def cancel(self, identity):
        with self.lock:
            job = self.job(identity)
            if job["status"] in TERMINAL:
                raise Conflict("任务已结束。")
            self.update(identity, cancel_requested=True, message="正在停止后续步骤；当前请求结束后生效。")
        return self.public_job(identity)

    def public_job(self, identity):
        job = self.job(identity)
        result = {k: deepcopy(job[k]) for k in ("job_id", "status", "stage", "name", "created_at", "message", "cancel_requested", "result_run_id", "stages", "page", "pages")}
        result["mode"] = job["options"]["mode"]
        result["options"] = deepcopy(job["options"])
        result["reused_extraction"] = job.get("reused_extraction", False)
        result["usage"] = self._usage(job)
        result["error_code"] = job.get("error_code")
        return result

    def jobs(self):
        return [self.public_job(p.stem) for p in sorted(self.directory.glob("job_*.json"), key=lambda p:p.stat().st_mtime, reverse=True)[:20]]

    def usage_records(self, job):
        attempts = {}
        paths = [self.directory / (job["job_id"] + ".vision.jsonl")]
        if job.get("pipeline_directory"):
            paths.append(Path(job["pipeline_directory"]) / "model_usage.jsonl")
            paths.append(Path(job["pipeline_directory"]) / "upstream/model_usage.jsonl")
        for path in paths:
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                        if not isinstance(row,dict):continue
                        attempts[row.get("call_id") or row["attempt_id"]] = row
                    except (ValueError, KeyError):
                        continue
        return list(attempts.values())

    def _usage(self,job):
        records=self.usage_records(job)
        result = {"calls":len(records), "input_tokens":0, "output_tokens":0, "total_tokens":0, "unknown_requests":0}
        for row in records:
            usage = row.get("usage") if isinstance(row.get("usage"),dict) else {}
            if type(usage.get("total_tokens")) is not int:
                result["unknown_requests"] += 1
            for source, target in (("prompt_tokens","input_tokens"),("completion_tokens","output_tokens"),("total_tokens","total_tokens")):
                if type(usage.get(source)) is int:
                    result[target] += usage[source]
        return result

    def _execute(self, identity):
        try:
            if self.job(identity).get("kind")=="collection":
                self._run_collection(identity)
                return
            if self.job(identity).get("kind")=="esgagent":
                self._run_esgagent(identity)
                return
            with PDF_LOCK:
                self._run(identity)
        except Exception:
            self.update(identity, status="FAILED", message="流程启动失败，请检查依赖；原文件与运行记录已保留。")

    def _run_esgagent(self,identity):
        sys.path.insert(0,str(ROOT/"src"))
        from esg_pipeline.esgagent_pipeline import run_esgagent_pipeline
        job=self.job(identity);options=job["options"]
        def check():
            if self.job(identity)["cancel_requested"]:raise Stopped()
        def progress(stage,details):
            current=self.job(identity)
            stages=current["stages"] if stage in current["stages"] else current["stages"]+[stage]
            self.update(identity,stage=stage,stages=stages,**details)
        try:
            # Core extraction lives in an isolated process. Projection renders
            # pages only after that worker completes; serialize host PDF access.
            with PDF_LOCK:
                summary=run_esgagent_pipeline(job["source"],self.output,year=options["year"],company_code=options["company_code"],
                    allow_external=options["mode"]=="model",a_calls=options["a_calls"],b_calls=options["b_calls"],
                    database=self.app.store.directory,progress=progress,cancel_check=check,
                    runtime=self.app.store.directory/"esgagent_runtime")
            self.update(identity,status="PARTIAL",stage="FINISHED",summary=summary,result_run_id=summary["workbench_run_id"],
                reused_extraction=summary["reused_extraction"],message="新版EsgAgent提取、归类、B审查及年度草稿已完成；可直接预览或导出。")
        except Stopped:self.update(identity,status="CANCELLED",message="后续调用已停止，当前响应和缓存保留。")
        except Exception as exc:
            messages={"KEY_FILE_MUST_CONTAIN_ONE_KEY":"API密钥文件未能识别出唯一密钥，请检查根目录的“阿里api key”文件；尚未发送模型请求。",
                      "MODEL_API_ERROR":"阿里模型接口请求失败，请查看本任务 upstream/provider_error.json 中的状态码及脱敏原因；不要反复重试。",
                      "MODEL_AUTH_ERROR":"阿里接口拒绝鉴权或访问权限，请检查密钥、地域及模型权限。",
                      "MODEL_CONNECTION_ERROR":"无法连接阿里模型服务，请检查网络、代理和防火墙；请求未收到有效响应。",
                      "MODEL_REQUEST_INVALID":"阿里接口返回HTTP 400，请查看本任务 upstream/provider_error.json 中的脱敏原因；请勿反复重试。",
                      "MODEL_TIMEOUT":"阿里模型响应超时，已保留任务记录及已返回用量。",
                      "MODEL_RATE_LIMIT":"阿里模型触发限流，请稍后重试。",
                      "MODEL_BAD_JSON":"模型返回内容为空或JSON不合格，已保留现有响应。",
                      "PDF_LIMIT_EXCEEDED":"PDF单页仍超过上传限制，请使用对象存储签名URL方式。",
                      "ESGAGENT_CACHE_MISS_REQUIRES_MODEL":"没有匹配的新版缓存，请选择模型模式并授权；不会回退旧提取器。",
                      "ESGAGENT_INLINE_PDF_TOO_LARGE":"PDF过大，不能直接内联上传；请先用新版EsgAgent的OSS签名URL入口提取，再导入缓存。",
                      "ESGAGENT_CALL_BUDGET_EXHAUSTED":"新版提取调用预算不足，已保留已有响应。"}
            code=str(exc)
            safe_code=code if re.fullmatch(r"[A-Z0-9_]+",code) else "ESGAGENT_PIPELINE_FAILED"
            self.update(identity,status="FAILED",error_code=safe_code,message=messages.get(code,"新版流程未完成，原件及已有缓存保留；错误代码："+safe_code+"。"))

    def cancel_owned(self):
        self.batches.cancel_owned()
        if self.owned_job and self.job(self.owned_job)["status"] == "RUNNING":
            self.cancel(self.owned_job)

    def _run(self, identity):
        # Lazy imports preserve the original dependency-free demo/review mode.
        sys.path.insert(0, str(ROOT / "src"))
        from esg_extraction.extractor import extract_pdf, file_hash
        from esg_extraction.vision import AliVisionClient
        from esg_pipeline.runner import run_pipeline
        job = self.job(identity)
        source, options = job["source"], job["options"]
        def check():
            if self.job(identity)["cancel_requested"]:
                raise Stopped()
        def advance(stage, details=None):
            check()
            with self.lock:
                current = self.job(identity)
                stages = current["stages"]
                if stage not in stages:
                    stages.append(stage)
                self.update(identity, stage=stage, stages=stages, **(details or {}))
        try:
            advance("EXTRACT", {"message":"正在读取报告，优先复用相同PDF的提取结果。"})
            if file_hash(source["_source"]) != source["_sha256"]:
                raise ValueError("SOURCE_CHANGED")
            extraction = source.get("_extraction")
            if extraction:
                self.update(identity, reused_extraction=True, page=source["pages"], message="已复用提取结果，无新增视觉调用。")
            else:
                client = None
                if options["vision"]:
                    client = AliVisionClient(key_file=self.root / "阿里api key", max_calls=options["vision_calls"],
                                             ledger_path=self.directory / (identity + ".vision.jsonl"))
                    original_call = client.__class__.__call__
                    class CancellableVision:
                        def __getattr__(self, key): return getattr(client, key)
                        def __call__(self, image):
                            check()
                            return original_call(client, image)
                    client_for_extraction = CancellableVision()
                else:
                    client_for_extraction = None
                def page_progress(page, total, coverage):
                    check()
                    self.update(identity, page=page, message=f"已读取第{page}页，共{source['pages']}页。")
                report = extract_pdf(source["_source"], self.output / "extracted", client=client_for_extraction, progress=page_progress)
                extraction = report["output_directory"]
            advance("FILTER", {"message":"正在运行A过滤与归类。"})
            result = run_pipeline(extraction, self.output, recovered=source.get("_recovered"), pdf=source["_source"],
                                  mode=options["mode"], key_file=self.root / "阿里api key" if options["mode"] == "model" else None,
                                  a_calls=options["a_calls"], b_calls=options["b_calls"], database=self.app.store.directory,
                                  progress=advance, cancel_check=check, display_name=source["name"])
            # Persist the result before publishing the terminal job state.
            self.update(identity, summary=result, result_run_id=result["workbench_run_id"], status="PARTIAL", stage="FINISHED",
                        message="提取、A归类和B审查已执行，结果已进入下方复核区；尚需独立原页核对。")
        except Stopped:
            self.update(identity, status="CANCELLED", message="已停止后续步骤；已有缓存和中间结果保留，未自动重试。")
        except Exception:
            self.update(identity, status="FAILED", message="流程未完成，原PDF与已有缓存已保留。请检查输入、依赖和调用预算后重试。")

    def summary_for_run(self, run_id):
        # Access requires an existing workbench run, not an arbitrary path.
        self.app.store.detail(run_id)
        for path in self.directory.glob("job_*.json"):
            job = load(path)
            if job.get("result_run_id") == run_id and job.get("summary"):
                return job["summary"]
        for latest in (self.root/"data/esgagent_pipeline/latest_run.json",self.root/"data/esg_pipeline/latest_run.json"):
            if latest.is_file():
                found=next((s for s in load(latest)["reports"] if s.get("workbench_run_id")==run_id),None)
                if found:return found
        return None

    def results(self, run_id, tab="extract", offset=0, limit=25):
        if tab not in ("extract", "classify") or not 0 <= offset <= 100000 or not 1 <= limit <= 100:
            raise ContractError("分页参数无效。")
        summary = self.summary_for_run(run_id)
        if summary is None:
            return {"available":False}
        a = Path(summary["output_directory"]) / "a"
        data = lines(a / ("quantitative_candidates.jsonl" if tab == "extract" else "records.jsonl"))
        rows = []
        for row in data[offset:offset+limit]:
            if tab == "extract":
                rows.append({"id":row["candidate_id"], "label":row.get("metric_label") or row.get("metric_label_raw"),
                             "value":row.get("value_text"), "unit":row.get("unit"), "year":row.get("year"),
                             "page":row["page_no"], "status":row.get("review_status"), "source":row["source_kind"]})
            else:
                rows.append({"id":row["record_id"], "label":row.get("metric_label_raw"), "value":row.get("raw_value"),
                             "unit":row.get("raw_unit"), "year":row.get("year"), "page":row["source_page"],
                             "upstream_status":row.get("upstream",{}).get("classification_status"), "metric":row.get("upstream_metric_id"),
                             "status":row["classification_status"], "field":row.get("profile_field"), "reason":row.get("classification_reason")})
        return {"available":True, "engine":summary.get("engine","legacy"), "name":summary["source_pdf"], "tab":tab, "total":len(data), "offset":offset,
                "rows":rows, "candidate_count":summary["candidate_count"], "record_count":summary["record_count"],
                "classification_states":summary["classification_states"], "b_review_states":summary["b_review_states"],
                "b_findings":summary["b_findings"], "mode":summary["mode"], "page_coverage":summary.get("page_coverage",{}),
                "usage":summary["usage"], "production_ready":False}

    def page_image(self, run_id, page):
        summary = self.summary_for_run(run_id)
        if not summary or type(page) is not int or not 1 <= page <= 2000:
            raise NotFound("未找到对应原页截图。")
        for ev in lines(Path(summary["output_directory"]) / "a/evidence.jsonl"):
            if ev["page_no"] == page:
                original = Path(ev["source_ref"]["asset_path"])
                image = original.parent / f"page_{page:05d}.png"
                if image.is_file():
                    return image.read_bytes()
        raise NotFound("本页没有截图，请查看附加PDF。")

    def annual(self, run_id=None, *, all_companies=False):
        sys.path.insert(0, str(ROOT / "src"))
        from esg_pipeline.annual import build_collection
        if run_id:
            summary = self.summary_for_run(run_id)
            if not summary:raise NotFound("该记录没有完整流程产物。")
            summaries = [summary]
        else:
            latest = self.root / "data/esgagent_pipeline/latest_run.json"
            if not latest.is_file():latest=self.root/"data/esg_pipeline/latest_run.json"
            summaries = load(latest)["reports"] if latest.is_file() else []
            for path in self.directory.glob("job_*.json"):
                job=load(path)
                if job.get("summary") and job.get("result_run_id") and (not (self.root/"data/esgagent_pipeline/latest_run.json").is_file() or job["summary"].get("engine")=="esgagent-v1"):
                    summaries.append(job["summary"])
            summaries=list({s["run_id"]:s for s in summaries}.values())
        return self.collection(summaries,all_companies=all_companies)

    def collection(self,summaries,*,all_companies=False):
        sys.path.insert(0,str(ROOT/"src"))
        from esg_pipeline.annual import build_collection
        if not summaries:raise NotFound("尚无已完成A/B的报告。")
        def review_detail(identity):
            try:return self.app.store.review_for_export(identity)
            except NotFound:return {}
        with PDF_LOCK:
            return build_collection(summaries,all_companies=all_companies,root=self.root,review_resolver=review_detail)

    def annual_export(self, run_id=None, *, all_companies=False):
        sys.path.insert(0, str(ROOT / "src"))
        from esg_pipeline.annual import persist
        from esg_pipeline.workbook_export import export_workbook
        collection=self.annual(run_id,all_companies=all_companies)
        folder=self.app.store.directory / "water_collection"
        persist(collection,folder)
        output=folder/(collection["batch_id"]+".xlsx")
        export_workbook(collection,output)
        return output.read_bytes()

    def export(self, run_id):
        summary = self.summary_for_run(run_id)
        if not summary:
            raise NotFound("该记录没有完整流程产物。")
        directory = Path(summary["output_directory"])
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in (directory / "a").iterdir():
                if path.is_file() and path.suffix in (".json", ".jsonl"):
                    archive.write(path, "a/" + path.name)
            for name in ("run_summary.json", "validation_report.json", "manifest.json", "model_usage.jsonl"):
                if (directory / name).is_file():
                    archive.write(directory / name, name)
            archive.writestr("review_workbench.zip", self.app.export(run_id))
            archive.writestr("annual_collection.json", json.dumps(self.annual(run_id),ensure_ascii=False))
            for name in ("extraction.json","report.json","stage1.csv","candidates.csv","derived.csv","worker_result.json","policy_manifest.json"):
                path=directory/"upstream"/name
                if path.is_file():archive.write(path,"esgagent/"+name)
            pointer=directory/"model_collection/latest.json"
            if pointer.is_file():
                path=Path(load(pointer)["path"]).resolve()
                if path.is_relative_to((directory/"model_collection").resolve()):
                    for item in path.parent.iterdir():
                        if item.is_file() and item.suffix in (".json",".jsonl"):
                            archive.write(item,"model_collection/"+item.name)
        return stream.getvalue()
