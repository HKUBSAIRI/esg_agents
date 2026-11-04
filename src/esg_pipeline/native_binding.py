"""Re-read PDF cell text and align labels/year/units; no model-generated proof."""
from copy import deepcopy
from datetime import date,timedelta
from decimal import Decimal
import re

import fitz

from esg_extraction.numbers import tokens
from .field_rules import semantic_issues
from .profiles import matches

MONTHS={name.lower():n for n,name in enumerate(("January","February","March","April","May","June","July","August","September","October","November","December"),1)}
UNIT = r"(?<![A-Za-z])(?:m³|m3|m\^3|立方米|ML|mL|kL|GL|litres?|Litres?|L|tonnes?|Tonnes?|kg|Kg|g|t|吨|噸|%|percent)(?![A-Za-z0-9])"


def period_context(pages):
    # Reporting-period sections take precedence over title and publication dates.
    explicit=[]
    months='|'.join(MONTHS)
    for n,text in enumerate(pages[:8],1):
        pattern=r"(\d{1,2})\s+("+months+r")\s+(20\d{2})\s*(?:to|until|through|[-–—])\s*(\d{1,2})\s+("+months+r")\s+(20\d{2})"
        for match in re.finditer(pattern,text,re.I):
            nearby=text[max(0,match.start()-180):match.end()+100]
            if not re.search(r"reporting period|report covers|reporting year|financial year|fiscal year",nearby,re.I):continue
            start=date(int(match[3]),MONTHS[match[2].lower()],int(match[1]));end=date(int(match[6]),MONTHS[match[5].lower()],int(match[4]))
            explicit.append((n,start,end,match.group(0)))
        for match in re.finditer(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:至|到|[-–—])\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",text):
            nearby=text[max(0,match.start()-100):match.end()+100]
            if not re.search(r"报告期|報告期|财年|財年|报告涵盖|報告涵蓋",nearby):continue
            explicit.append((n,date(*map(int,match.groups()[:3])),date(*map(int,match.groups()[3:])),match.group(0)))
    if explicit:
        first=min(x[0] for x in explicit); found=[x for x in explicit if x[0]==first]
        if len({(x[1],x[2]) for x in found})!=1:return {"status":"FISCAL_PERIOD_CONFLICT","start":None,"end":None,"year":None,"quote":None}
        n,start,end,quote=found[0]
        return {"status":"ANNUAL_YEAR_ENDED" if 364<=(end-start).days+1<=366 else "NONSTANDARD_PERIOD_REQUIRES_EXPLICIT_DATES",
                "start":str(start),"end":str(end),"year":end.year,"quote":quote,"page":n}
    candidates=[]
    for n,text in enumerate(pages[:8],1):
        for match in re.finditer(r"(?:year ended|年度截至|截至)\s*(\d{1,2})\s*("+'|'.join(MONTHS)+r")\s*(20\d{2})",text,re.I):
            end=date(int(match[3]),MONTHS[match[2].lower()],int(match[1]))
            candidates.append((n,end,match.group(0)))
        for match in re.finditer(r"(?:year ended|年度截至|截至)\s*(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日",text,re.I):
            candidates.append((n,date(*map(int,match.groups())),match.group(0)))
    if not candidates:return {"status":"FISCAL_PERIOD_UNRESOLVED","start":None,"end":None,"year":None,"quote":None}
    first=min(x[0] for x in candidates)
    found=[x for x in candidates if x[0]==first]
    if len({x[1] for x in found})!=1:return {"status":"FISCAL_PERIOD_CONFLICT","start":None,"end":None,"year":None,"quote":None}
    _,end,quote=found[0]
    try:start=date(end.year-1,end.month,end.day)+timedelta(days=1)
    except ValueError:start=date(end.year-1,2,28)+timedelta(days=1)
    if re.search(r"(?:six|nine|eighteen|15|18|6|9)[ -]month|change.{0,30}financial year|长财年|短财年|更改.{0,8}年结",'\n'.join(pages[:8]),re.I):
        return {"status":"NONSTANDARD_PERIOD_REQUIRES_EXPLICIT_DATES","start":None,"end":str(end),"year":end.year,"quote":quote}
    return {"status":"ANNUAL_YEAR_ENDED","start":str(start),"end":str(end),"year":end.year,"quote":quote,"page":first}


def _scope(text,field):
    pattern = r"consolidated|合并|合併" if field=="revenue_million" else r"(?:of|for|by)\s+the\s+group|the group[’']s|group total|集团整体|集團整體|本集团|本集團"
    match=re.search(pattern,text,re.I)
    if not match:return None
    if field!="revenue_million" and not re.search(r"all (?:operations|operating entities|subsidiaries)|entire group|whole group|集团所有|集團所有|全部运营|全部運營|集团整体|集團整體",text,re.I):return None
    quote=text[max(0,match.start()-60):match.end()+140]
    if re.search(r"selected|certain|only|except|exclud|部分|仅|僅|不包括",quote,re.I):return None
    return quote


def cell_facts(pdf_path,tables,profile):
    facts=[]
    with fitz.open(pdf_path) as pdf:
        pages=[p.get_text() for p in pdf]
        period=period_context(pages)
        textpages={}
        for table in tables:
            if table.get("strategy")!="lines":continue
            candidate_rows={c["row"] for c in table["cells"] if c["column"]==0 and len(c.get("text",""))<240 and len(matches(c.get("text",""),profile))==1}
            if not candidate_rows:continue
            if table["page_no"] not in textpages:
                page=pdf[table["page_no"]-1]
                textpage=page.get_textpage()
                textpages[table["page_no"]]=(page,textpage)
            page,textpage=textpages[table["page_no"]]
            # Re-read labels, year headers and units as well as numeric cells.
            # Cached table text and contextual prose alone are not proof.
            cells=[]
            for original in table["cells"]:
                # Ignore unrelated rows before expensive glyph-level clipping.
                # An omitted/ambiguous row cannot become an accepted fact.
                if original["row"] not in candidate_rows and not re.fullmatch(r"\s*20\d{2}\s*",original.get("text","")) and not re.fullmatch(UNIT,original.get("text","")):
                    continue
                if not original.get("bbox"):continue
                literal=page.get_textbox(fitz.Rect(original["bbox"]),textpage=textpage).strip()
                if re.sub(r"\s+","",literal)!=re.sub(r"\s+","",original.get("text","")):continue
                cells.append({**original,"text":literal})
            rowmap={}
            for cell in cells:rowmap.setdefault(cell["row"],{})[cell["column"]]=cell
            bounds=table["bbox"]
            before=page.get_textbox(fitz.Rect(bounds[0],max(0,bounds[1]-130),bounds[2],bounds[1]),textpage=textpage)
            heading=before
            for ri,row in rowmap.items():
                label=" ".join(c["text"] for col,c in sorted(row.items()) if col<min((cc for cc,c in row.items() if tokens(c["text"])),default=1)).strip()
                if not label:label=row.get(0,{}).get("text","").strip()
                fields=matches(label,profile)
                if len(fields)!=1:continue
                field=fields[0]
                rowtext=" ".join(c["text"] for c in row.values())
                for ci,cell in row.items():
                    values=tokens(cell["text"])
                    if ci==0 or len(values)!=1 or values[0]["value_decimal"] is None or cell.get("raw_is_null"):continue
                    number=values[0]
                    # Year/header rows and multi-number cells are never values.
                    if re.fullmatch(r"20\d{2}",cell["text"].strip()):continue
                    if re.search(r"[<>≤≥~≈]|at least|less than|more than|不少于|超过|超過",cell["text"],re.I):continue
                    columns=" ".join(r.get(ci,{}).get("text","") for nr,r in rowmap.items() if nr<ri)
                    years={int(y) for y in re.findall(r"\b20\d{2}\b",columns)}
                    if len(years)!=1:continue
                    year=next(iter(years))
                    unit_options=re.findall(UNIT,cell["text"]+" "+rowtext+" "+" ".join(r.get(ci,{}).get("text","") for nr,r in rowmap.items() if nr<ri))
                    unique={x for x in unit_options if x.lower() not in ("m",)}
                    unit=next(iter(unique)) if len(unique)==1 else None
                    if field=="revenue_million":
                        money_match=re.search(r"(?:RMB|CNY|HKD|HK\$|USD|US\$|EUR|人民币|港元|美元)\s*(?:['’]000|thousands?|millions?|billions?|百万元|千元|万元|亿元)?",heading+" "+rowtext,re.I)
                        unit=money_match.group(0) if money_match else None
                    if not unit:continue
                    box=cell.get("bbox")
                    if not box:continue
                    literal=page.get_textbox(fitz.Rect(box),textpage=textpage).strip()
                    checked=tokens(literal)
                    if len(checked)!=1 or checked[0]["value_decimal"] is None or Decimal(checked[0]["value_decimal"])!=Decimal(number["value_decimal"]):continue
                    fact_context=heading+"\n"+rowtext
                    facts.append({"table_id":table["table_id"],"page_no":table["page_no"],"row":ri,"column":ci,"label":label,
                                  "field":field,"value_decimal":number["value_decimal"],"unit":unit,"year":year,"bbox":box,
                                  "literal":literal,"year_quote":columns[:700],"scope_quote":_scope(heading,field),
                                  "semantic_issues":semantic_issues(field,label,fact_context),"context":fact_context,
                                  "method":"PDF_LINE_GRID_AND_CELL_TEXT_RECHECK","source_format":"native_pdf"})
    return facts,pages,period


def bind(candidate,facts,profile):
    value=candidate.get("value_decimal")
    if value is None:return None
    if candidate.get("source_kind")=="native_table":
        found=[f for f in facts if (f["table_id"],f["row"],f["column"])==(candidate.get("table_id"),candidate.get("row"),candidate.get("column"))]
    else:
        fields=matches(str(candidate.get("metric_label") or candidate.get("metric_label_raw") or ""),profile)
        found=[f for f in facts if f["page_no"]==candidate["page_no"] and f["field"] in fields
               and (candidate.get("year") is None or f["year"]==candidate["year"])]
    found=[f for f in found if Decimal(f["value_decimal"])==Decimal(str(value))
           and (candidate.get("year") is None or str(f["year"])==str(candidate["year"]))]
    if len(found)!=1:return None
    return deepcopy(found[0])


def enrich(candidates,facts,profile):
    for candidate in candidates:
        fact=bind(candidate,facts,profile)
        if not fact:continue
        candidate["independent_cell_verification"]={"passed":True,**fact}
        if candidate.get("source_kind")=="native_table":
            candidate["upstream_unit"],candidate["upstream_year"]=candidate.get("unit"),candidate.get("year")
            candidate.update(metric_label=fact["label"],unit=fact["unit"],year=fact["year"])
        elif candidate.get("unit") is None:candidate["unit"]=fact["unit"]
        if candidate.get("year") is None:candidate["year"]=fact["year"]
