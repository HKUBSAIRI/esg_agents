"""Versioned collection contract, read from the user's actual workbook."""
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json
import posixpath
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
NS = {"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
VERSION = "WATER-COLLECTION-2025-1.0"
FIELDS = ("stock_code", "company_name", "sasb_sector", "sasb_industry", "revenue_million", "revenue_currency",
          "water_withdrawal_m3", "water_consumption_m3", "water_recycling_rate_pct", "water_recycled_reused_m3",
          "water_discharge_m3", "cod_discharge_t", "ammonia_n_discharge_t", "total_n_discharge_t", "total_p_discharge_t")
HEADERS = ("股票代码", "公司名称", "SASB Sector", "SASB Industry", "营业收入（百万）", "收入币种", "总取水量（m³）",
           "总耗水量（m³）", "水循环利用率（%）", "循环利用水量（m³）", "总排水量（m³）", "COD 排放量（t）",
           "氨氮排放量（t）", "总氮排放量（t）", "总磷排放量（t）")
TARGETS = dict(zip(FIELDS[6:], ("m3","m3","%","m3","m3","t","t","t","t"))) | {"revenue_million":"million"}
PROJECT_INDICATORS = {"E2.1.1":["water_withdrawal_m3","revenue_million","revenue_currency"],
                      "E2.1.2":[], "E2.1.3":["water_recycled_reused_m3","water_recycling_rate_pct"],
                      "E2.2.2":["cod_discharge_t","ammonia_n_discharge_t","total_n_discharge_t","total_p_discharge_t"]}


def sheets(path):
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(t.text or "" for t in item.findall(".//m:t",NS)) for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si",NS)]
        rels = {r.attrib["Id"]:r.attrib["Target"] for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        result = {}
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("m:sheets/m:sheet",NS):
            target = rels[sheet.attrib[RID]]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/"+target)
            rows = {}
            for row in ET.fromstring(archive.read(target)).findall("m:sheetData/m:row",NS):
                cells = {}
                for cell in row.findall("m:c",NS):
                    value = cell.find("m:v",NS)
                    value = value.text if value is not None else ""
                    if cell.get("t")=="s": value = strings[int(value)]
                    elif cell.get("t")=="inlineStr": value = "".join(t.text or "" for t in cell.findall(".//m:t",NS))
                    cells[cell.attrib["r"]] = value
                rows[int(row.attrib["r"])] = cells
            result[sheet.attrib["name"]] = {"part":target,"rows":rows}
        return result


def contract(path=None):
    path = Path(path or ROOT/"水资源.xlsx")
    book = sheets(path)
    actual = tuple(book["2025"]["rows"][1].get(chr(65+i)+"1", "") for i in range(15))
    if actual != HEADERS:
        raise ValueError("WATER_TEMPLATE_HEADERS_CHANGED")
    definitions = next((s for name,s in book.items() if name!="2025"),None)
    rules = {}
    for number,row in (definitions or {}).get("rows",{}).items():
        if number>1:
            label = row.get(f"B{number}","")
            if "\n" in label: rules[label.splitlines()[-1]] = row.get(f"F{number}","")
    if set(rules) != set(FIELDS):
        raise ValueError("WATER_TEMPLATE_DEFINITIONS_CHANGED")
    return {"version":VERSION,"template_path":str(path.resolve()),"template_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "rules_sheet_name":next(name for name in book if name!="2025"),
            "fields":list(FIELDS),"headers":list(HEADERS),"rules":rules,"project_indicators":PROJECT_INDICATORS,
            "scoring_enabled":False,"ratio_policy":"DISCLOSED_ONLY_PERCENT_NUMBER","currency_policy":"ORIGINAL_CURRENCY_NO_FX"}
