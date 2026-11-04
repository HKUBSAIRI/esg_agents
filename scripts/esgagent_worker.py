"""Private JSON/file worker isolating the delivered package's generic `app` name.

It never falls back to the old extractor. Network is disabled unless explicitly
authorized; provider-level retries are off and all physical calls share a budget.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
import uuid


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,default=str,allow_nan=False),encoding="utf-8")
    temp.replace(path)


def execute(request):
    core=Path(request["core"]);directory=Path(request["directory"]);runtime=Path(request["runtime"])
    sys.path.insert(0,str(core))
    from app.core.settings import Settings
    from app.models.contracts import CompanyProfile,ReportRef,ExtractionBatch
    from app.services.ingest import IngestService
    from app.services.skill_loader import SkillLoader
    from app.services.prompt_registry import PromptRegistry
    from app.services.export import build_stage1_row,build_candidate_rows,write_stage1_csv,write_candidate_csv,build_derived_rows,write_derived_csv
    from app.adapters.object_store import LocalObjectStore
    from app.adapters.qwen import QwenPDFClient
    from app.agents.extraction import ExtractionAgent
    from app.core.logging import RunLogger
    from app.services.normalizer import normalize
    def event(stage,**details):
        with (directory/"worker_events.jsonl").open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"stage":stage,"details":details},ensure_ascii=False)+"\n")
    def check():
        if (directory/"cancel.request").is_file():raise RuntimeError("ESGAGENT_CANCELLED")
    source=Path(request["source"]);raw=source.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=request["sha256"]:raise ValueError("SOURCE_CHANGED")
    company=CompanyProfile.model_validate(request["company"])
    policy_files=[*sorted((core/"skills").rglob("*.md")),*sorted((core/"prompts").rglob("*.j2")),core/"app/agents/extraction.py"]
    policy_hashes={str(p.relative_to(core)):hashlib.sha256(p.read_bytes()).hexdigest() for p in policy_files}
    policy_digest=hashlib.sha256(json.dumps(policy_hashes,sort_keys=True).encode()).hexdigest()
    write(directory/"policy_manifest.json",{"extraction_policy_digest":policy_digest,"files":policy_hashes,
        "export_sha256":hashlib.sha256((core/"app/services/export.py").read_bytes()).hexdigest(),
        "normalizer_sha256":hashlib.sha256((core/"app/services/normalizer.py").read_bytes()).hexdigest()})
    key="cache-only-no-network"
    if request["allow_external"]:
        contents=Path(request["key_file"]).read_text(encoding="utf-8-sig")
        keys=set(re.findall(r"sk-[A-Za-z0-9_-]{12,}",contents))
        if not keys:
            # Match the existing B client: supplied tokens need not start with sk-.
            # Keep ambiguity fatal; never pick arbitrarily from multiple tokens.
            keys=set(re.findall(r"[A-Za-z0-9_.+-]{24,}",contents))
        if len(keys)!=1:raise ValueError("KEY_FILE_MUST_CONTAIN_ONE_KEY")
        key=keys.pop()
    base=Settings.load(core/"config/.env",dashscope_api_key=key,project_root=core,model_max_retries=1,extract_chunk_concurrency=1) if (core/"config/.env").is_file() else Settings(dashscope_api_key=key,model_max_retries=1,extract_chunk_concurrency=1)
    settings=SimpleNamespace(**base.model_dump())
    settings.extract_max_calls=request["max_calls"]
    for name,path in {"objects_dir":runtime/"objects","reports_dir":runtime/"reports","batches_dir":runtime/"batches",
                      "skills_dir":core/"skills","prompts_dir":core/"prompts","log_dir":directory/"logs"}.items():
        setattr(settings,name,path);path.mkdir(parents=True,exist_ok=True)
    store=LocalObjectStore(settings.objects_dir)
    event("EXTRACT",message="新版EsgAgent正在匹配PDF哈希与提取缓存。")
    delivered=None
    for path in (core/"data/reports").glob("*.json"):
        item=json.loads(path.read_text(encoding="utf-8"))
        if item["sha256"]==digest and item["company_id"]==company.company_id and item["report_year"]==request["year"]:
            delivered=ReportRef.model_validate(item);break
    ingest=IngestService(settings,store)
    cached_ref=None
    if not delivered:
        # An earlier failed upload may have a different generated report ID.
        # Prefer a matching completed cache over that incomplete ingest entry.
        for path in settings.reports_dir.glob("*.json"):
            item=json.loads(path.read_text(encoding="utf-8"))
            rid=item["report_id"]
            policy=settings.batches_dir/(rid+".policy.json")
            if (item["sha256"]==digest and item["company_id"]==company.company_id
                    and item["report_year"]==request["year"]
                    and (settings.batches_dir/(rid+".json")).is_file()
                    and policy.is_file() and json.loads(policy.read_text(encoding="utf-8")).get("digest")==policy_digest):
                cached_ref=ReportRef.model_validate(item)
                break
    if delivered or cached_ref:
        uri=store.put_bytes("pdf/"+digest+".pdf",raw)
        report=(delivered or cached_ref).model_copy(update={"object_uri":uri,"source_filename":request["name"]})
    else:
        report=ingest.ingest_pdf(source,company,request["year"])
    skills=SkillLoader(settings.skills_dir);registry=skills.load_registry()
    prompts=PromptRegistry(settings.prompts_dir)
    cached_path=core/"data/batches"/(report.report_id+".json") if delivered else settings.batches_dir/(report.report_id+".json")
    policy_path=settings.batches_dir/(report.report_id+".policy.json")
    policy_matches=delivered is not None or policy_path.is_file() and json.loads(policy_path.read_text(encoding="utf-8")).get("digest")==policy_digest
    from_cache=cached_path.is_file() and policy_matches
    if from_cache:
        batch=ExtractionBatch.model_validate_json(cached_path.read_text(encoding="utf-8"))
        if batch.report_id!=report.report_id or batch.company_id!=company.company_id or batch.report_year!=report.report_year:
            raise ValueError("ESGAGENT_CACHE_IDENTITY_MISMATCH")
    else:
        if not request["allow_external"] or request["max_calls"]<1:raise ValueError("ESGAGENT_CACHE_MISS_REQUIRES_MODEL")
        from openai import OpenAI
        actual=OpenAI(api_key=key,base_url=base.dashscope_base_url,timeout=base.qwen_timeout_s,max_retries=0)
        class Calls:
            def __init__(self):self.count=0;self.lock=threading.Lock()
            def create(self,**kwargs):
                check()
                with self.lock:
                    if self.count>=request["max_calls"]:raise RuntimeError("ESGAGENT_CALL_BUDGET_EXHAUSTED")
                    self.count+=1
                call_id="new_"+uuid.uuid4().hex
                def ledger(phase,usage=None,status="STARTED"):
                    with (directory/"model_usage.jsonl").open("a",encoding="utf-8") as stream:
                        stream.write(json.dumps({"call_id":call_id,"stage":"esgagent_extract","phase":phase,"status":status,"model":kwargs["model"],"usage":usage})+"\n")
                ledger("start")
                usage=None
                try:stream=actual.chat.completions.create(**kwargs)
                except Exception as exc:
                    ledger("end",status="REQUEST_FAILED")
                    body=getattr(exc,"body",None)
                    detail=body.get("error",body) if isinstance(body,dict) else {}
                    message=str(detail.get("message","") if isinstance(detail,dict) else "")
                    message=message.replace(key,"[REDACTED]")
                    message=re.sub(r"[A-Za-z0-9+/=_-]{80,}","[LONG_TOKEN]",message)[:600]
                    write(directory/"provider_error.json",{"status_code":getattr(exc,"status_code",None),
                        "error_type":type(exc).__name__,"message":message})
                    raise
                def consume():
                    nonlocal usage
                    status="INTERRUPTED"
                    announced=False
                    try:
                        for chunk in stream:
                            if not announced and getattr(chunk,"choices",None):
                                event("EXTRACT",message="模型已开始返回本块结果，正在接收并校验；请勿重复提交。")
                                announced=True
                            if getattr(chunk,"usage",None):usage=chunk.usage.model_dump()
                            yield chunk
                        status="RESPONSE_RECEIVED"
                    finally:
                        stream.close();ledger("end",usage,status)
                return consume()
        calls=Calls()
        qwen=QwenPDFClient(settings,RunLogger(settings.log_dir,"qwen"),client=SimpleNamespace(chat=SimpleNamespace(completions=calls)))
        agent=ExtractionAgent(settings,store,skills,prompts,qwen,RunLogger(settings.log_dir,"agent"))
        event("EXTRACT",message="新版模型直接读取PDF，提取并归类；本地不做文本提取或OCR。")
        outcome=agent.extract(report,company,force=not policy_matches,concurrency=1,progress=lambda message:event("EXTRACT",message=message))
        batch=outcome.batch
        write(policy_path,{"digest":policy_digest})
    check()
    event("CLASSIFY",message="正在使用新版指标规则、选择器和单位换算生成15列研究草稿。")
    row=build_stage1_row(report,company,batch,registry)
    write(directory/"extraction.json",batch.model_dump(mode="json"))
    write(directory/"report.json",report.model_dump(mode="json"))
    write_stage1_csv([row],directory/"stage1.csv")
    write_candidate_csv(build_candidate_rows(report,company,batch,registry),directory/"candidates.csv")
    write_derived_csv(build_derived_rows(row),directory/"derived.csv")
    selections={}
    for metric,selection in row.selections.items():
        normalized=selection.normalized
        selections[metric]={"candidate_id":selection.candidate.candidate_id if selection.candidate else None,
            "value":str(normalized.value) if normalized and normalized.value is not None else None,
            "unit":normalized.unit if normalized else None,"currency":selection.currency,
            "notes":selection.notes,"conflicts":selection.conflicts,"derived":selection.derived,
            "formula_id":selection.formula_id,"formula_version":selection.formula_version,"inputs":selection.derivation_inputs}
    normalizations={}
    for candidate in batch.candidates:
        definition=registry.get(candidate.metric_id)
        if definition is not None:
            value=normalize(candidate.raw_value,candidate.unit,definition.value_kind)
            normalizations[candidate.candidate_id]={"value":str(value.value) if value.value is not None else None,
                "unit":value.unit,"factor":str(value.factor) if value.factor is not None else None,
                "currency":value.currency,"method":value.method,"note":value.note}
    write(directory/"worker_result.json",{"engine":"esgagent-v1","from_cache":from_cache,"normalizations":normalizations,"extraction_policy_digest":policy_digest,
        "batch":batch.model_dump(mode="json"),"report":report.model_dump(mode="json"),"company":company.model_dump(mode="json"),
        "stage1":{"values":row.values,"selections":selections,"issues":row.issues},
        "source_pdf":str(source.resolve()),"source_sha256":digest})


if __name__=="__main__":
    request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:execute(request)
    except Exception as exc:
        # Never echo provider exceptions, credentials or document contents.
        native=getattr(getattr(exc,"code",None),"value",None)
        code=native if isinstance(native,str) and re.fullmatch(r"[A-Z0-9_]+",native) else str(exc) if str(exc).isupper() and re.fullmatch(r"[A-Z0-9_]+",str(exc)) else "ESGAGENT_"+type(exc).__name__.upper()
        provider_path=Path(request["directory"])/"provider_error.json"
        if provider_path.is_file() and code=="MODEL_API_ERROR":
            detail=json.loads(provider_path.read_text(encoding="utf-8"))
            status=detail.get("status_code")
            if status in (401,403):code="MODEL_AUTH_ERROR"
            elif detail.get("error_type")=="APIConnectionError":code="MODEL_CONNECTION_ERROR"
            elif status==400:code="MODEL_REQUEST_INVALID"
        write(Path(request["directory"])/"worker_error.json",{"code":code})
        raise SystemExit(1)
