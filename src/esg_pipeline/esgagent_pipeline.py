"""New EsgAgent -> explicit B contract projection -> workbench -> annual draft.

Projection changes names, not extraction/classification conclusions. Original
Pydantic batches, multi-evidence relations, assumptions and formulas are retained.
"""
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import re
import uuid

import fitz

from esg_extraction.extractor import atomic_json,file_hash,output_jsonl
from esg_extraction.numbers import tokens
from esg_semantic.audit.delivery import run_delivery
from .esgagent_core import ENGINE,METRICS,ROOT,run_worker,load
from .llm import JsonClient,usage_summary
from .profiles import load_profile
from .runner import import_review,preview


def project(result,directory):
    """B compatibility view, with stable links to upstream candidate/evidence IDs."""
    directory=Path(directory);a=directory/"a";a.mkdir()
    batch,report=result["batch"],result["report"]
    pid=report["report_id"]
    assets=directory/"assets";assets.mkdir()
    pages={e["source_page"] for e in batch["evidence"]}
    with fitz.open(result["source_pdf"]) as pdf:
        if len(pdf)!=report["page_count"]:raise ValueError("SOURCE_PAGE_COUNT_CHANGED")
        for n in sorted(pages):
            if not 1<=n<=len(pdf):raise ValueError("EVIDENCE_PAGE_OUT_OF_RANGE")
            pdf[n-1].get_pixmap(matrix=fitz.Matrix(1,1)).save(assets/f"page_{n:05d}.png")
    evidence=[]
    for upstream in batch["evidence"]:
        content=upstream["source_evidence"]
        ev={"evidence_id":upstream["evidence_id"],"evidence_version_id":upstream["evidence_id"],"pid":pid,
            "content":content,"content_sha256":hashlib.sha256(content.encode()).hexdigest(),"page_no":upstream["source_page"],
            "source_format":"v2","kind":"table_cell" if upstream["source_type"]=="TABLE" else "figure" if upstream["source_type"] in ("CHART","IMAGE") else "text",
            "bbox":None,"tier":"unverified","numbers":[n["value_decimal"] for n in tokens(content) if n["value_decimal"] is not None],
            "unit_hint":upstream.get("unit_header"),"year_hint":None,
            "source_ref":{"pdf_path":result["source_pdf"],"pdf_sha256":result["source_sha256"],"asset_path":str(assets/f"page_{upstream['source_page']:05d}.png"),
                "independent_binding_verified":False,"evidence_origin":"ESGAGENT_MODEL_TRANSCRIPTION","upstream_evidence_id":upstream["evidence_id"],
                "table_title":upstream.get("table_title"),"row_header":upstream.get("row_header"),"column_header":upstream.get("column_header")},
            "upstream":upstream}
        evidence.append(ev)
    index={e["evidence_id"]:e for e in evidence}
    decisions=[{"evidence_version_id":e["evidence_id"],"status":"ACCEPT","reason_code":"ESGAGENT_RETAINED_EVIDENCE",
                "policy_version":"ESGAGENT_NO_SECOND_FILTER"} for e in evidence]
    records=[];candidates=[];outcomes=[]
    for candidate in batch["candidates"]:
        ev=index[candidate["evidence_ids"][0]]
        field=METRICS.get(candidate.get("metric_id"))
        number=next((n["value_decimal"] for n in tokens(candidate.get("raw_value") or "") if n["value_decimal"] is not None),None)
        normalized=result["normalizations"].get(candidate["candidate_id"],{})
        target="million" if field=="revenue_million" else "m3" if field and field.endswith("_m3") else "%" if field=="water_recycling_rate_pct" else "t" if field and field.endswith("_t") else None
        status="ASSIGNED" if field and candidate["classification_status"]=="CLASSIFIED" else "AMBIGUOUS" if candidate["classification_status"]=="NEEDS_REVIEW" else "UNMAPPED"
        boundary=candidate.get("reporting_boundary",{});year=candidate.get("data_year")
        label=ev["upstream"].get("row_header") or candidate.get("metric_id") or "未归类"
        row={"schema_version":"local-a-1.0","record_id":"rec_eg_"+candidate["candidate_id"],"pid":pid,
            "candidate_id":candidate["candidate_id"],"classification_run_id":batch["batch_id"],"filter_run_id":"eg_retained",
            "profile_id":"water_v1","profile_version":"ESGAGENT-13-METRICS","profile_field":field,
            "evidence_id":ev["evidence_id"],"evidence_version_id":ev["evidence_id"],"evidence_version_ids":candidate["evidence_ids"],
            "source_page":ev["page_no"],"source_kind":ev["kind"],"bbox":None,"filter_status":"ACCEPT",
            "classification_status":status,"classification_reason":candidate["classification_reason"],"metric_label_raw":label,
            "raw_value":candidate.get("raw_value") or candidate.get("raw_text"),"raw_unit":candidate.get("unit"),"value":number,"value_decimal":number,
            "unit":candidate.get("unit"),"normalized_value":normalized.get("value"),"normalized_unit":target,
            "normalization":{"status":"OK" if normalized.get("value") is not None else "NOT_APPLICABLE","from_unit":candidate.get("unit"),
                "to_unit":target,"factor":normalized.get("factor"),"rule_id":"ESGAGENT_NORMALIZER","rule_version":"1.0"},
            "year":year,"year_status":"EXPLICIT_UNVERIFIED" if year else "UNKNOWN","report_year":report["report_year"],
            "scope_type":boundary.get("level","UNKNOWN"),"scope_note":boundary.get("description"),"period_start":None,"period_end":None,
            "value_nature":"REPORTED" if number is not None else "UNCERTAIN","value_qualifier":"UNVERIFIED","record_usable":False,
            "confidence":candidate["confidence"],"confidence_tier":"unverified","provenance_source":"evidence_id",
            "validation_status":"needs_review","validation_notes":["ESGAGENT_MODEL_SOURCE_REQUIRES_B_REVIEW",*candidate.get("ambiguity_flags",[])],
            "native_numeric_support":"UNAVAILABLE","upstream_metric_id":candidate.get("metric_id"),"upstream":candidate}
        row["logical_group_key"]="|".join(str(v) for v in (pid,"water_v1",field or "",year if year is not None else "UNKNOWN","",boundary.get("level") or ""))
        records.append(row)
        candidates.append({"candidate_id":candidate["candidate_id"],"pid":pid,"evidence_version_id":ev["evidence_id"],
            "page_no":ev["page_no"],"metric_label":label,"value_text":candidate.get("raw_value") or candidate.get("raw_text"),"value_decimal":number,
            "unit":candidate.get("unit"),"year":year,"scope":boundary.get("level"),"source_kind":"esgagent_pdf",
            "upstream_metric_id":candidate.get("metric_id"),"upstream_status":candidate["classification_status"]})
        outcomes.append({"candidate_id":candidate["candidate_id"],"evidence_version_id":ev["evidence_id"],"classification_run_id":batch["batch_id"],
                         "status":status,"record_ids":[row["record_id"]],"n_records":1})
    document={"schema_version":"local-a-1.0","pid":pid,"company_name":result["company"]["name"],"report_year":report["report_year"],
              "period_start":None,"period_end":None,"page_count":report["page_count"],"pdf_path":result["source_pdf"],"pdf_sha256":report["sha256"],"source_format":"v2"}
    for name,data in (("evidence",evidence),("quantitative_candidates",candidates),("records",records),("semantic_filter",decisions),("filter_audit_sample",[]),("candidate_outcome",outcomes)):
        output_jsonl(a/(name+".jsonl"),data)
    atomic_json(a/"document_context.json",document)
    atomic_json(a/"coverage.json",{"pid":pid,"key_fields":[],"by_field":{},"unclassified_hints":[],"source":batch.get("coverage"),
                                       "note":"模型声称的覆盖范围，不作为无遗漏证明"})
    atomic_json(a/"profile.json",load_profile())
    return document,records,evidence


def run_esgagent_pipeline(source,output_root,*,year=2025,company_code=None,allow_external=False,a_calls=4,b_calls=0,
                         database=None,progress=None,cancel_check=None,runtime=None):
    run_id="eg_"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex[:8]
    directory=Path(output_root).resolve()/run_id;directory.mkdir(parents=True)
    state={"engine":ENGINE,"run_id":run_id,"status":"RUNNING"}
    def advance(stage,details=None):
        if cancel_check:cancel_check()
        state["stage"]=stage;atomic_json(directory/"task.json",state)
        if progress:progress(stage,{"pipeline_directory":str(directory),**(details or {})})
    try:
        advance("EXTRACT",{"message":"正在使用新版EsgAgent；不会回退旧提取器。"})
        result=run_worker(source,directory/"upstream",year=year,company_code=company_code,allow_external=allow_external,
                          max_calls=a_calls,runtime=runtime,progress=advance,cancel_check=cancel_check)
        advance("CLASSIFY",{"message":"新版提取与归类已完成，正在适配B审查契约。"})
        document,records,evidence=project(result,directory)
        advance("AUDIT",{"message":"正在执行现有独立B审查；原模型证据不自动作为审核通过。"})
        client=JsonClient(directory,key_file=ROOT/"阿里api key",max_calls=b_calls) if allow_external and b_calls>0 else None
        if client and cancel_check:client.before_call=cancel_check
        b_result=run_delivery(directory/"a",directory/"b",mode="model" if client else "rules_only",llm=client.review if client else None,max_model_calls=b_calls)
        if b_result["status"]=="FAILED":raise ValueError("B_AUDIT_FAILED")
        audit=load(Path(b_result["output_directory"])/"audit_report.json")
        advance("VALIDATE",{"message":"正在校验源文件、年度草稿及推算/推定标志。"})
        if file_hash(source["_source"])!=source["_sha256"]:raise ValueError("SOURCE_CHANGED")
        advance("IMPORT_REVIEW",{"message":"正在把新版结果导入原工作台，保留B结论。"})
        ui=import_review(b_result,document,database) if database else None
        extraction_usage=usage_summary(directory/"upstream");b_usage=usage_summary(directory)
        usage={key:extraction_usage[key]+b_usage[key] for key in ("api_attempts","known_input_tokens","known_output_tokens","known_total_tokens","unknown_usage_requests")}
        usage["by_stage"]={**extraction_usage["by_stage"],**b_usage["by_stage"]}
        summary={"engine":ENGINE,"version":"ESGAGENT-INTEGRATION-1.0","run_id":run_id,"status":"PARTIAL",
            "mode":"model" if allow_external else "rules_only","source_pdf":source["name"],"source_sha256":source["_sha256"],"source_unchanged":True,
            "candidate_count":len(result["batch"]["candidates"]),"record_count":len(records),"evidence_count":len(evidence),"candidate_outcome_count":len(records),
            "classification_states":dict(Counter(r["classification_status"] for r in records)),"b_review_states":dict(Counter(r["review_status"] for r in audit["record_results"])),
            "b_findings":len(audit["findings"]),"b_result":b_result,"usage":usage,"workbench_run_id":ui,"output_directory":str(directory),
            "reused_extraction":result["from_cache"],"profile_version":"ESGAGENT-13-METRICS","production_ready":False,"scoring_enabled":False,
            "page_coverage":{"MODEL_CLAIMED_PAGES":len((result["batch"].get("coverage") or {}).get("pages_scanned",[]))},
            "upstream_result_sha256":file_hash(directory/"upstream/worker_result.json")}
        atomic_json(directory/"manifest.json",{"engine":ENGINE,"source_directory":str(directory/"upstream"),"pdf_sha256":source["_sha256"],
            "source_hashes":{str(Path(source["_source"]).resolve()):source["_sha256"]},
            "a_artifact_hashes":{p.name:file_hash(p) for p in (directory/"a").iterdir() if p.is_file()}})
        atomic_json(directory/"run_summary.json",summary)
        from .annual import build_collection,persist
        collection=build_collection([summary]);path=persist(collection,directory/"annual")
        summary["annual_collection"]={"path":str(path),"statistics":collection["statistics"]}
        atomic_json(directory/"run_summary.json",summary)
        atomic_json(directory/"validation_report.json",{"status":"DRAFT_REVIEW_REQUIRED","source_unchanged":True,"production_ready":False,"scoring_enabled":False,
            "engine":ENGINE,"note":"新Agent研究草稿；推定/推算与报告直接披露分开标记，不自动发布正式事实。"})
        preview(directory,records,evidence,audit,summary)
        state.update(status="PARTIAL",stage="FINISHED");atomic_json(directory/"task.json",state)
        return summary
    except Exception as exc:
        state.update(status="FAILED",error_type=type(exc).__name__);atomic_json(directory/"task.json",state)
        raise


def inspect_annual(summary):
    directory=Path(summary["output_directory"])
    if file_hash(directory/"upstream/worker_result.json")!=summary["upstream_result_sha256"]:raise ValueError("UPSTREAM_RESULT_CHANGED")
    data=load(directory/"upstream/worker_result.json")
    if file_hash(data["source_pdf"])!=data["source_sha256"]:raise ValueError("SOURCE_CHANGED")
    manifest=load(directory/"manifest.json")
    if any(file_hash(directory/"a"/n)!=h for n,h in manifest["a_artifact_hashes"].items()):raise ValueError("ANNUAL_A_SNAPSHOT_CHANGED")
    audit=load(Path(summary["b_result"]["output_directory"])/"audit_report.json")
    verdicts={r["record_id"]:r["review_status"] for r in audit["record_results"]}
    candidates={c["candidate_id"]:c for c in data["batch"]["candidates"]}
    company=data["company"];values=data["stage1"]["values"];year=data["report"]["report_year"]
    identity={"stock_code":company["hkex_code"].split(".")[0],"company_name":values["公司名称"],"sasb_sector":company["sasb_sector"],"sasb_industry":company["sasb_industry"],
              "status":"UPSTREAM_COMPANY_MASTER","reason":"新版输入公司主数据；未从模型猜测SASB行业。"}
    observations=[]
    for metric,selection in data["stage1"]["selections"].items():
        field=METRICS.get(metric)
        if not field or selection["value"] is None:continue
        candidate=candidates.get(selection["candidate_id"],{})
        flags=list(candidate.get("ambiguity_flags",[]));reasons=[]
        if re.match(r"^\s*(约|約|approximately|about|~|≈)",str(candidate.get("raw_value") or ""),re.I):
            flags.append("SOURCE_APPROXIMATION")
        source_ids=[selection["candidate_id"]] if selection["candidate_id"] else []
        if selection["derived"]:
            source_ids=[s["candidate_id"] for m,s in data["stage1"]["selections"].items() if m in ("water_withdrawal","water_recycled_volume") and s["candidate_id"]]
            flags.append("DERIVED_NOT_DISCLOSED")
        statuses={verdicts.get("rec_eg_"+cid,"NOT_REVIEWED") for cid in source_ids}
        if "REJECTED" in statuses:reasons.append("B_REJECTED")
        flags.extend("B_"+s for s in sorted(statuses))
        if field=="revenue_million":
            flags.append("REVENUE_SOURCE_REQUIRES_ANNUAL_CROSSCHECK")
            if selection["currency"]=="CNY" and not re.search(r"RMB|CNY|人民币|人民幣",str(candidate.get("unit","")),re.I):flags.append("CURRENCY_INFERRED")
        if field.endswith("_m3") and (str(candidate.get("unit","")).strip().lower()=="t" or any(x in str(candidate.get("unit","")).lower() for x in ("吨","噸","ton"))):
            flags.append("WATER_TONNE_TO_M3_APPROXIMATION")
        tier="DERIVED_DRAFT" if selection["derived"] else "INFERRED_DRAFT" if "DEFINITION_INFERRED" in flags else "ESGAGENT_DRAFT"
        evs=[e for e in data["batch"]["evidence"] if any(e["evidence_id"] in candidates[cid]["evidence_ids"] for cid in source_ids)]
        observations.append({"observation_id":summary["run_id"]+":"+metric,"run_id":summary["run_id"],"stock_code":identity["stock_code"],"field":field,"year":year,
            "normalized_value":selection["value"],"normalized_unit":selection["unit"],"currency":selection["currency"],"period_start":None,"period_end":None,
            "reasons":reasons,"source_bound":True,"admission_tier":tier,"quality_flags":flags,"record_id":"rec_eg_"+selection["candidate_id"] if selection["candidate_id"] else None,
            "source_proof":{"candidate_ids":source_ids,"evidence":evs,"selection":selection,"upstream_batch_id":data["batch"]["batch_id"]},
            "page":evs[0]["source_page"] if evs else None,"raw_value":candidate.get("raw_value"),"raw_unit":candidate.get("unit"),
            "normalization":{"approximate":bool(set(flags)&{"WATER_TONNE_TO_M3_APPROXIMATION","SOURCE_APPROXIMATION"})}})
    return {"run_id":summary["run_id"],"workbench_run_id":summary.get("workbench_run_id"),"source_pdf":summary["source_pdf"],"engine":ENGINE,
        "pdf_sha256":data["source_sha256"],
        "identity":identity,"observations":observations,"period":{"year":year,"status":"UPSTREAM_REPORT_YEAR"},"coverage_complete":False,
        "source_hashes":{str(directory/"upstream/worker_result.json"):summary["upstream_result_sha256"]}}
