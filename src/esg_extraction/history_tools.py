"""Offline readers for historical usage and extraction-response formats.

No extraction runner, fixed report list or external model calls live here.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
from .extractor import atomic_json
from .numbers import textual_state,tokens
from .vision import validate_response


def usage_summary(batch):
    attempts={}
    for path in (Path(batch)/"usage").glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:row=json.loads(line)
                except ValueError:continue
                attempts[row["attempt_id"]]=row
    prompt=completion=total=known=unknown=0
    by_pdf={}
    for row in attempts.values():
        name=row.get("context",{}).get("pdf_name","unknown")
        stats=by_pdf.setdefault(name,{"attempts":0,"known_total_tokens":0,"unknown_usage_requests":0,"validated":0})
        stats["attempts"]+=1;stats["validated"]+=row.get("status")=="VALIDATED"
        usage=row.get("usage") or {}
        p=usage.get("prompt_tokens",usage.get("input_tokens"));c=usage.get("completion_tokens",usage.get("output_tokens"));t=usage.get("total_tokens")
        if type(t) is not int and type(p) is int and type(c) is int:t=p+c
        if type(t) is int:
            total+=t;known+=1;stats["known_total_tokens"]+=t
        else:unknown+=1;stats["unknown_usage_requests"]+=1
        if type(p) is int:prompt+=p
        if type(c) is int:completion+=c
    result={"api_attempts":len(attempts),"requests_with_known_total":known,"requests_with_unknown_usage":unknown,
            "known_prompt_tokens":prompt,"known_completion_tokens":completion,"known_total_tokens":total,
            "total_is_complete":unknown==0,"by_pdf":by_pdf,"note":"Image tokens are normally included in prompt_tokens; do not add them twice. Unknown usage is not zero and exact billed total may exceed the returned-usage sum."}
    atomic_json(Path(batch)/"token_usage_summary.json",result)
    return result



def normalize(value):
    data=deepcopy(value);changes=[]
    if not isinstance(data,dict):raise ValueError("Not an object")
    if data.get("warnings") is None:data["warnings"]=[];changes.append("null warnings represented as empty list")
    for region in data.get("regions",[]):
        for row in region.get("observations",[]):
            raw=row.get("value_text")
            if row.get("value_kind")=="LABELLED" and isinstance(raw,str) and textual_state(raw)=="MISSING_OR_NOT_APPLICABLE":
                row["value_kind"]="UNCERTAIN";changes.append("Non-numeric missing literal retained, never converted to zero")
            elif row.get("value_kind") in ("LABELLED","ESTIMATED") and isinstance(raw,str) and raw.strip() and not tokens(raw):
                row["value_kind"]="UNCERTAIN";changes.append("Non-numeric text retained verbatim as uncertain, without assigning a number")
            if isinstance(row.get("year"),str) and re.fullmatch(r"(?:19|20|21)\d{2}",row["year"]):
                row["year"]=int(row["year"]);changes.append("Four-digit year string represented as integer")
    validate_response(data)
    if not changes:raise ValueError("No supported representation repair")
    return data,sorted(set(changes))

