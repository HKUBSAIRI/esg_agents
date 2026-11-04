"""Reuse the established industry mapping; never infer sectors from ESG wording."""
from pathlib import Path
import re
import sys

from esg_extraction.extractor import file_hash

ROOT = Path(__file__).resolve().parents[2]


def load_master(root=ROOT):
    sys.path.insert(0,str(ROOT))
    from classify_esg import load_mapping
    root = Path(root)
    paths = [root/"公司行业对照.xlsx",root/"data/补充公司行业.csv"]
    mapping = load_mapping(*paths)
    companies = {}
    for alias,item in mapping.items():
        code = item["canonical_code"]
        current = companies.setdefault(code,{"stock_code":code,"company_name":item["company"],"sasb_sector":item["sector"],
                                             "sasb_industry":item["industry"],"aliases":[]})
        if (current["sasb_sector"],current["sasb_industry"]) != (item["sector"],item["industry"]):
            raise ValueError("COMPANY_MASTER_CONFLICT")
        current["aliases"].append(alias)
    return {"companies":companies,"alias_to_code":{a:r["canonical_code"] for a,r in mapping.items()},
            "source_hashes":{str(p.resolve()):file_hash(p) for p in paths},
            "population_status":"REGISTERED_MASTER_NOT_VERIFIED_ALL_HKEX_2025"}


def name_key(value):
    text = str(value).lower().replace("limited","ltd")
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]","",text)


def identify(source_name, cover_text, master):
    match = re.match(r"^(\d{5})(?:_|\b)",Path(source_name).stem)
    code = master["alias_to_code"].get(match[1]) if match else None
    if not code:
        return {"stock_code":None,"status":"UNRESOLVED_IDENTITY","reason":"文件名代码未匹配公司底册；不猜测公司。"}
    company = master["companies"][code]
    candidate = name_key(company["company_name"])
    name_ok = len(candidate)>=6 and candidate in name_key(cover_text)
    disclosed = re.findall(r"(?:stock\s*(?:code|symbol)|股份代[碼码]|股票代[碼码])\s*[:：]?\s*0*(\d{1,5})\b",cover_text,re.I)
    codes = {master["alias_to_code"].get(x.zfill(5),x.zfill(5)) for x in disclosed}
    status = "MATCHED" if (name_ok or codes=={code}) and (not codes or codes=={code}) else "IDENTITY_REVIEW_REQUIRED"
    if match[1]!=code:status="HISTORICAL_CODE_DATE_UNVERIFIED"
    return {**company,"status":status,"reason":"文件代码与封面公司名称/明确证券代码交叉匹配。" if status=="MATCHED" else "文件名仅作线索，封面身份未充分核实或代码冲突。"}
