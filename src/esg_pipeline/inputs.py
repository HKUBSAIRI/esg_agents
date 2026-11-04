"""Read immutable extraction artifacts and attach independently read PDF context."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import fitz

from esg_extraction.extractor import file_hash, fingerprint, canonical


def read_lines(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def load_extraction(directory, *, recovered=None, pdf=None):
    directory = Path(directory).resolve()
    report = json.loads((directory / "extraction_report.json").read_text(encoding="utf-8"))
    source = Path(pdf or report["source_pdf"]).resolve()
    if file_hash(source) != report["source_sha256"]:
        raise ValueError("SOURCE_PDF_HASH_CHANGED")
    candidates = read_lines(directory / "quantitative_candidates.jsonl")
    evidence = read_lines(directory / "evidence.jsonl")
    if recovered:
        candidates += read_lines(Path(recovered) / "candidates.jsonl")
        evidence += read_lines(Path(recovered) / "evidence.jsonl")
    ids = [c["candidate_id"] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("DUPLICATE_CANDIDATE_ID")
    index = {}
    for ev in evidence:
        if hashlib.sha256(ev["content"].encode()).hexdigest() != ev["content_sha256"]:
            raise ValueError("EVIDENCE_HASH_CHANGED")
        if ev["pid"] != report["pid"] or ev["evidence_version_id"] in index:
            raise ValueError("EVIDENCE_ID_INVALID")
        index[ev["evidence_version_id"]] = ev
    if any(c["pid"] != report["pid"] or c["evidence_version_id"] not in index for c in candidates):
        raise ValueError("CANDIDATE_TRACE_INVALID")
    mapped = {}
    with fitz.open(source) as document:
        cover = "\n".join(document[n].get_text() for n in range(min(3, len(document))))
        years = set(re.findall(r"(?:sustainability\s+report|ESG\s+report|report\s+and\s+governance)\s*(20\d{2})", cover, re.I))
        # No filing-year fallback; a missing title date remains unknown.
        report_year = int(next(iter(years))) if len(years) == 1 else None
        for key, original in index.items():
            ev = deepcopy(original)
            page = document[ev["page_no"] - 1]
            box = fitz.Rect(ev["bbox"]) if ev.get("bbox") else page.rect
            native_text = page.get_textbox(box)
            visual = ev.get("source_ref", {}).get("extractor") == "vision"
            content = ("[PDF_TEXT_REGION; independent text layer, reading order may be imperfect]\n" + native_text +
                       "\n[UPSTREAM_" + ("MODEL_TRANSCRIPTION; NOT INDEPENDENT PROOF" if visual else "TABLE_EXTRACTION") + "]\n" + ev["content"])
            ev.update(content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                      evidence_version_id="ev_a_" + fingerprint([key, content])[:24], tier="unverified",
                      numbers=original.get("numbers", []), unit_hint=None, year_hint=None)
            ev["source_ref"] = {**ev.get("source_ref", {}), "upstream_evidence_version_id": key,
                                "upstream_content_sha256": original["content_sha256"],
                                "pdf_path": str(source), "pdf_sha256": report["source_sha256"],
                                "asset_path": str(directory / ev["asset"]), "native_text": native_text,
                                "independent_binding_verified": False,
                                "evidence_origin": "MODEL_TRANSCRIPTION_NOT_INDEPENDENT_PROOF" if visual else "NATIVE_TABLE_UNVERIFIED"}
            mapped[key] = ev
    for c in candidates:
        old = c["evidence_version_id"]
        c["upstream_evidence_version_id"] = old
        c["evidence_version_id"] = mapped[old]["evidence_version_id"]
    context = {"schema_version": "local-a-1.0", "pid": report["pid"], "company_name": source.stem,
               "company_name_source": "FILENAME_ONLY_NOT_ENTITY_VERIFIED", "report_year": report_year,
               "report_year_source": "PDF_TITLE" if report_year else "UNKNOWN", "filing_year": None,
               "period_start": None, "period_end": None, "page_count": report["page_count"],
               "pdf_path": str(source), "source_format": "v2", "pdf_sha256": report["source_sha256"]}
    paths = [directory / name for name in ("extraction_report.json", "document_context.json", "evidence.jsonl", "quantitative_candidates.jsonl", "page_coverage.jsonl")]
    if recovered:
        paths += [p for p in Path(recovered).glob("*.json*") if p.is_file()]
    source_hashes = {str(p.resolve()): file_hash(p) for p in paths if p.is_file()}
    return {"document": context, "candidates": candidates, "evidence": list(mapped.values()),
            "extraction_report": report, "page_coverage": read_lines(directory / "page_coverage.jsonl"),
            "source_hashes": source_hashes, "extraction_directory": str(directory)}
