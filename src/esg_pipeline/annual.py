"""Read-only admission of A/B results into a versioned company-year collection.

No language model calls, imputation, scoring or mutation of upstream snapshots.
Each blank is an explicit collection state, never a zero or non-disclosure claim.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from esg_extraction.extractor import file_hash
from .company_master import identify, load_master
from .field_rules import semantic_issues, standardize
from .inputs import read_lines
from .native_binding import bind, cell_facts
from .profiles import load_profile
from .water_template import FIELDS, ROOT, VERSION, contract

NUMERIC_FIELDS = tuple(f for f in FIELDS[4:] if f != "revenue_currency")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def select_primary(observations):
    """Conflict detection precedes admission, so a suspect competing value matters."""
    eligible = [o for o in observations if not o["reasons"]]
    if not eligible:
        return {"value":None, "status":"NO_ELIGIBLE_VALUE" if observations else "NO_CANDIDATE_IN_AVAILABLE_INPUT",
                "reasons":sorted({r for o in observations for r in o["reasons"]}), "source_ids":[]}
    signatures = {(Decimal(o["normalized_value"]), o.get("currency"),o.get("period_start"),o.get("period_end")) for o in eligible}
    competing = [o for o in observations if o.get("normalized_value") is not None and o.get("source_bound")]
    other = {(Decimal(o["normalized_value"]),o.get("currency"),o.get("period_start"),o.get("period_end")) for o in competing}
    if len(signatures)!=1 or any(s not in signatures for s in other):
        return {"value":None,"status":"CONFLICT","reasons":["COMPETING_VALUES_REQUIRE_RECONCILIATION"],"source_ids":[]}
    tiers={o.get("admission_tier") for o in eligible}
    status=next((tier for tier in ("DERIVED_DRAFT","INFERRED_DRAFT","ESGAGENT_DRAFT","MODEL_REVIEWED") if tier in tiers),"AUTO_VERIFIED")
    return {"value":eligible[0]["normalized_value"],"currency":eligible[0].get("currency"),"status":status,
            "quality_flags":sorted({f for o in eligible for f in o.get("quality_flags",[])}),
            "approximate":any(o.get("normalization",{}).get("approximate") for o in eligible),
            "reasons":[],"source_ids":[o["observation_id"] for o in eligible]}


def inspect_report(summary, master, profile=None):
    profile = profile or load_profile()
    directory = Path(summary["output_directory"])
    a = directory / "a"
    manifest = load(directory / "manifest.json")
    for name, digest in manifest["a_artifact_hashes"].items():
        if file_hash(a/name)!=digest:raise ValueError("ANNUAL_A_SNAPSHOT_CHANGED")
    document = load(a/"document_context.json")
    pdf = Path(document["pdf_path"])
    if file_hash(pdf)!=document["pdf_sha256"]:raise ValueError("ANNUAL_PDF_CHANGED")
    extraction = Path(manifest["source_directory"])
    facts,pages,period = cell_facts(pdf,read_lines(extraction/"tables.jsonl"),profile)
    identity = identify(summary["source_pdf"],"\n".join(pages[:3]),master)
    candidates = {c["candidate_id"]:c for c in read_lines(a/"quantitative_candidates.jsonl")}
    bpath = Path(summary["b_result"]["output_directory"])/"audit_report.json"
    audit = load(bpath)
    bresults = {r["record_id"]:r for r in audit["record_results"]}
    observations = []
    for record in read_lines(a/"records.jsonl"):
        field = record.get("profile_field")
        if field not in NUMERIC_FIELDS:continue
        c = candidates.get(record.get("candidate_id"),{})
        fact = bind(c,facts,profile)
        reasons = []
        if identity["status"]!="MATCHED":reasons.append(identity["status"])
        if record.get("classification_status")!="ASSIGNED":reasons.append("A_CLASSIFICATION_UNRESOLVED")
        bstatus = bresults.get(record["record_id"],{}).get("review_status","NOT_REVIEWED")
        if bstatus!="CONFIRMED":reasons.append("B_"+bstatus)
        if not fact:reasons.append("INDEPENDENT_CELL_BINDING_UNPROVEN")
        if fact and fact["field"]!=field:reasons.append("A_FIELD_SOURCE_CONFLICT")
        context = fact["context"] if fact else ""
        label = fact["label"] if fact else record.get("metric_label_raw","")
        reasons += semantic_issues(field,label,context)
        if not fact or not fact.get("scope_quote"):reasons.append("COMPLETE_GROUP_BOUNDARY_UNPROVEN")
        year = fact["year"] if fact else record.get("year")
        if period["status"]!="ANNUAL_YEAR_ENDED":reasons.append(period["status"])
        elif year!=period["year"]:reasons.append("HISTORICAL_PERIOD_BOUNDARY_UNPROVEN")
        if year is None:reasons.append("YEAR_UNRESOLVED")
        value,unit,normal = None,None,{"error":"NO_INDEPENDENT_VALUE"}
        if fact:
            value,unit,normal=standardize(field,fact["value_decimal"],fact["unit"],context)
            # Candidate units must independently describe the same quantity.
            if c.get("unit"):
                cv,_,cn=standardize(field,c["value_decimal"],c["unit"],context)
                if cv is None or value is None or Decimal(cv)!=Decimal(value) or cn.get("currency")!=normal.get("currency"):
                    reasons.append("CANDIDATE_UNIT_SOURCE_CONFLICT")
            if record.get("year") is not None and str(year)!=str(record["year"]):reasons.append("A_YEAR_SOURCE_CONFLICT")
            if value is not None:
                if Decimal(value)<0:reasons.append("NEGATIVE_ACTUAL_VALUE")
                if field=="water_recycling_rate_pct" and not 0<=Decimal(value)<=100:reasons.append("RATIO_RANGE_INVALID")
            if field=="revenue_million":
                import re
                if not (re.search(r"audited|经审计|經審計",context,re.I) and re.search(r"income statement|profit or loss|损益表|損益表",context,re.I)) or re.search(r"unaudited|未经审计|未經審計",context,re.I):
                    reasons.append("AUDITED_CONSOLIDATED_ANNUAL_REVENUE_UNPROVEN")
                company=master["companies"].get(identity.get("stock_code"),{})
                if "financial" in company.get("sasb_sector","").lower():reasons.append("FINANCIAL_SECTOR_REVENUE_POLICY_UNRESOLVED")
        if normal.get("error"):reasons.append(normal["error"])
        if c.get("value_kind") in ("ESTIMATED","UNCERTAIN"):reasons.append("NON_EXACT_SOURCE_VALUE")
        observations.append({"observation_id":summary["run_id"]+":"+record["record_id"],"run_id":summary["run_id"],
            "stock_code":identity.get("stock_code"),"field":field,"year":year,"pid":record["pid"],
            "record_id":record["record_id"],"candidate_id":record.get("candidate_id"),"evidence_version_id":record["evidence_version_id"],
            "page":record["source_page"],"raw_value":record.get("raw_value"),"raw_unit":record.get("raw_unit"),
            "normalized_value":value,"normalized_unit":unit,"currency":normal.get("currency"),"normalization":normal,
            "period_start":period["start"],"period_end":period["end"],"b_review_status":bstatus,
            "source_bound":bool(fact),"source_proof":fact,"reasons":list(dict.fromkeys(reasons))})
    hashes = {str(p.resolve()):file_hash(p) for p in (a/"records.jsonl",a/"quantitative_candidates.jsonl",bpath,extraction/"tables.jsonl") if p.is_file()}
    return {"run_id":summary["run_id"],"workbench_run_id":summary.get("workbench_run_id"),"source_pdf":summary["source_pdf"],
            "pdf_sha256":document["pdf_sha256"],"identity":identity,"period":period,"observations":observations,
            "native_bound_facts":len(facts),"source_hashes":hashes,"coverage":summary.get("page_coverage",{}),
            "coverage_complete":False}


def build_collection(summaries, *, years=(2024,2025), all_companies=False, root=ROOT, review_resolver=None):
    spec=contract(Path(root)/"水资源.xlsx")
    master=load_master(root)
    from .esgagent_pipeline import inspect_annual
    from .esgagent_core import DRAFT_POLICY
    reports=[inspect_annual(s) if s.get("engine")=="esgagent-v1" else inspect_report(s,master) for s in summaries]
    if review_resolver:
        for report in reports:
            if report.get("engine")!="esgagent-v1" or not report.get("workbench_run_id"):continue
            try:review=review_resolver(report["workbench_run_id"])
            except LookupError:continue
            tasks=review.get("tasks",[])
            for observation in report["observations"]:
                ids={"rec_eg_"+cid for cid in observation["source_proof"]["candidate_ids"]}
                states={task["state"] for task in tasks if task.get("record_uid") in ids}
                blocked=states & {"REAUDIT_REQUIRED","EXCLUDED_FROM_SCORING"}
                observation["reasons"].extend("HUMAN_"+s for s in sorted(blocked))
                observation["quality_flags"].extend("HUMAN_"+s for s in sorted(states))
            report["review_events"]=[{"seq":e.get("seq"),"action":e.get("action"),"reason":e.get("reason")} for e in review.get("events",[])]
            report["inherited_block_run_ids"]=review.get("inherited_block_run_ids",[])
    # Reusing a cached native candidate must not evade a prior exclusion merely
    # by assigning it a new workbench run ID. Include derived input candidates.
    blocked_sources={}
    for report in reports:
        if report.get("engine")!="esgagent-v1":continue
        for o in report["observations"]:
            blocked={r for r in o["reasons"] if r.startswith("HUMAN_") or r=="B_REJECTED"}
            if blocked and o.get("record_id"):
                cid=o["record_id"].removeprefix("rec_eg_")
                blocked_sources.setdefault((report["pdf_sha256"],cid),set()).update(blocked)
    for report in reports:
        if report.get("engine")!="esgagent-v1":continue
        for o in report["observations"]:
            for cid in o["source_proof"]["candidate_ids"]:
                o["reasons"]=sorted(set(o["reasons"])|blocked_sources.get((report["pdf_sha256"],cid),set()))
    for report in reports:
        if report.get("engine")=="esgagent-v1":
            identity=report["identity"]
            master["companies"][identity["stock_code"]]={k:identity[k] for k in FIELDS[:4]}
    from .model_collection import model_observations
    for summary,report in zip(summaries,reports):
        extra,model=([],None) if summary.get("engine")=="esgagent-v1" else model_observations(summary,spec["template_sha256"])
        report["observations"].extend(extra)
        report["model_collection"]=model
    codes=set(master["companies"]) if all_companies else {r["identity"]["stock_code"] for r in reports if r["identity"].get("stock_code")}
    observations=[o for r in reports for o in r["observations"]]
    rows=[]
    for code in sorted(codes):
        company=master["companies"][code]
        related=[r for r in reports if r["identity"].get("stock_code")==code]
        for year in sorted(set(years)):
            values={k:company[k] for k in FIELDS[:4]}
            states={}
            for field in NUMERIC_FIELDS:
                relevant=[o for o in observations if o["stock_code"]==code and o["field"]==field and o["year"]==year]
                result=select_primary(relevant)
                if not relevant:
                    result["status"]="NO_REPORT" if not related else "NO_ELIGIBLE_YEAR_CANDIDATE"
                    result["reasons"]=[result["status"]]
                states[field]=result
                values[field]=result["value"]
            values["revenue_currency"]=states["revenue_million"].get("currency") if values["revenue_million"] is not None else None
            rows.append({"stock_code":code,"year":year,"values":values,"states":states,
                         "identity_states":sorted({r["identity"]["status"] for r in related}),"report_ids":[r["run_id"] for r in related]})
    stats=Counter(s["status"] for row in rows for s in row["states"].values())
    return {"version":VERSION,"batch_id":"water_"+uuid.uuid4().hex,"created_at":datetime.now(timezone.utc).isoformat(),
            "contract":spec,"master_source_hashes":master["source_hashes"],"population_status":master["population_status"],
            "registered_companies":len(master["companies"]),"all_companies":all_companies,"years":list(years),
            "reports":reports,"rows":rows,"statistics":{"reports":len(reports),"companies":len(codes),
            "company_years":len(rows),"observations":len(observations),"admission_states":dict(stats),
            "rejection_reasons":dict(Counter(r for o in observations for r in o["reasons"]))},
            "scoring_enabled":False,"production_ready":False,"api_calls":0,
            "research_policy":DRAFT_POLICY if any(s.get("engine")=="esgagent-v1" for s in summaries) else None,
            "model_usage_history":[r["model_collection"]["cumulative_usage"] for r in reports if r.get("model_collection")]}


def persist(collection, directory):
    """Immutable batch JSON plus normalized SQLite rows. Re-running creates a batch."""
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    batch=collection["batch_id"]
    target=directory/(batch+".json")
    raw=json.dumps(collection,ensure_ascii=False,allow_nan=False,indent=2)
    with target.open("x",encoding="utf-8") as stream:stream.write(raw)
    with closing(sqlite3.connect(directory/"water.sqlite3",timeout=30)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, created_at TEXT, version TEXT, sha256 TEXT, payload TEXT);
          CREATE TABLE IF NOT EXISTS company_years (batch_id TEXT,stock_code TEXT,year INTEGER,payload TEXT,PRIMARY KEY(batch_id,stock_code,year));
          CREATE TABLE IF NOT EXISTS observations (batch_id TEXT,observation_id TEXT,payload TEXT,PRIMARY KEY(batch_id,observation_id));""")
        db.execute("INSERT INTO batches VALUES(?,?,?,?,?)",(batch,collection["created_at"],VERSION,hashlib.sha256(raw.encode()).hexdigest(),raw))
        db.executemany("INSERT INTO company_years VALUES(?,?,?,?)",[(batch,r["stock_code"],r["year"],json.dumps(r,ensure_ascii=False)) for r in collection["rows"]])
        db.executemany("INSERT INTO observations VALUES(?,?,?)",[(batch,o["observation_id"],json.dumps(o,ensure_ascii=False)) for r in collection["reports"] for o in r["observations"]])
        db.commit()
    pointer=directory/"latest.json"
    temporary=directory/("latest."+uuid.uuid4().hex+".tmp")
    temporary.write_text(json.dumps({"batch_id":batch,"path":str(target.resolve()),"statistics":collection["statistics"]},ensure_ascii=False),encoding="utf-8")
    temporary.replace(pointer)
    return target
