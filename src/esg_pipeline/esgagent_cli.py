"""Unified CLI; new EsgAgent is the default and never silently uses legacy A."""
import argparse
import json
from pathlib import Path

from esg_extraction.extractor import atomic_json,file_hash
from .esgagent_core import ROOT,delivered_reports
from .esgagent_pipeline import run_esgagent_pipeline


def main(argv=None):
    parser=argparse.ArgumentParser(description="新版EsgAgent → B审查 → 工作台 → 年度研究草稿")
    parser.add_argument("--input",type=Path,help="PDF路径；不提供时使用随新版交付的3份缓存报告")
    parser.add_argument("--company",help="5位股票代码；文件名不能识别时填写")
    parser.add_argument("--year",type=int,default=2025)
    parser.add_argument("--mode",choices=("rules_only","model"),default="rules_only",help="rules_only只复用新版缓存，不运行旧提取器")
    parser.add_argument("--allow-external",action="store_true")
    parser.add_argument("--a-calls",type=int,default=4)
    parser.add_argument("--b-calls",type=int,default=0)
    parser.add_argument("--output",type=Path,default=ROOT/"data/esgagent_pipeline")
    parser.add_argument("--database",type=Path,default=ROOT/"data/review_workbench")
    parser.add_argument("--plan",action="store_true")
    parser.add_argument("--open-results",action="store_true")
    args=parser.parse_args(argv)
    if args.mode=="model" and (not args.allow_external or not args.input):parser.error("模型模式须指定单份--input，并显式--allow-external")
    if not 0<=args.a_calls<=20 or not 0<=args.b_calls<=20:parser.error("调用上限须为0至20")
    sources=delivered_reports()
    if args.input:
        source=args.input.resolve()
        if not source.is_file():parser.error("PDF不存在")
        sources=[{"_source":str(source),"_sha256":file_hash(source),"name":source.name}]
    if args.plan:
        print(json.dumps({"engine":"esgagent-v1","reports":[s["name"] for s in sources],"mode":args.mode,"new_calls":0},ensure_ascii=True));return 0
    args.output.mkdir(parents=True,exist_ok=True)
    index=args.output/"latest_run.json"
    previous=json.loads(index.read_text(encoding="utf-8"))["reports"] if index.is_file() else []
    if any(r.get("engine")!="esgagent-v1" for r in previous):
        parser.error("该目录包含历史旧版结果；请指定新的输出目录，避免覆盖历史记录。")
    for source in sources:
        summary=run_esgagent_pipeline(source,args.output,year=source.get("report_year",args.year),company_code=args.company or source.get("company_code"),
            allow_external=args.mode=="model",a_calls=args.a_calls,b_calls=args.b_calls,database=args.database,
            runtime=args.database/"esgagent_runtime",
            progress=lambda stage,detail:print(json.dumps({"stage":stage,"message":detail.get("message")},ensure_ascii=True),flush=True))
        previous=[r for r in previous if r["source_sha256"]!=summary["source_sha256"]]+[summary]
        atomic_json(index,{"engine":"esgagent-v1","reports":previous})
        print(json.dumps({"source":source["name"],"run_id":summary["workbench_run_id"],"usage":summary["usage"],"reused":summary["reused_extraction"]},ensure_ascii=True),flush=True)
    if args.open_results:
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
    return 0
