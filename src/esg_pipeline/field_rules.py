"""Business semantics from the workbook, shared by A, B and annual admission."""
from decimal import Decimal
import re

from .validation import normalize

NEGATIVE = r"capacity|capacities|target|intensity|per\b|/|increase|growth|remov|reduction|concentration|能力|目标|目標|强度|強度|去除|削减|削減|浓度|濃度|同比|節水|节水"
CURRENCIES = {"RMB":"CNY","CNY":"CNY","HKD":"HKD","HK$":"HKD","USD":"USD","US$":"USD","EUR":"EUR","人民币":"CNY","人民幣":"CNY","港元":"HKD","美元":"USD","欧元":"EUR"}


def semantic_issues(field, label, context):
    issues = []
    if re.search(NEGATIVE,label,re.I): issues.append("NOT_ACTUAL_ABSOLUTE_OR_DISCLOSED_RATE")
    if field=="water_consumption_m3" and not re.search(r"consumptive|not returned|not discharged|evaporat|incorporated into products|未返回|蒸发|蒸發|进入产品|淨耗水|净耗水",context,re.I):
        issues.append("CONSUMPTION_DEFINITION_UNPROVEN")
    if field=="water_withdrawal_m3" and not re.search(r"withdraw|intake|abstract|取水|取用水",label,re.I):
        if not re.search(r"(?:purchased|municipal|external|meter).{0,70}water|外部.*供水|购水.*水表",context,re.I):
            issues.append("WITHDRAWAL_DEFINITION_UNPROVEN")
    if field in ("cod_discharge_t","ammonia_n_discharge_t","total_n_discharge_t","total_p_discharge_t"):
        if not re.search(r"discharg|effluent|pollutant emissions|排放|出水",context,re.I): issues.append("POLLUTANT_DISCHARGE_UNPROVEN")
        if re.search(r"remov|reduction|去除|削减|削減",label,re.I): issues.append("REMOVAL_IS_NOT_DISCHARGE")
        if re.search(r"third.party.*(?:sewage|wastewater)|customers.*wastewater|外部客户|外部客戶|受托处理|受託處理",context,re.I):
            issues.append("CLIENT_TREATMENT_LOAD_NOT_OWN_OPERATIONS")
    if field=="water_recycled_reused_m3" and re.search(r"purchased.*(?:recycled|reclaimed)|外购再生水|外購再生水",label,re.I):issues.append("PURCHASED_RECLAIMED_WATER_NOT_INTERNAL_REUSE")
    if field=="water_recycling_rate_pct" and re.search(r"treatment|compliance|达标|達標|污水处理率",label,re.I):issues.append("WRONG_RATIO_DEFINITION")
    return list(dict.fromkeys(issues))


def money(value, unit):
    text = str(unit or "")
    found = {canonical for token,canonical in CURRENCIES.items() if token in text or token in text.upper()}
    if len(found)!=1:return None,None,"CURRENCY_UNRESOLVED"
    currency = next(iter(found))
    low = text.lower()
    if re.search(r"billion|十亿",low):factor=Decimal(1000)
    elif re.search(r"million|百万|百萬",low):factor=Decimal(1)
    elif "亿元" in text or "億元" in text:factor=Decimal(100)
    elif re.search(r"thousand|['’]000|千元",low):factor=Decimal("0.001")
    elif "万元" in text or "萬元" in text:factor=Decimal("0.01")
    elif re.fullmatch(r"(?:RMB|CNY|HKD|HK\$|USD|US\$|EUR|人民币|人民幣|港元|美元|欧元)(?:\s*(?:yuan|元))?",text,re.I):factor=Decimal("0.000001")
    else:return None,currency,"MONEY_MAGNITUDE_UNRESOLVED"
    return format(Decimal(str(value))*factor,"f"),currency,None


def standardize(field,value,unit,context=""):
    if field=="revenue_million":
        amount,currency,error=money(value,unit)
        return amount,"million",{"currency":currency,"error":error,"approximate":False}
    target = "%" if field=="water_recycling_rate_pct" else "t" if field.endswith("_t") else "m3"
    text = str(unit or "").strip()
    aliases = {"kL":"m3","GL":"million_m3","万m³":"ten_thousand_m3","万立方米":"ten_thousand_m3","百万m³":"million_m3","百万立方米":"million_m3","g":"grams","万t":"ten_thousand_t","万吨":"ten_thousand_t"}
    extra = {"million_m3":("m3",Decimal(1000000)),"ten_thousand_m3":("m3",Decimal(10000)),"grams":("t",Decimal("0.000001")),"ten_thousand_t":("t",Decimal(10000))}
    key=aliases.get(text,text)
    if key in extra and extra[key][0]==target:
        return format(Decimal(str(value))*extra[key][1],"f"),target,{"error":None,"factor":str(extra[key][1]),"approximate":False}
    if target=="m3" and text.lower() in ("tonnes","tonne","t","吨","噸"):
        if re.search(r"fresh\s*water|ordinary water|淡水|普通水",context,re.I) and not re.search(r"seawater|brine|海水|卤水|鹵水",context,re.I):
            return str(value),target,{"error":None,"factor":"1","approximate":True,"rule":"ORDINARY_FRESHWATER_TONNE_APPROX"}
        return None,target,{"error":"WATER_DENSITY_CONDITION_UNPROVEN","approximate":False}
    if target=="%" and text in ("fraction","0-1比例"):
        if not 0<=Decimal(str(value))<=1:return None,target,{"error":"RATIO_RANGE_INVALID","approximate":False}
        return format(Decimal(str(value))*100,"f"),target,{"error":None,"factor":"100","approximate":False}
    result,_,rule=normalize(value,key,target)
    return result,target,{**rule,"error":None if result is not None else "UNIT_UNRESOLVED","approximate":False}
