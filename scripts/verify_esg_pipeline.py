"""Verify current EsgAgent PDF/snapshot integrity without model calls or writes."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',type=Path,default=ROOT/'data/esgagent_pipeline/latest_run.json')
    parser.add_argument('--database',type=Path,default=ROOT/'data/review_workbench')
    args=parser.parse_args(argv)
    from esg_pipeline.esgagent_pipeline import inspect_annual
    summaries=[];failures=[]
    if args.index.is_file():
        summaries=json.loads(args.index.read_text(encoding='utf-8-sig')).get('reports',[])
    for path in (args.database/'pipeline_jobs').glob('job_*.json'):
        try:
            job=json.loads(path.read_text(encoding='utf-8-sig'))
            if job.get('summary') and job.get('result_run_id'):summaries.append(job['summary'])
        except (ValueError,OSError):failures.append({'job':path.name,'error':'UNREADABLE_JOB_METADATA'})
    summaries=list({s['run_id']:s for s in summaries if s.get('engine')=='esgagent-v1'}.values())
    checked=0
    for summary in summaries:
        try:
            inspect_annual(summary)
            checked+=1
        except Exception as exc:
            failures.append({'run_id':summary['run_id'],'error_type':type(exc).__name__})
    result={'status':'FAILED' if failures else 'SUCCEEDED' if summaries else 'NO_RESULTS',
            'checked_reports':checked,'failures':failures,'new_api_calls':0,
            'scope':'PDF and saved snapshot integrity; not accuracy or completeness certification',
            'production_ready':False}
    print(json.dumps(result,ensure_ascii=True))
    return 1 if failures else 0


if __name__=='__main__':raise SystemExit(main())
