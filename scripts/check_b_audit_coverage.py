"""Fail CI when B's measured implementation coverage falls below the agreed gates."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("report",nargs="?",type=Path,default=Path("data/b_audit_validation/coverage.json"))
    args=parser.parse_args()
    data=json.loads(args.report.read_text(encoding="utf-8"))
    failures=[]
    rows={name.replace("\\","/"):value for name,value in data["files"].items()}
    required={"src/esg_semantic/auditor.py","src/esg_semantic/audit/postprocess.py","src/esg_semantic/audit/files.py",
              "src/esg_semantic/audit/external_contracts.py","src/esg_semantic/audit/visual.py"}
    missing=required-set(rows)
    if missing: failures.append("Modules missing from coverage: "+", ".join(sorted(missing)))
    for name,value in rows.items():
        summary=value["summary"]
        lines=summary["covered_lines"]/max(summary["num_statements"],1)
        if lines<.85: failures.append(f"{name}: line coverage {lines:.1%} < 85%")
        if name.endswith("audit/postprocess.py"):
            branches=summary["covered_branches"]/max(summary["num_branches"],1)
            if branches<.95: failures.append(f"{name}: branch coverage {branches:.1%} < 95%")
    for message in failures: print(message)
    if failures: return 1
    print("B coverage gates passed: all measured core modules >=85% lines; postprocess >=95% branches.")
    return 0


if __name__=="__main__": raise SystemExit(main())
