"""Persistent sequential batches using the same single-report execution boundary."""
from datetime import datetime, timezone
from decimal import Decimal
import math
from pathlib import Path
import re
import threading
import uuid

from .contracts import ContractError
from .store import Conflict, NotFound

PRICE_KEYS = ("extract_input", "extract_output", "audit_input", "audit_output")
FINISHED = {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELLED", "INTERRUPTED", "FAILED"}


class BatchQueue:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.directory = pipeline.app.store.directory / "pipeline_batches"
        self.lock = threading.RLock()
        self.export_lock = threading.Lock()
        self.owned = None

    def read(self, identity):
        from .pipeline import load
        if not isinstance(identity,str) or not re.fullmatch(r"batch_[a-f0-9]{32}", identity):
            raise NotFound("批量队列不存在。")
        path=self.directory/(identity+".json")
        if not path.is_file():raise NotFound("批量队列不存在。")
        return load(path)

    def write(self, batch):
        from .pipeline import save
        save(self.directory/(batch["batch_id"]+".json"),batch)

    def recover(self):
        from .pipeline import load
        for path in self.directory.glob("batch_*.json"):
            batch=load(path)
            if batch["status"] not in FINISHED:
                batch.update(status="INTERRUPTED",message="工作台上次退出，队列已中断；已完成结果保留，未完成项目不会自动付费重跑。",current_job_id=None)
                self.write(batch)

    def start(self,body):
        if not isinstance(body,dict) or set(body)!={"items","options","pricing","price_note"}:
            raise ContractError("批量请求参数不完整。")
        items,options=body["items"],body["options"]
        if not isinstance(items,list) or not 1<=len(items)<=100:
            raise ContractError("每批请选择1至100份报告。")
        fields={"mode","allow_external","a_calls","b_calls"}
        if not isinstance(options,dict) or set(options)!=fields:
            raise ContractError("请设置批量处理方式及每份调用上限。")
        if options["mode"] not in ("rules_only","model") or type(options["allow_external"]) is not bool:
            raise ContractError("批量处理方式无效。")
        if any(type(options[k]) is not int or not 0<=options[k]<=20 for k in ("a_calls","b_calls")):
            raise ContractError("每份报告A、B调用上限须为0至20。")
        if options["mode"]=="model" and (not options["allow_external"] or not options["a_calls"]+options["b_calls"]):
            raise ContractError("请明确授权本批全部报告外发，并设置正数调用上限。")
        if options["mode"]=="rules_only" and (options["allow_external"] or options["a_calls"] or options["b_calls"]):
            raise ContractError("缓存模式不外发、不调用API，请将调用上限设为0。")
        pricing=body["pricing"]
        if not isinstance(pricing,dict) or set(pricing)!=set(PRICE_KEYS):
            raise ContractError("费用单价格式无效。")
        for value in pricing.values():
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100000):
                raise ContractError("单价须为每百万token的人民币金额，留空表示未知。")
        if not isinstance(body["price_note"],str) or len(body["price_note"])>300:
            raise ContractError("价格说明最长300字。")
        for item in items:
            if not isinstance(item,dict) or not {"source_id","year","company_code"}.issubset(item) or set(item)-{"source_id","year","company_code","name"}:
                raise ContractError("每份报告须提供来源、公司代码和财年。")
            if "name" in item and (not isinstance(item["name"],str) or not 1<=len(item["name"])<=160):
                raise ContractError("报告显示名称须为1至160字。")
        identity="batch_"+uuid.uuid4().hex
        batch={"batch_id":identity,"created_at":datetime.now(timezone.utc).isoformat(),"status":"RUNNING",
               "message":"队列已创建，将按顺序处理。","cancel_requested":False,"current_job_id":None,
               "options":options,"pricing":pricing,"price_note":body["price_note"],"items":[]}
        with self.pipeline.app.lock:
            if self.pipeline.app.active and not self.pipeline.app.active.done():
                raise Conflict("已有任务或队列运行中，请等待完成或停止。")
            seen=set()
            for item in items:
                entry={"request":item,"job_id":None,"name":item["source_id"],"status":"FAILED","message":"来源或参数无效。"}
                try:
                    request={**options,**{k:v for k,v in item.items() if k!="name"},"engine":"esgagent-v1","vision":False,"vision_calls":0}
                    request,source,engine=self.pipeline.prepare(request)
                    if item.get("name"):source={**source,"name":item["name"]}
                    entry["name"]=source["name"]
                    key=(source["_sha256"],request["year"],request.get("company_code"))
                    if key in seen:
                        entry.update(status="SKIPPED",message="同一PDF、公司及财年重复，已跳过，避免重复调用。")
                    else:
                        seen.add(key)
                        entry.update(job_id=self.pipeline.create_job(request,source,engine,status="QUEUED",batch_id=identity),status="QUEUED",message="等待处理。")
                except (ContractError,NotFound) as exc:
                    entry["message"]=str(exc)
                except Exception as exc:
                    entry.update(error_code="BATCH_PREFLIGHT_FAILED",message="报告预检失败（"+type(exc).__name__+"）；该项已记录，继续处理其他报告。")
                batch["items"].append(entry)
            self.write(batch)
            self.owned=identity
            self.pipeline.app.active=self.pipeline.app.executor.submit(self.execute,identity)
        return identity

    def execute(self,identity):
        try:
            for index in range(len(self.read(identity)["items"])):
                with self.lock:
                    batch=self.read(identity)
                    if batch["cancel_requested"]:break
                    child=batch["items"][index]["job_id"]
                    if not child:continue
                    batch.update(current_job_id=child,message=f"正在处理第{index+1}/{len(batch['items'])}份报告。")
                    self.pipeline.update(child,status="RUNNING",message="正在准备提取。")
                    self.write(batch)
                # Do not submit onto the occupied single-worker executor.
                self.pipeline._execute(child)
            with self.lock:
                batch=self.read(identity)
                batch.update(current_job_id=None,message="正在汇总本批已完成报告的年度草稿。")
                self.write(batch)
            summaries=self.summaries(identity)
            if summaries:
                from .pipeline import save
                collection=self.pipeline.collection(summaries)
                save(self.directory/identity/"annual_collection.json",collection)
            with self.lock:
                batch=self.read(identity)
                failures=any(x["status"] not in ("PARTIAL","SKIPPED") for x in self.public(identity)["items"])
                batch.update(status="CANCELLED" if batch["cancel_requested"] else "COMPLETED_WITH_ERRORS" if failures else "COMPLETED",
                    message="队列已停止，已完成结果保留。" if batch["cancel_requested"] else "队列处理结束，可导出本批已完成报告的年度草稿；B待复核状态保留。")
                self.write(batch)
        except Exception:
            with self.lock:
                batch=self.read(identity)
                batch.update(status="FAILED",current_job_id=None,message="队列调度或年度汇总失败；已完成结果与逐份任务保留。")
                self.write(batch)

    def cost(self,job,pricing):
        known=Decimal(0);unknown=0
        for record in self.pipeline.usage_records(job):
            usage=record.get("usage") if isinstance(record.get("usage"),dict) else {}
            kind="extract" if record.get("stage")=="esgagent_extract" else "audit"
            rates=(pricing[kind+"_input"],pricing[kind+"_output"])
            tokens=(usage.get("prompt_tokens"),usage.get("completion_tokens"))
            if any(rate is None for rate in rates) or any(type(n) is not int for n in tokens):
                unknown+=1
                continue
            known+=sum(Decimal(str(rate))*n/Decimal(1000000) for rate,n in zip(rates,tokens))
        return {"known_estimate_cny":float(known),"unknown_cost_requests":unknown,"is_estimate":True}

    def public(self,identity):
        batch=self.read(identity)
        result={k:v for k,v in batch.items() if k!="items"}
        result["items"]=[]
        totals={"calls":0,"input_tokens":0,"output_tokens":0,"total_tokens":0,"unknown_requests":0}
        estimate=0;unknown=0
        for entry in batch["items"]:
            row=dict(entry)
            if entry["job_id"]:
                job=self.pipeline.public_job(entry["job_id"])
                row.update(job)
                cost=self.cost(self.pipeline.job(entry["job_id"]),batch["pricing"])
                row["cost"]=cost
                estimate+=cost["known_estimate_cny"];unknown+=cost["unknown_cost_requests"]
                for k in totals:totals[k]+=job["usage"][k]
            result["items"].append(row)
        result["usage"]=totals
        result["cost"]={"known_estimate_cny":estimate,"unknown_cost_requests":unknown,"is_estimate":True}
        result["counts"]={status:sum(x["status"]==status for x in result["items"]) for status in ("QUEUED","RUNNING","PARTIAL","FAILED","CANCELLED","INTERRUPTED","SKIPPED")}
        result["export_available"]=any(x.get("result_run_id") for x in result["items"])
        return result

    def list(self):
        return [self.public(p.stem) for p in sorted(self.directory.glob("batch_*.json"),key=lambda p:p.stat().st_mtime,reverse=True)[:20]]

    def cancel(self,identity):
        from .pipeline import TERMINAL
        with self.lock:
            batch=self.read(identity)
            if batch["status"] in FINISHED:raise Conflict("队列已结束。")
            batch.update(cancel_requested=True,message="正在停止；当前API请求结束后生效，其余报告不再调用。")
            self.write(batch)
            for entry in batch["items"]:
                child=entry["job_id"]
                if not child:continue
                job=self.pipeline.job(child)
                if job["status"]=="QUEUED":self.pipeline.update(child,status="CANCELLED",cancel_requested=True,message="队列停止，未开始此报告。")
                elif job["status"] not in TERMINAL:self.pipeline.cancel(child)
        return self.public(identity)

    def cancel_owned(self):
        if self.owned and self.read(self.owned)["status"] not in FINISHED:self.cancel(self.owned)

    def summaries(self,identity):
        return [self.pipeline.job(x["job_id"])["summary"] for x in self.read(identity)["items"]
                if x["job_id"] and self.pipeline.job(x["job_id"]).get("summary") and self.pipeline.job(x["job_id"]).get("result_run_id")]

    def export(self,identity):
        batch=self.read(identity)
        if batch["status"] not in FINISHED:raise Conflict("请等待队列结束后统一导出。")
        with self.export_lock:
            collection=self.pipeline.collection(self.summaries(identity))
            from esg_pipeline.workbook_export import export_workbook
            output=self.directory/identity/"water-2025.xlsx"
            export_workbook(collection,output)
            return output.read_bytes()
