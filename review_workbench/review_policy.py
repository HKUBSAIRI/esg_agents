"""Human review requires a concrete issue; incomplete automation is a separate state."""
from decimal import Decimal, InvalidOperation
VERSION="ACTIONABLE-REVIEW-2.0"
GENERIC_FINDINGS={"A-EVIDENCE-WEAK","A-CONTEXT-INSUFFICIENT","A-TRACE-MISSING","A-BOUNDARY-MISSING","A-CLOSED-SET-GAP","A-MISSING-KEY"}
QUESTIONS={
    "A-IND-MISMATCH":"核对原文对应的指标，确认是否归入了错误议题。",
    "A-VAR-MISMATCH":"核对原文名称和定义，确认是否归入了错误变量。",
    "A-SCOPE-MIX":"核对集团、业务或设施范围，确认是否混用了统计范围。",
    "A-UNIT-MISMATCH":"对照原表单位及数量级，确认单位与换算是否正确。",
    "A-YEAR-MISMATCH":"对照原表年份列和报告期，确认数值所属年度。",
    "A-VALUE-IMPLAUSIBLE":"核对原表数值、正负号及数量级，确认异常数值。",
    "A-CROSS-RECORD":"核对相互冲突的记录，确认年度、范围和计量口径是否相同。",
    "A-DUPLICATE-UNMERGED":"核对重复记录及来源，确认是否重复统计。",
    "A-NORMALIZATION-INVALID":"核对原始值、单位及换算因子，确认标准化计算。",
    "A-EXTRACT-VALUE":"对照原表对应行列，确认提取数值是否与原文一致。",
    "A-EXTRACT-CONTEXT":"核对引用的标题、表头和范围说明是否对应本条记录。",
    "A-EXTRACT-PAGE":"核对引用页码，确认正确证据所在页。",
    "A-FILTER-OVERDROP":"核对被过滤的证据，确认是否误删了有效披露。",
    "A-MISSING-KEY":"核对给定原文中的指标，确认是否已有披露但遗漏提取。",
    "A-BOUNDARY-MISSING":"核对原文与记录中的统计边界差异。",
    "A-TRACE-MISSING":"核对指出的引用位置与原文实际位置差异。",
    "A-EVIDENCE-WEAK":"核对指出的原文与记录具体差异。",
    "A-CONTEXT-INSUFFICIENT":"核对引用上下文与记录之间已指出的差异。",
    "A-CLOSED-SET-GAP":"核对证据定义与当前指标映射的具体差异。"}


def actionable(finding):
    if finding.get("severity") not in {"BLOCK","WARN"} or not str(finding.get("reason") or "").strip():return False
    comparison=(finding.get("observed") is not None and finding.get("expected") is not None
                and finding["observed"]!=finding["expected"])
    if (any(text in finding["reason"] for text in ("未独立核对","尚未核实","未完成核验","未执行原页"))
            and not comparison and not finding.get("evidence_span")):return False
    if finding.get("code") in GENERIC_FINDINGS:
        # Generic uncertainty is not evidence of a data error. Require an
        # explicit source span and a specific observed/expected discrepancy.
        return bool(finding.get("evidence_span") and comparison)
    return finding.get("code") in QUESTIONS


def plan(bundle,result):
    manual=[];unverified=[]
    findings=[f for f in result.get("findings",[]) if actionable(f)]
    for record in bundle["records"]:
        rid=record["record_uid"]
        own=[f for f in findings if f.get("record_uid")==rid]
        if not own:
            verdict=next((v.get("verdict") for v in result.get("record_verdicts",[]) if v.get("record_uid")==rid),"NOT_REVIEWED")
            if verdict!="CONFIRMED" and record.get("numeric_audit_applicability")!="NOT_APPLICABLE":unverified.append(rid)
            continue
        codes=list(dict.fromkeys(f["code"] for f in own))
        manual.append({"record_uid":rid,"indicator_id":record["indicator_id"],"finding_ids":[f["finding_id"] for f in own],
            "review_status":next((v["verdict"] for v in result["record_verdicts"] if v["record_uid"]==rid),"INSUFFICIENT"),
            "title":QUESTIONS.get(codes[0],"核对证据中指出的具体差异。"),
            "review_reason":"；".join(dict.fromkeys(str(f["reason"]) for f in own)),
            "review_question":"；".join(dict.fromkeys(QUESTIONS.get(c,"核对原文中的明确差异。") for c in codes)),
            "review_evidence":[{"code":f["code"],"page":f.get("page_no"),"quote":f.get("evidence_span"),
                "observed":f.get("observed"),"expected":f.get("expected")} for f in own]})
    for f in findings:
        if f.get("record_uid") is None:
            manual.append({"record_uid":None,"indicator_id":f.get("indicator_id") or "报告级检查",
                "finding_ids":[f["finding_id"]],"review_status":"INSUFFICIENT","title":"报告级具体问题",
                "review_reason":f["reason"],"review_question":QUESTIONS.get(f["code"],"对照引用证据核对明确差异。")})
    groups={}
    for record in bundle['records']:
        if (record.get('scope_type')!='GROUP' or not record.get('profile_field')
                or record.get('year') is None or not record.get('unit') or record.get('normalized_value') is None):continue
        try:value=Decimal(str(record['normalized_value']))
        except InvalidOperation:continue
        if not value.is_finite():continue
        key=(record.get('pid'),record['profile_field'],record['year'],record['unit'],record.get('period_start'),record.get('period_end'))
        groups.setdefault(key,[]).append((record,value))
    for group in groups.values():
        if len({value for _,value in group})<2:continue
        ids={record['record_uid'] for record,_ in group}
        if any(t.get('record_uid') in ids for t in manual):continue
        first=group[0][0]
        detail='；'.join(f"{r.get('normalized_value')}（引用第{r.get('source_page','未知')}页）" for r,_ in group)
        manual.append({'record_uid':first['record_uid'],'indicator_id':first['indicator_id'],'finding_ids':[],
            'review_status':'SUSPECT','title':'同口径候选数值冲突','related_record_uids':sorted(ids),
            'review_reason':f"同一年度、集团范围及原始单位的同一指标出现不同候选值：{detail}。不能直接选择其中一个。",
            'review_question':'对照这些记录的原页、年度列及集团边界，确认是否存在提取、归类或范围差异。'})
        unverified=[rid for rid in unverified if rid not in ids]
    return manual,{"version":VERSION,"manual_records":len(manual),"unverified_records":unverified,
        "system_problems":[p for p in result.get("b_report",{}).get("problems",[])],
        "note":"待自动核验和技术状态不自动转人工；不进入人工队列不代表数值已核实或可以评分。"}
