"""Read-only PDF extraction with page checkpoints, visual coverage and provenance."""
from contextlib import contextmanager
import hashlib
import html
import json
import math
import os
from pathlib import Path
import platform
import uuid
from datetime import datetime, timezone

import fitz

from . import VERSION
from .numbers import tokens, textual_state
from .vision import PROMPT, validate_response


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)


def fingerprint(value): return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):digest.update(block)
    return digest.hexdigest()


def atomic_json(path,value):
    path=Path(path);temporary=path.with_name(path.name+"."+uuid.uuid4().hex[:8]+".tmp")
    try:
        temporary.write_text(canonical(value),encoding="utf-8")
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


@contextmanager
def exclusive(directory):
    path=directory/".extract.lock"
    try: descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError("This extraction already has a lock. Check for a running process before removing .extract.lock.") from None
    try:
        os.write(descriptor,str(os.getpid()).encode());os.close(descriptor)
        yield
    finally:path.unlink(missing_ok=True)


def page_box(page):
    return page.rect*page.derotation_matrix


def render(page,box,target,dpi):
    rotated=fitz.Rect(box)*page.rotation_matrix
    clip=rotated & page.rect
    scale=dpi/72
    pixels=clip.width*clip.height*scale*scale
    if pixels>12_000_000:scale*=math.sqrt(12_000_000/pixels)
    pixmap=page.get_pixmap(matrix=fitz.Matrix(scale,scale),clip=clip,alpha=False)
    pixmap.save(str(target))


def region_evidence(pid,page,kind,box,content,source_ref,asset):
    version="ev_"+fingerprint([pid,page,kind,box,content])[:24]
    return {"schema_version":"esg-quant-1.0","pid":pid,"evidence_id":version,"evidence_version_id":version,
            "page_no":page,"kind":kind,"bbox":list(box),"coordinate_space":"pymupdf_unrotated_points",
            "source_format":"v2","content":content,"content_sha256":hashlib.sha256(content.encode()).hexdigest(),
            "numbers":[t["value_text"] for t in tokens(content)],"source_ref":source_ref,"asset":asset,
            "tier":"unverified","unit_hint":None,"year_hint":None}


def native_tables(page,pid,directory,dpi,strategy):
    tables=[];evidence=[];candidates=[];warnings=[]
    try:
        def snapshot(table):
            return {"values":table.extract(),"bbox":list(table.bbox),"headers":list(table.header.names or []) if table.header else [],
                    "rows":[list(row.cells) for row in table.rows]}
        finder=page.find_tables(strategy="lines")
        found=[snapshot(t) for t in finder.tables]
        used="lines"
        if not found and strategy=="lines_then_text":
            bounds=page_box(page);mid=bounds.width/2
            words=page.get_text("words")
            band=[w for w in words if w[0]<mid+20 and w[2]>mid-20 and bounds.height*.1<w[1]<bounds.height*.9]
            crossing=False
            for drawing in page.get_drawings():
                box=drawing["rect"]
                if box.height<3 and box.x0<mid-20 and box.x1>mid+20 and bounds.height*.1<box.y0<bounds.height*.9:crossing=True;break
            clips=[bounds]
            if bounds.width>bounds.height*1.25 and not band and not crossing:
                clips=[fitz.Rect(0,0,mid,bounds.height),fitz.Rect(mid,0,bounds.width,bounds.height)]
                warnings.append("DOUBLE_PAGE_LAYOUT_SPLIT_HEURISTIC_REQUIRES_REVIEW")
            for clip in clips:
                finder=page.find_tables(strategy="text",clip=clip,min_words_vertical=3,min_words_horizontal=1)
                found.extend(snapshot(t) for t in finder.tables)
            used="text_heuristic"
        for index,table in enumerate(found):
            values=table["values"]
            if len(values)<2 or max((len(row) for row in values),default=0)<2:continue
            if not any(tokens(cell or "") or textual_state(cell or "")=="TEXTUAL_ZERO_CANDIDATE" for row in values for cell in row):continue
            tid=f"p{page.number+1:05d}_t{index:03d}"
            box=table["bbox"]
            asset="assets/"+tid+".png"
            render(page,box,directory/asset,dpi)
            headers=table["headers"]
            above=page.get_textbox(fitz.Rect(0,max(0,box[1]-60),page_box(page).width,box[1]))
            below=page.get_textbox(fitz.Rect(0,box[3],page_box(page).width,min(page_box(page).height,box[3]+60)))
            content=above+"\n<table>"+"".join("<tr>"+"".join("<td>"+html.escape(str(cell or ""))+"</td>" for cell in row)+"</tr>" for row in values)+"</table>\n"+below
            ev=region_evidence(pid,page.number+1,"table_cell",box,content,{"table_id":tid,"extractor":"pymupdf","strategy":used,"context_before":above,"context_after":below},asset)
            evidence.append(ev)
            cells=[]
            for ri,row in enumerate(values):
                for ci,cell in enumerate(row):
                    text="" if cell is None else str(cell)
                    cell_box=table["rows"][ri][ci] if ri<len(table["rows"]) and ci<len(table["rows"][ri]) else None
                    header=headers[ci] if ci<len(headers) else None
                    row_prefix=" | ".join(str(v) for v in row[:ci] if v is not None)
                    state=textual_state(text)
                    cells.append({"row":ri,"column":ci,"text":text,"bbox":list(cell_box) if cell_box else None,
                                  "column_header":header,"row_prefix":row_prefix,"raw_is_null":cell is None,
                                  "state":"MERGED_OR_EMPTY_UNRESOLVED" if cell is None else state})
                    parsed=tokens(text)
                    if not parsed and state=="TEXTUAL_ZERO_CANDIDATE":parsed=[{"value_text":text,"value_decimal":None,"warning":"TEXTUAL_ZERO_REQUIRES_CONTEXT"}]
                    for ni,number in enumerate(parsed):
                        cid="q_"+fingerprint([pid,tid,ri,ci,ni,text])[:24]
                        candidates.append({"schema_version":"esg-quant-1.0","candidate_id":cid,"pid":pid,"page_no":page.number+1,
                                           "evidence_version_id":ev["evidence_version_id"],"source_kind":"native_table",
                                           "bbox":list(cell_box) if cell_box else box,"table_id":tid,"row":ri,"column":ci,
                                           "metric_label_raw":row_prefix or None,"column_header":header,"row_context":row,
                                           **number,"unit":None,"year":None,"scope":None,
                                           "value_kind":"TEXTUAL_ZERO_CANDIDATE" if state=="TEXTUAL_ZERO_CANDIDATE" else "LABELLED",
                                           "value_role":"HEADER_OR_LABEL_CANDIDATE" if ri==0 or str(text).strip()==str(header or "").strip() else "UNRESOLVED",
                                           "review_status":"NEEDS_REVIEW","normalization":None})
            tables.append({"table_id":tid,"pid":pid,"page_no":page.number+1,"bbox":box,"headers":headers,"cells":cells,
                           "context_before":above,"context_after":below,"strategy":used,"structure_verified":False,
                           "evidence_version_id":ev["evidence_version_id"]})
    except Exception as exc:
        warnings.append("TABLE_ENGINE_"+type(exc).__name__)
    return tables,evidence,candidates,warnings


def vision_observations(page,pid,data,asset):
    evidence=[];candidates=[]
    for index,region in enumerate(data["regions"]):
        normalized=region["bbox"];viewport=page.rect
        rotated=fitz.Rect(viewport.x0+normalized[0]/1000*viewport.width,viewport.y0+normalized[1]/1000*viewport.height,
                          viewport.x0+normalized[2]/1000*viewport.width,viewport.y0+normalized[3]/1000*viewport.height)
        box=list(rotated*page.derotation_matrix)
        rid=f"p{page.number+1:05d}_v{index:03d}"
        kind="table_cell" if region["kind"]=="table" else "figure"
        ev=region_evidence(pid,page.number+1,kind,box,canonical(region),{"region_id":rid,"bbox_source":"MODEL_ESTIMATED","extractor":"vision","evidence_origin":"MODEL_TRANSCRIPTION_NOT_INDEPENDENT_PROOF","table_id":rid if kind=="table_cell" else None},asset)
        evidence.append(ev)
        for oi,observation in enumerate(region["observations"]):
            parsed=tokens(observation.get("value_text") or "")
            value=parsed[0]["value_decimal"] if len(parsed)==1 else None
            candidates.append({"schema_version":"esg-quant-1.0","candidate_id":"q_"+fingerprint([pid,rid,oi,observation])[:24],
                               "pid":pid,"page_no":page.number+1,"evidence_version_id":ev["evidence_version_id"],"source_kind":"vision_"+region["kind"],
                               "bbox":box,"bbox_source":"MODEL_ESTIMATED","metric_label_raw":observation.get("metric_label"),
                               **observation,"value_decimal":value,"context":region.get("context"),"review_status":"NEEDS_REVIEW",
                               "normalization":None,"native_correspondence":"NOT_RECONCILED"})
    return evidence,candidates


def mark_duplicates(candidates):
    grouped={}
    for row in candidates:
        # Similar values alone are not enough to merge records from different scopes.
        key=(row["page_no"],row.get("value_decimal"))
        if key[1] is not None:grouped.setdefault(key,[]).append(row)
    for rows in grouped.values():
        if len({r["source_kind"] for r in rows})>1:
            group="dup_"+fingerprint(sorted(r["candidate_id"] for r in rows))[:16]
            for row in rows:row["possible_duplicate_group"]=group


def process_page(page,pid,directory,config,client=None):
    number=page.number+1
    page_asset=f"assets/page_{number:05d}.png"
    render(page,page_box(page),directory/page_asset,config["dpi"])
    text=page.get_text("text",sort=True)
    words=page.get_text("words",sort=True)
    tables,evidence,candidates,warnings=native_tables(page,pid,directory,config["dpi"],config["table_strategy"])
    images=[]
    try:
        images=[{k:item.get(k) for k in ("bbox","width","height","xref")} for item in page.get_image_info(xrefs=True)]
    except Exception as exc:warnings.append("IMAGE_INVENTORY_"+type(exc).__name__)
    page_ev=region_evidence(pid,number,"text",list(page_box(page)),text,{"unit_id":f"page_{number:05d}","extractor":"pdf_text","full_page_image":page_asset},page_asset)
    evidence.append(page_ev)
    visual_status="NOT_EXECUTED"
    receipt=None
    if client is not None:
        if getattr(client,"blocked_code",None):
            visual_status="PENDING_CLIENT_FAILURE";warnings.append(client.blocked_code)
        elif client.calls>=client.max_calls:visual_status="PENDING_BUDGET"
        else:
            try:
                if hasattr(client,"context"):client.context.update(pdf_id=pid,page_no=number)
                receipt=client(directory/page_asset)
                data=validate_response(receipt["data"])
                visual_status="MODEL_EXTRACTED_NEEDS_REVIEW" if data["page_status"]=="EXTRACTED" else "MODEL_PARTIAL" if data["page_status"]=="PARTIAL" else "MODEL_NO_QUANTITATIVE_FOUND_UNVERIFIED"
                extra_evidence,extra_candidates=vision_observations(page,pid,data,page_asset)
                evidence.extend(extra_evidence);candidates.extend(extra_candidates);warnings.extend(data["warnings"])
            except Exception as exc:
                visual_status="FAILED"
                # Only known safe codes; raw exception messages may include credentials.
                code=str(exc) if str(exc) in {"VISION_CONNECTION_FAILED","VISION_INCOMPLETE_RESPONSE","VISION_BUDGET_EXHAUSTED","VISION_RESPONSE_INVALID","VISION_HTTP_401","VISION_HTTP_403"} else type(exc).__name__
                warnings.append("VISION_"+code)
    coverage={"page_no":number,"native_tables":len(tables),"native_word_count":len(words),"raster_images":len(images),
              "page_asset":page_asset,"visual_status":visual_status,"quantitative_candidates":len(candidates),
              "coverage_verified":False,"warnings":warnings}
    if len(text.strip())<40:coverage["warnings"].append("LOW_TEXT_PAGE_NEEDS_OCR_OR_VISION")
    if not tables:coverage["warnings"].append("NO_NATIVE_TABLE_FOUND_NOT_PROOF_OF_NO_QUANTITATIVE_CONTENT")
    return {"page_no":number,"tables":tables,"evidence":evidence,"candidates":candidates,"images":images,"words":words,
            "coverage":coverage,"vision_receipt":receipt,"asset_sha256":{str(path.relative_to(directory)):file_hash(path) for path in (directory/"assets").glob(f"p{number:05d}_*.png")} | {page_asset:file_hash(directory/page_asset)}}


def output_jsonl(path,values):
    temporary=path.with_name(path.name+".tmp")
    try:
        temporary.write_text("\n".join(canonical(value) for value in values)+("\n" if values else ""),encoding="utf-8")
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def extract_pdf(source,output_root,*,pages=None,dpi=120,table_strategy="lines_then_text",client=None,retry_failed=False,progress=None):
    source=Path(source).resolve();output_root=Path(output_root).resolve()
    if not source.is_file() or source.suffix.lower()!=".pdf":raise ValueError("Input must be a PDF file")
    if not 72<=dpi<=200:raise ValueError("DPI must be 72-200")
    if table_strategy not in ("lines","lines_then_text"):raise ValueError("Unsupported table strategy")
    source_sha=file_hash(source)
    code_sha=fingerprint({p.name:file_hash(p) for p in Path(__file__).parent.glob("*.py")})
    config={"version":VERSION,"code_sha256":code_sha,"pymupdf":fitz.VersionBind,"dpi":dpi,"table_strategy":table_strategy,
            "vision_model":client.model if client is not None else None,"prompt_sha256":file_hash(PROMPT)}
    cache_key=fingerprint(config)
    pid="pdf_"+source_sha[:20]
    directory=output_root/pid/cache_key[:16]
    directory.mkdir(parents=True,exist_ok=True)
    (directory/"assets").mkdir(exist_ok=True);(directory/"pages").mkdir(exist_ok=True);(directory/"runs").mkdir(exist_ok=True)
    with exclusive(directory):
        with fitz.open(source) as document:
            if document.needs_pass:raise ValueError("Encrypted PDF requires an unlocked input copy")
            selected=sorted(set(pages if pages is not None else range(1,len(document)+1)))
            if not selected or any(type(p) is not int or p<1 or p>len(document) for p in selected):raise ValueError("Invalid selected pages")
            results=[];resumed=0
            for number in selected:
                checkpoint=directory/"pages"/f"{number:05d}.json"
                cached=None
                if checkpoint.exists():
                    try:
                        value=json.loads(checkpoint.read_text(encoding="utf-8"))
                        valid_assets=all((directory/name).resolve().is_relative_to(directory.resolve()) and (directory/name).is_file() and file_hash(directory/name)==digest for name,digest in value["data"]["asset_sha256"].items())
                        if value.get("cache_key")==cache_key and value["data"].get("page_no")==number and value.get("data_sha256")==fingerprint(value["data"]) and valid_assets:cached=value["data"]
                    except (ValueError,KeyError,TypeError,OSError):cached=None
                status=cached["coverage"]["visual_status"] if cached else None
                rerun=cached is None or retry_failed and status in ("FAILED","PENDING_CLIENT_FAILURE") or client is not None and status=="PENDING_BUDGET"
                if rerun:
                    try:result=process_page(document[number-1],pid,directory,config,client)
                    except Exception as exc:
                        result={"page_no":number,"tables":[],"evidence":[],"candidates":[],"images":[],"words":[],"asset_sha256":{},
                                "coverage":{"page_no":number,"visual_status":"FAILED","coverage_verified":False,"warnings":["PAGE_PROCESS_"+type(exc).__name__]}}
                    atomic_json(checkpoint,{"cache_key":cache_key,"data_sha256":fingerprint(result),"data":result})
                else:result=cached;resumed+=1
                results.append(result)
                if callable(progress):progress(number,len(selected),result["coverage"])
            candidates=[r for page in results for r in page["candidates"]]
            mark_duplicates(candidates)
            evidence=[r for page in results for r in page["evidence"]]
            tables=[r for page in results for r in page["tables"]]
            coverage=[r["coverage"] for r in results]
            reviews=[{"review_id":"review_p"+str(p["page_no"]),"page_no":p["page_no"],"reason":"VISUAL_AND_COMPLETENESS_NOT_VERIFIED","details":p} for p in coverage]
            for row in candidates:
                if row.get("warning") or row.get("possible_duplicate_group") or row.get("value_kind") in ("ESTIMATED","TEXTUAL_ZERO","TEXTUAL_ZERO_CANDIDATE","UNCERTAIN"):
                    reviews.append({"review_id":"review_"+row["candidate_id"],"candidate_id":row["candidate_id"],"page_no":row["page_no"],"reason":"CANDIDATE_REQUIRES_REVIEW"})
            for filename,values in [("quantitative_candidates.jsonl",candidates),("evidence.jsonl",evidence),("tables.jsonl",tables),("page_coverage.jsonl",coverage),("review_items.jsonl",reviews)]:output_jsonl(directory/filename,values)
            unchanged=file_hash(source)==source_sha
            report={"schema_version":"esg-quant-1.0","version":VERSION,"pid":pid,"source_pdf":str(source),"source_sha256":source_sha,
                    "source_unchanged":unchanged,"page_count":len(document),"selected_pages":selected,"unselected_page_count":len(document)-len(selected),
                    "processed_pages":len(results),"resumed_pages":resumed,"native_tables":len(tables),"quantitative_candidates":len(candidates),
                    "candidate_count_is_not_distinct_metric_count":True,"evidence_regions":len(evidence),"review_items":len(reviews),
                    "vision_executed":client is not None,"vision_api_attempts_in_client":client.calls if client else 0,
                    "status":"PARTIAL" if unchanged else "FAILED","coverage_verified":False,"output_directory":str(directory),
                    "limitations":["候选值不是已验收指标，包含表头/年份/坐标标签候选。","图片、矢量图和漏检表格须视觉复核；没有识别出不等于没有定量数据。","不自动填充合并表头或拼接跨页表格；保留原单元格和全页证据。","模型与原生结果可能重复，保留待对齐标记，不累计为独立事实。"]}
            atomic_json(directory/"document_context.json",{"schema_version":"esg-quant-1.0","pid":pid,"pdf_path":str(source),"page_count":len(document),"source_format":"v2","report_year":None,"company_name":None})
            atomic_json(directory/"extraction_report.json",report)
            atomic_json(directory/"manifest.json",{"source_sha256":source_sha,"configuration":config,"python":platform.python_version(),
                        "files":{p.name:file_hash(p) for p in directory.iterdir() if p.is_file() and p.name not in ("manifest.json",".extract.lock")}})
            atomic_json(directory/"runs"/("run_"+uuid.uuid4().hex[:16]+".json"),{"created_at":datetime.now(timezone.utc).isoformat(),"report":report})
            return report
