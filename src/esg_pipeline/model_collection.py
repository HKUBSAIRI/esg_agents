"""A selects existing evidence; B independently reviews source pages for filling.

These are research collection values, not overrides of the original S4 decisions
or permission to score. All requests are bounded, cached and accounted for.
"""
from datetime import datetime, timezone
from contextlib import nullcontext
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import uuid

import fitz

from esg_extraction.extractor import atomic_json, file_hash
from esg_extraction.numbers import tokens
from .company_master import identify, load_master
from .field_rules import standardize, semantic_issues
from .inputs import read_lines
from .llm import JsonClient, usage_summary
from .water_template import ROOT, contract

VERSION = "WATER-MODEL-COLLECTION-1.0"
PROMPTS = ROOT / "skills/collect_water"
TOPIC = re.compile(r"water consumption|water withdrawal|recycled water|reused.*water|water recycling|wastewater discharge|\bCOD\b|ammonia nitrogen|total nitrogen|total phosphorus|\brevenue\b|取水|耗水|回用|循环水|排水量|氨氮|总氮|总磷|营业收入", re.I)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def compact(text):
    return re.sub(r"\s+", "", str(text)).casefold()


def decimal_text(raw):
    found = tokens(raw)
    if len(found) != 1 or found[0]["value_decimal"] is None:
        return None
    if re.search(r"[<>≤≥~≈]|at least|less than|more than", str(raw), re.I):
        return None
    return found[0]["value_decimal"]


def source_bundle(summary, max_pages=24):
    directory = Path(summary["output_directory"])
    manifest = load(directory / "manifest.json")
    for name, digest in manifest["a_artifact_hashes"].items():
        if file_hash(directory / "a" / name) != digest:
            raise ValueError("COLLECTION_A_SOURCE_CHANGED")
    context = load(directory / "a/document_context.json")
    pdf = Path(context["pdf_path"])
    if file_hash(pdf) != context["pdf_sha256"]:
        raise ValueError("COLLECTION_PDF_CHANGED")
    with fitz.open(pdf) as document:
        native = {n + 1: p.get_text() for n, p in enumerate(document)}
    evidence = read_lines(directory / "a/evidence.jsonl")
    candidates = read_lines(directory / "a/quantitative_candidates.jsonl")
    ranked = []
    for page, text in native.items():
        hits = len(TOPIC.findall(text))
        if hits:
            ranked.append((hits * 10 + min(len(tokens(text)), 50), page))
    for ev in evidence:
        if TOPIC.search(ev["content"]):
            ranked.append((len(TOPIC.findall(ev["content"])) * 10, ev["page_no"]))
    # Score per physical page, rather than let duplicate evidence consume slots.
    scores = {}
    for score, page in ranked:
        scores[page] = max(score, scores.get(page, 0))
    pages = sorted(sorted(scores, key=lambda p: (-scores[p], p))[:max_pages])
    return {"summary": summary, "pdf": pdf, "sha256": context["pdf_sha256"], "native": native,
            "evidence": evidence, "candidates": candidates, "pages": pages,
            "metadata": [{"page": n, "text": native[n][:20000]} for n in range(1, min(4, len(native)) + 1)]}


def validate_proposal(proposal, sources, spec):
    issues = []
    if not isinstance(proposal, dict):
        return None
    item = dict(proposal)
    field, page = item.get("field"), item.get("page")
    if not isinstance(field,str) or type(page) is not int:
        return None
    # A sometimes returns the target-unit amount instead of the source scalar.
    # Rebind only if the quote is verbatim PDF text and the two Decimal amounts
    # are exactly equivalent; B still independently reviews the original amount.
    if field=="revenue_million" and compact(item.get("quote","")) in compact(sources["native"].get(page,"")):
        match=re.search(r"\b(RMB|CNY|HKD|USD|EUR)\s*([0-9][0-9,.]*)\s*(billion|million|thousand)\b",str(item.get("quote","")),re.I)
        if match:
            source_number=decimal_text(match[2]);proposed_number=decimal_text(item.get("value_text",""))
            if source_number is not None and proposed_number is not None:
                source_unit=match[1].upper()+" "+match[3].lower()
                source_value,_,source_rule=standardize(field,source_number,source_unit)
                proposed_value,_,proposed_rule=standardize(field,proposed_number,item.get("unit"))
                if source_value is not None and proposed_value is not None and Decimal(source_value)==Decimal(proposed_value) and source_rule.get("currency")==proposed_rule.get("currency"):
                    item["a_original_value_text"],item["a_original_unit"]=item.get("value_text"),item.get("unit")
                    item.update(value_text=match[2],unit=source_unit,representation_rule="REBIND_EXACT_EQUIVALENT_SOURCE_MONEY")
    if field not in spec["fields"][4:] or field == "revenue_currency":
        issues.append("UNKNOWN_NUMERIC_FIELD")
    if type(item.get("year")) is not int or item["year"] not in (2024, 2025):
        issues.append("TARGET_YEAR_UNPROVEN")
    if type(page) is not int or page not in sources["pages"]:
        issues.append("PAGE_NOT_IN_SELECTION_INPUT")
    number = decimal_text(item.get("value_text", ""))
    if number is None or Decimal(number) < 0:
        issues.append("INVALID_EXACT_NUMBER")
    quote = item.get("quote", "")
    native = sources["native"].get(page, "")
    extracts = [e["content"] for e in sources["evidence"] if e["page_no"] == page]
    text_match = isinstance(quote, str) and len(compact(quote)) >= 6 and compact(quote) in compact(native)
    extraction_match = isinstance(quote, str) and len(compact(quote)) >= 6 and any(compact(quote) in compact(t) for t in extracts)
    if not text_match and not extraction_match:
        issues.append("QUOTE_NOT_FOUND_IN_INPUT")
    if number is not None and not any(t["value_decimal"] is not None and Decimal(t["value_decimal"]) == Decimal(number) for t in tokens(quote)):
        issues.append("NUMBER_NOT_IN_QUOTE")
    if item.get("group_total") is not True:
        issues.append("NOT_GROUP_TOTAL")
    if not isinstance(item.get("unit"), str) or not item["unit"].strip():
        issues.append("UNIT_UNRESOLVED")
    identity=hashlib.sha256(json.dumps(item,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    item.update(proposal_id="mp_" + identity, value_decimal=number,
                validation_issues=issues, native_quote_matched=text_match,
                extraction_quote_matched=extraction_match)
    return item


def review_outcome(item, review, sources, spec, *, has_image=False):
    reasons = list(item["validation_issues"])
    if not isinstance(review, dict):
        review = {}
    if review.get("decision") != "ACCEPT":
        reasons.append("COLLECTION_B_" + str(review.get("decision", "NOT_REVIEWED")))
    for flag in ("group_total", "year_checked", "period_checked", "scope_checked", "definition_checked"):
        if review.get(flag) is not True:
            reasons.append("B_" + flag.upper() + "_UNPROVEN")
    if review.get("field") != item.get("field") or type(review.get("year")) is not int or review.get("year") != item.get("year"):
        reasons.append("A_B_FIELD_YEAR_DISAGREEMENT")
    checked = decimal_text(review.get("value_text", ""))
    number = item.get("value_decimal")
    equivalent_money=False
    if item.get("field")=="revenue_million" and checked is not None and number is not None:
        av,_,ar=standardize("revenue_million",number,item.get("unit"))
        bv,_,br=standardize("revenue_million",checked,review.get("unit"))
        equivalent_money=av is not None and bv is not None and Decimal(av)==Decimal(bv) and ar.get("currency")==br.get("currency")
    if (checked is None or number is None or Decimal(checked) != Decimal(number)) and not equivalent_money:
        reasons.append("A_B_NUMBER_DISAGREEMENT")
    # No silent unit repair by the reviewer.
    if compact(review.get("unit", "")) != compact(item.get("unit", "")) and not equivalent_money:
        reasons.append("A_B_UNIT_DISAGREEMENT")
    quote = review.get("source_quote", "")
    native = sources["native"].get(item.get("page"), "")
    native_match = isinstance(quote, str) and len(compact(quote)) >= 6 and compact(quote) in compact(native)
    if not has_image and not (item["native_quote_matched"] and native_match):
        reasons.append("NO_INDEPENDENT_SOURCE_TO_REVIEW")
    if not any(t["value_decimal"] is not None and number is not None and Decimal(t["value_decimal"]) == Decimal(number) for t in tokens(quote)):
        reasons.append("B_SOURCE_QUOTE_NUMBER_MISSING")
    value, unit, normal = None, None, {"error": "INVALID_PROPOSAL"}
    quality = ["MODEL_REVIEWED_RESEARCH_ONLY", "NOT_FORMAL_SCORE_APPROVAL"]
    if number is not None and item.get("field") in spec["fields"][4:] and item["field"] != "revenue_currency":
        context = native
        water_assumption = (item.get("water_type") in ("ordinary_water", "freshwater") and review.get("ordinary_water") is True)
        if water_assumption and not re.search(r"seawater|brine|concentrated brine|海水|卤水|鹵水", native, re.I):
            context += "\nordinary water [A/B project density approximation, not a measured density]"
        value, unit, normal = standardize(item["field"], number, item.get("unit"), context)
        if normal.get("error"):
            reasons.append(normal["error"])
        if normal.get("approximate"):
            quality.append("WATER_TONNE_TO_M3_APPROXIMATION")
        if item["field"] == "water_recycling_rate_pct" and value is not None and not 0 <= Decimal(value) <= 100:
            reasons.append("RATIO_RANGE_INVALID")
        # Generic consumption cannot pass merely by the A/B boolean flags.
        if item["field"] == "water_consumption_m3":
            reasons.extend(semantic_issues(item["field"], "Water consumption", native))
        if item["field"] == "revenue_million":
            quality.append("ESG_REPORTED_REVENUE_REQUIRES_AUDITED_ANNUAL_CROSSCHECK")
    return {**item, "review": review, "blocking_reasons": sorted(set(reasons)), "quality_flags": quality,
            "money_representation_equivalent":equivalent_money,
            "normalized_value": value, "normalized_unit": unit, "normalization": normal,
            "currency": normal.get("currency"), "image_reviewed": has_image,
            "status": "MODEL_REVIEWED" if not reasons and value is not None else "WITHHELD"}


def run_model_collection(summary, *, key_file=None, a_calls=4, b_calls=4, vision=True,
                         client=None, progress=None, cancel_check=None, pdf_lock=None, cached_a_only=False):
    for count in (a_calls, b_calls):
        if type(count) is not int or not 1 <= count <= 8:
            raise ValueError("COLLECTION_BUDGET_MUST_BE_1_TO_8")
    spec = contract()
    lock = pdf_lock or nullcontext()
    with lock:
        sources = source_bundle(summary, max_pages=min(a_calls * 6, 24))
    directory = Path(summary["output_directory"]) / "model_collection" / ("mc_" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    client = client or JsonClient(directory, key_file=key_file, max_calls=a_calls+b_calls,
                                  stage_budgets={"collection_select":0 if cached_a_only else a_calls,"collection_review":b_calls})
    def advance(stage, **detail):
        if cancel_check: cancel_check()
        if progress: progress(stage, {**detail,"pipeline_directory":str(directory)})
    client.before_call = lambda: cancel_check() if cancel_check else None
    proposals, missing, reviews, outcomes = [], [], {}, []
    state = {"version": VERSION, "status": "RUNNING", "source_run_id": summary["run_id"],
             "pdf_sha256": sources["sha256"], "template_sha256": spec["template_sha256"],
             "directory": str(directory), "created_at":datetime.now(timezone.utc).isoformat(),
             "budgets":{"a_calls":a_calls,"b_calls":b_calls,"vision":vision}, "selected_pages":sources["pages"]}
    atomic_json(directory / "task.json", state)
    try:
        for start in range(0, len(sources["pages"]), 6):
            selected = sources["pages"][start:start+6]
            advance("COLLECTION_A", message="LLM正在筛选已提取的年度字段。", pages=selected)
            page_data = []
            for page in selected:
                evs = [e for e in sources["evidence"] if e["page_no"] == page]
                page_data.append({"page": page, "native_text":sources["native"][page][:24000],
                    "extracted_regions":[{"evidence_version_id":e["evidence_version_id"],"text":e["content"][:4500]} for e in evs[:3]],
                    "candidates":[{k:c.get(k) for k in ("candidate_id","metric_label","metric_label_raw","value_text","unit","year","scope")} for c in sources["candidates"] if c["page_no"]==page and TOPIC.search(str(c.get("metric_label") or c.get("metric_label_raw") or ""))][:80]})
            payload={"fields":spec["fields"][4:],"workbook_rules":spec["rules"],"report_metadata":sources["metadata"],"pages":page_data}
            response=client.request("collection_select",payload,(PROMPTS/"select.md").read_text(encoding="utf-8"))
            atomic_json(directory/f"a_{start//6:02d}.json",response)
            data=response["data"]
            if not isinstance(data.get("proposals"),list) or len(data["proposals"])>20:
                raise ValueError("INVALID_COLLECTION_SELECTION_SHAPE")
            proposals.extend(p for p in (validate_proposal(p,sources,spec) for p in data["proposals"]) if p)
            missing.extend(data.get("missing",[]) if isinstance(data.get("missing",[]),list) else [])
        proposals=list({p["proposal_id"]:p for p in proposals}.values())
        atomic_json(directory/"a_proposals.json",proposals)
        # Independent review grouped by physical source page, at most four images.
        pages=sorted({p["page"] for p in proposals if not p["validation_issues"]})
        for start in range(0,min(len(pages),b_calls*4),4):
            selected=pages[start:start+4]
            items=[p for p in proposals if p["page"] in selected and not p["validation_issues"]]
            advance("COLLECTION_B",message="B正在对照原页核对数值、年列、单位和定义。",pages=selected)
            pictures=[]
            if vision:
                with lock, fitz.open(sources["pdf"]) as pdf:
                    for page in selected:
                        image=directory/f"source_page_{page:05d}.png"
                        pdf[page-1].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(image)
                        pictures.append(image)
            payload={"workbook_rules":spec["rules"],"report_metadata":sources["metadata"],"proposals":items,
                     "pages":[{"page":p,"native_text":sources["native"][p]} for p in selected],
                     "image_order":selected if vision else []}
            response=client.request("collection_review",payload,(PROMPTS/"review.md").read_text(encoding="utf-8"),images=pictures)
            atomic_json(directory/f"b_{start//4:02d}.json",response)
            rows=response["data"].get("reviews")
            if not isinstance(rows,list):raise ValueError("INVALID_COLLECTION_REVIEW_SHAPE")
            expected={p["proposal_id"] for p in items}
            seen=set()
            for row in rows:
                if not isinstance(row,dict) or row.get("proposal_id") not in expected or row["proposal_id"] in seen:
                    raise ValueError("COLLECTION_REVIEW_REFERENCE_INVALID")
                seen.add(row["proposal_id"]); reviews[row["proposal_id"]]=(row,bool(pictures))
        for item in proposals:
            review, image=reviews.get(item["proposal_id"],(None,False))
            outcomes.append(review_outcome(item,review,sources,spec,has_image=image))
        advance("COLLECTION_VALIDATE",message="正在校验填表依据并保存审查台账。")
        if file_hash(sources["pdf"])!=sources["sha256"]:raise ValueError("COLLECTION_PDF_CHANGED")
        identity=identify(summary["source_pdf"],"\n".join(sources["native"].get(n,"") for n in range(1,5)),load_master())
        for item in outcomes:
            if identity["status"]!="MATCHED":
                item["blocking_reasons"].append(identity["status"]);item["status"]="WITHHELD"
            if item.get("field")=="revenue_million" and "financial" in identity.get("sasb_sector","").lower():
                item["blocking_reasons"].append("FINANCIAL_SECTOR_REVENUE_POLICY_UNRESOLVED");item["status"]="WITHHELD"
        result={**state,"status":"COMPLETED","identity":identity,"outcomes":outcomes,"missing":missing,
                "usage":usage_summary(directory),"scoring_enabled":False,"production_ready":False,
                "prompts_sha256":{p.name:file_hash(p) for p in PROMPTS.glob("*.md")}}
        result_path=directory/"result.json"
        atomic_json(result_path,result)
        atomic_json(directory.parent/"latest.json",{"path":str(result_path),"sha256":file_hash(result_path)})
        atomic_json(directory/"task.json",{**state,"status":"COMPLETED"})
        advance("COLLECTION_DONE",message="年度字段筛选与B原页核对已完成。")
        return result
    except Exception as exc:
        atomic_json(directory/"task.json",{**state,"status":"FAILED","error_type":type(exc).__name__,"usage":usage_summary(directory)})
        raise


def model_observations(summary, template_sha256):
    """Only consume a completed, hashed sidecar matching this PDF and template."""
    directory=Path(summary["output_directory"])/"model_collection"
    pointer=directory/"latest.json"
    if not pointer.is_file():return [],None
    ref=load(pointer);path=Path(ref["path"]).resolve()
    if not path.is_relative_to(directory.resolve()) or file_hash(path)!=ref["sha256"]:
        raise ValueError("COLLECTION_SIDECAR_CHANGED")
    result=load(path)
    if result["source_run_id"]!=summary["run_id"] or result["pdf_sha256"]!=summary["source_sha256"] or result["template_sha256"]!=template_sha256:
        raise ValueError("COLLECTION_SOURCE_VERSION_MISMATCH")
    observations=[]
    for item in result["outcomes"]:
        observations.append({"observation_id":summary["run_id"]+":"+item["proposal_id"],"run_id":summary["run_id"],
            "stock_code":result["identity"].get("stock_code"),"field":item.get("field"),"year":item.get("year"),
            "normalized_value":item["normalized_value"],"normalized_unit":item["normalized_unit"],"currency":item.get("currency"),
            "period_start":None,"period_end":None,"normalization":item["normalization"],
            "reasons":item["blocking_reasons"],"source_bound":item["status"]=="MODEL_REVIEWED",
            "admission_tier":"MODEL_REVIEWED","quality_flags":item["quality_flags"],"source_proof":item,
            "page":item.get("page"),"raw_value":item.get("value_text"),"raw_unit":item.get("unit"),
            "b_review_status":item["review"].get("decision","NOT_REVIEWED")})
    histories=[usage_summary(ledger.parent) for ledger in directory.glob("mc_*/model_usage.jsonl")]
    cumulative={key:sum(u[key] for u in histories) for key in ("api_attempts","known_input_tokens","known_output_tokens","known_total_tokens","unknown_usage_requests")}
    return observations,{"directory":str(path.parent),"usage":result["usage"],"cumulative_usage":cumulative,"status":result["status"]}


def revalidate_saved(summary):
    """Re-apply deterministic guards to saved A/B responses, making no requests."""
    spec=contract()
    model_observations(summary,spec["template_sha256"])  # Hash/version guard first.
    parent=Path(summary["output_directory"])/"model_collection"
    ref=load(parent/"latest.json");old=Path(ref["path"]);result=load(old)
    sources=source_bundle(summary,max_pages=len(result["selected_pages"]))
    result["outcomes"]=[review_outcome(item,item["review"],sources,spec,has_image=item["image_reviewed"]) for item in result["outcomes"]]
    for item in result["outcomes"]:
        if result["identity"]["status"]!="MATCHED":
            item["blocking_reasons"].append(result["identity"]["status"]);item["status"]="WITHHELD"
        if item.get("field")=="revenue_million" and "financial" in result["identity"].get("sasb_sector","").lower():
            item["blocking_reasons"].append("FINANCIAL_SECTOR_REVENUE_POLICY_UNRESOLVED");item["status"]="WITHHELD"
    result["revalidation"]={"source_result":str(old),"source_result_sha256":ref["sha256"],"new_api_calls":0,
                            "rule":"EXACT_DECIMAL_MONEY_REPRESENTATION_EQUIVALENCE"}
    target=old.parent/("result_revalidated_"+uuid.uuid4().hex+".json")
    atomic_json(target,result)
    atomic_json(parent/"latest.json",{"path":str(target),"sha256":file_hash(target)})
    return result
