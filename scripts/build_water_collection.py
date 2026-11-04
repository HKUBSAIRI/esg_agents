"""Rebuild company-year collection from completed A/B runs, with no API calls."""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
sys.path.insert(0,str(ROOT))
from esg_pipeline.annual import build_collection,persist
from esg_pipeline.workbook_export import export_workbook


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    default_index=ROOT/"data/esgagent_pipeline/latest_run.json"
    parser.add_argument("--index",type=Path,default=default_index if default_index.is_file() else ROOT/"data/esg_pipeline/latest_run.json")
    parser.add_argument("--output-dir",type=Path,default=ROOT/"data/water_collection")
    parser.add_argument("--all-companies",action="store_true",help="Keep all registered master companies, including no-report rows")
    parser.add_argument("--xlsx",type=Path,help="Export the 2025 template, without changing its source")
    parser.add_argument("--preview",action="store_true")
    parser.add_argument("--database",type=Path,default=ROOT/"data/review_workbench")
    args=parser.parse_args()
    from review_workbench.store import Store
    review_resolver=Store(args.database).review_for_export if (args.database/"workbench.sqlite3").is_file() else None
    summaries=json.loads(args.index.read_text(encoding="utf-8-sig"))["reports"]
    if args.index.resolve()==default_index.resolve():
        for path in (args.database/"pipeline_jobs").glob("job_*.json"):
            job=json.loads(path.read_text(encoding="utf-8"))
            if job.get("summary",{}).get("engine")=="esgagent-v1":summaries.append(job["summary"])
        summaries=list({s["run_id"]:s for s in summaries}.values())
    collection=build_collection(summaries,all_companies=args.all_companies,review_resolver=review_resolver)
    path=persist(collection,args.output_dir)
    if args.xlsx:export_workbook(collection,args.xlsx,preview=args.preview)
    print(json.dumps({"batch":str(path),"statistics":collection["statistics"],"api_calls":0},ensure_ascii=True))


if __name__=="__main__":main()
