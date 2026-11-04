"""Boundary to the delivered EsgAgent. Only the worker imports its `app` package.

The source package stays in one place; no copies of its extraction/selection
logic are maintained in the workbench. Historical legacy runners remain readers.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from esg_extraction.extractor import atomic_json, file_hash
from .company_master import load_master

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "EsgAgent/EsgAgent"
ENGINE = "esgagent-v1"
DRAFT_POLICY={"version":"ESGAGENT-DRAFT-1.0","official_publication":False,"notes":[
    ["当前草稿","本文件为新版EsgAgent研究草稿，含待B确认的候选，不是正式事实或评分结果。"],
    ["推定取水","DEFINITION_INFERRED表示按新版约定将未单列耗水的普通用水量推定为取水，非报告明示定义。"],
    ["循环率推算","DERIVED_DRAFT使用回用量/(取水量+回用量)×100，非报告直接披露；公式及输入在台账中保留。"],
    ["近似与币种","水按1吨≈1m³换算时保留近似标志；按中文金额单位推定CNY时保留币种推定标志。"],
    ["B与人工","原B状态不因填表改为通过；人工修订或排除会暂停相关值及其依赖推算，待重审。"],
    ["原规则保留","上方为原模板的字段与正式口径参考。本研究草稿采用本说明的推定/推算约定；正式发布仍须独立审核和口径确认。"]]}
METRICS = {"revenue":"revenue_million", "water_withdrawal":"water_withdrawal_m3",
    "water_consumption":"water_consumption_m3", "water_recycling_rate":"water_recycling_rate_pct",
    "water_recycled_volume":"water_recycled_reused_m3", "water_discharge":"water_discharge_m3",
    "cod_emission":"cod_discharge_t", "ammonia_nitrogen_emission":"ammonia_n_discharge_t",
    "total_nitrogen_emission":"total_n_discharge_t", "total_phosphorus_emission":"total_p_discharge_t"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def delivered_reports(core=CORE):
    """Relocate the Mac delivery using hashes, never its absolute object paths."""
    core = Path(core)
    companies = {c["company_id"]:c for c in load(core/"config/companies.json")["companies"]}
    result = []
    for path in sorted((core/"data/reports").glob("*.json")):
        report = load(path)
        company = companies.get(report["company_id"])
        source = core/"data/objects/pdf"/(report["sha256"]+".pdf")
        batch = core/"data/batches"/(report["report_id"]+".json")
        if not company or not source.is_file() or not batch.is_file():
            continue
        result.append({"id":"report_"+report["sha256"],"name":company["name"]+"_"+str(report["report_year"])+".pdf",
            "pages":report["page_count"],"company_code":company["hkex_code"].split(".")[0],
            "report_year":report["report_year"],"engine":ENGINE,"has_esgagent_cache":True,
            "_source":str(source.resolve()),"_sha256":report["sha256"],"_batch":str(batch.resolve()),
            "_report":str(path.resolve()),"_company":company})
    return result


def resolve_company(source, code=None):
    known = next((r for r in delivered_reports() if r["_sha256"]==source["_sha256"]),None)
    if known:
        actual=known["_company"]
        if code and code != actual["hkex_code"].split(".")[0]:
            raise ValueError("COMPANY_CODE_CONFLICT_WITH_SOURCE")
        return actual
    match=re.match(r"^(\d{5})(?:_|\b)",source["name"])
    clue=code or (match[1] if match else None)
    master=load_master()
    canonical=master["alias_to_code"].get(clue)
    if canonical is None:raise ValueError("REGISTER_COMPANY_CODE_FIRST")
    if canonical!=clue:raise ValueError("HISTORICAL_CODE_DATE_REQUIRES_REVIEW")
    company=master["companies"][canonical]
    industry=company["sasb_industry"]
    slug="fb-ab" if industry=="Alcoholic Beverages" else "fb-nb" if industry=="Non-Alcoholic Beverages" else "general"
    return {"company_id":"hkex_"+canonical,"hkex_code":canonical+".HK","name":company["company_name"],
            "name_en":company["company_name"],"industry_code":slug,"sasb_sector":company["sasb_sector"],
            "sasb_industry":industry,"aliases":[]}


def run_worker(source, directory, *, year, company_code=None, allow_external=False, max_calls=4,
               runtime=None, progress=None, cancel_check=None):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    if type(year) is not int or not 2000<=year<=2100:raise ValueError("REPORT_YEAR_REQUIRED")
    if type(max_calls) is not int or not 0<=max_calls<=20:raise ValueError("INVALID_ESGAGENT_BUDGET")
    company=resolve_company(source,company_code)
    known=next((r for r in delivered_reports() if r["_sha256"]==source["_sha256"]),None)
    if known and year!=known["report_year"]:raise ValueError("REPORT_YEAR_CONFLICT_WITH_SOURCE")
    if file_hash(source["_source"])!=source["_sha256"]:raise ValueError("SOURCE_CHANGED")
    request={"source":source["_source"],"sha256":source["_sha256"],"name":source["name"],"year":year,
             "company":company,"allow_external":allow_external,"max_calls":max_calls,
             "directory":str(directory),"runtime":str(Path(runtime or ROOT/"data/esgagent_runtime").resolve()),
             "core":str(CORE.resolve()),"key_file":str(ROOT/"阿里api key")}
    atomic_json(directory/"worker_request.json",request)
    flags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0
    process=subprocess.Popen([sys.executable,str(ROOT/"scripts/esgagent_worker.py"),str(directory/"worker_request.json")],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
    seen=0;cancelled=None
    try:
        while process.poll() is None:
            if cancel_check and cancelled is None:
                try:cancel_check()
                except Exception as exc:
                    cancelled=exc;(directory/"cancel.request").touch()
            events=directory/"worker_events.jsonl"
            if events.is_file():
                rows=events.read_text(encoding="utf-8").splitlines()
                for raw in rows[seen:]:
                    try:event=json.loads(raw)
                    except ValueError:continue
                    if progress and cancelled is None:progress(event["stage"],event.get("details",{}))
                seen=len(rows)
            time.sleep(0.15)
        if cancelled is not None:raise cancelled
        result=directory/"worker_result.json"
        if process.returncode or not result.is_file():
            error=load(directory/"worker_error.json") if (directory/"worker_error.json").is_file() else {}
            raise ValueError(error.get("code","ESGAGENT_WORKER_FAILED"))
        response=load(result)
        if response["report"]["sha256"]!=source["_sha256"] or file_hash(source["_source"])!=source["_sha256"]:
            raise ValueError("SOURCE_CHANGED")
        return response
    finally:
        if process.poll() is None:
            (directory/"cancel.request").touch()
            process.wait()
