"""命令行入口：PDF 输入 → 入库 → 一次模型调用提取归类 → 导出 CSV。

用法示例：
    .venv/bin/python -m app.cli smoke
    .venv/bin/python -m app.cli extract --pdf "HKEX ESG Reports/华润饮料 2025.pdf" --year 2025
    .venv/bin/python -m app.cli batch --dir "HKEX ESG Reports" --force
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from app.adapters.object_store import LocalObjectStore
from app.adapters.qwen import QwenPDFClient
from app.agents.extraction import ExtractionAgent, stats
from app.core.errors import AppError
from app.core.logging import RunLogger
from app.core.settings import Settings, ensure_dirs
from app.models.contracts import CompanyProfile, CompanyRegistry, ExtractionBatch, MetricDefinition, ReportRef
from app.services.export import (
    build_candidate_rows,
    build_derived_rows,
    build_stage1_row,
    summarize_row,
    write_candidate_csv,
    write_derived_csv,
    write_stage1_csv,
)
from app.services.ingest import IngestService
from app.services.prompt_registry import PromptRegistry
from app.services.skill_loader import SkillLoader


class AppContext:
    def __init__(self, settings: Settings) -> None:
        ensure_dirs(settings)
        self.settings = settings
        self.store = LocalObjectStore(settings.objects_dir)
        self.ingest = IngestService(settings, self.store)
        self.skills = SkillLoader(settings.skills_dir)
        self.prompts = PromptRegistry(settings.prompts_dir)
        self.run_logger = RunLogger(settings.log_dir, f"cli_{datetime.now():%Y%m%d_%H%M%S}")
        self.qwen = QwenPDFClient(settings, self.run_logger)
        self.agent = ExtractionAgent(settings, self.store, self.skills, self.prompts, self.qwen, self.run_logger)
        self.registry: dict[str, MetricDefinition] = self.skills.load_registry()
        companies = json.loads((settings.config_dir / "companies.json").read_text(encoding="utf-8"))
        self.companies = CompanyRegistry.model_validate(companies)


def _slug(company: CompanyProfile) -> str:
    return f"{company.hkex_code.split('.')[0]}_{company.name}"


def _export(ctx: AppContext, report: ReportRef, company: CompanyProfile, batch: ExtractionBatch) -> dict:
    out = Path(ctx.settings.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prefix = f"{_slug(company)}_{report.report_year}"
    row = build_stage1_row(report, company, batch, ctx.registry)
    stage1_path = write_stage1_csv([row], out / f"{prefix}_归类结果.csv")
    cand_rows = build_candidate_rows(report, company, batch, ctx.registry)
    cand_path = write_candidate_csv(cand_rows, out / f"{prefix}_候选人明细.csv")
    derived_rows = build_derived_rows(row)
    derived_path = write_derived_csv(derived_rows, out / f"{prefix}_推算值.csv") if derived_rows else None
    json_path = out / f"{prefix}_extraction.json"
    json_path.write_text(batch.model_dump_json(indent=2), encoding="utf-8")
    return {
        "row": row,
        "stage1": stage1_path,
        "candidates": cand_path,
        "batch_json": json_path,
        "candidate_count": len(cand_rows),
        "derived": derived_path,
        "derived_count": len(derived_rows),
    }


def cmd_smoke(ctx: AppContext) -> int:
    print("== Phase-1 冒烟 ==")
    print(f"model        : {ctx.settings.qwen_model}")
    print(f"base_url     : {ctx.settings.dashscope_base_url}")
    print(f"api_key      : {'已配置' if ctx.settings.dashscope_api_key else '缺失'} (len={len(ctx.settings.dashscope_api_key)})")
    print(f"skills       : {len(ctx.registry)} 个指标已登记")
    bundle = ctx.skills.resolve(
        task="extraction", industry_code="fb-nb", metric_ids=list(ctx.registry), max_tokens=24000
    )
    prompt = ctx.prompts.render(
        "extraction",
        version=ctx.settings.prompt_version,
        skill_text=bundle.text,
        report_context={"report_id": "rp_smoke", "company_name": "冒烟测试", "report_year": 2025, "page_count": 1},
        metric_ids=list(ctx.registry),
        schema_version=ctx.settings.schema_version,
        prompt_version=ctx.settings.prompt_version,
        chunk_info=None,
    )
    print(f"skill_bundle : {len(bundle.files)} 文件, digest={bundle.digest[:16]}…, ~{bundle.token_estimate} tokens")
    print(f"prompt       : {len(prompt.text)} 字符, digest={prompt.digest[:16]}…")
    ping = ctx.qwen.ping()
    print(f"qwen ping    : {ping}")
    return 0


def _ingest_and_extract(
    ctx: AppContext,
    pdf: Path,
    company: CompanyProfile,
    year: int,
    force: bool,
    pdf_url: str | None = None,
    concurrency: int | None = None,
    progress=None,
):
    report = ctx.ingest.ingest_pdf(pdf, company, year)
    print(f"[入库] {company.name} {year} → report_id={report.report_id} sha256={report.sha256[:12]}… {report.page_count} 页")
    outcome = ctx.agent.extract(
        report,
        company,
        force=force,
        file_url=pdf_url,
        concurrency=concurrency,
        progress=progress,
    )
    batch = outcome.batch
    for w in outcome.warnings:
        print(f"  ! {w}")
    print(f"[提取] {json.dumps(stats(batch), ensure_ascii=False)}")
    return report, batch


def cmd_extract(ctx: AppContext, args: argparse.Namespace) -> int:
    pdf = Path(args.pdf)
    company = ctx.companies.match(args.company) if args.company else ctx.companies.match(pdf.name)
    if company is None:
        raise AppError("NOT_FOUND", f"无法识别公司，请用 --company 指定（可选：{[c.hkex_code for c in ctx.companies.companies]}）")
    year = args.year or company.default_report_year or 0
    if not year:
        raise AppError("SCHEMA_INVALID", "缺少报告年度，请用 --year 指定")
    if not pdf.exists():
        raise AppError("NOT_FOUND", f"PDF 不存在: {pdf}")

    report, batch = _ingest_and_extract(
        ctx, pdf, company, year, args.force, args.pdf_url, args.concurrency,
        progress=lambda msg: print(msg, flush=True),
    )
    result = _export(ctx, report, company, batch)
    print("\n" + summarize_row(result["row"]))
    print(f"\n归类结果 CSV   : {result['stage1']}")
    print(f"候选人明细 CSV : {result['candidates']}（{result['candidate_count']} 条）")
    print(f"原始 JSON      : {result['batch_json']}")
    if result.get("derived"):
        print(f"推算值说明 CSV : {result['derived']}（{result['derived_count']} 条，需人工确认）")
    return 0


def cmd_batch(ctx: AppContext, args: argparse.Namespace) -> int:
    root = Path(args.dir)
    pdfs = sorted(root.glob(args.pattern))
    if not pdfs:
        raise AppError("NOT_FOUND", f"{root} 下没有匹配 {args.pattern} 的文件")
    results = []
    failures = []
    for pdf in pdfs:
        company = ctx.companies.match(pdf.name)
        if company is None:
            print(f"[跳过] 无法识别公司: {pdf.name}（可用 --company 单文件处理）")
            continue
        year = args.year or company.default_report_year or 0
        print(f"\n{'=' * 70}\n处理 {pdf.name} → {company.name} ({company.hkex_code}) {year}")
        try:
            report, batch = _ingest_and_extract(
                ctx,
                pdf,
                company,
                year,
                args.force,
                getattr(args, "pdf_url", None),
                getattr(args, "concurrency", None),
                progress=lambda msg: print(f"  {msg}", flush=True),
            )
            results.append(_export(ctx, report, company, batch))
        except AppError as exc:
            failures.append((pdf.name, exc.code.value, exc.message))
            print(f"  [失败] {exc.code.value}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - CLI 兜底，保留 traceback 便于排查
            failures.append((pdf.name, "UNEXPECTED", repr(exc)))
            print(f"  [异常] {exc!r}")
            traceback.print_exc()

    if results:
        combined = Path(ctx.settings.out_dir) / "阶段一_归类结果_汇总.csv"
        write_stage1_csv([r["row"] for r in results], combined)
        print(f"\n{'=' * 70}\n汇总归类结果 CSV: {combined}")
        for r in results:
            print("\n" + summarize_row(r["row"]))
    if failures:
        print("\n失败清单:")
        for name, code, msg in failures:
            print(f"  - {name}: [{code}] {msg}")
        return 1
    return 0


def cmd_export(ctx: AppContext, args: argparse.Namespace) -> int:
    report = ctx.ingest.load_report(args.report_id)
    companies = {c.company_id: c for c in ctx.companies.companies}
    company = companies.get(report.company_id)
    if company is None:
        raise AppError("NOT_FOUND", f"公司主数据缺失: {report.company_id}")
    batch_path = Path(ctx.settings.batches_dir) / f"{report.report_id}.json"
    if not batch_path.exists():
        raise AppError("NOT_FOUND", f"该报告还没有提取批次: {batch_path}")
    batch = ExtractionBatch.model_validate_json(batch_path.read_text(encoding="utf-8"))
    result = _export(ctx, report, company, batch)
    print(summarize_row(result["row"]))
    print(f"归类结果 CSV   : {result['stage1']}")
    print(f"候选人明细 CSV : {result['candidates']}")
    return 0


def cmd_list_reports(ctx: AppContext) -> int:
    reports = sorted(Path(ctx.settings.reports_dir).glob("*.json"))
    if not reports:
        print("还没有已入库报告")
        return 0
    for path in reports:
        report = ReportRef.model_validate_json(path.read_text(encoding="utf-8"))
        company = next((c for c in ctx.companies.companies if c.company_id == report.company_id), None)
        name = company.name if company else report.company_id
        print(f"{report.report_id}  {name}  {report.report_year}  {report.source_filename}  {report.page_count}页")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="esgagent", description="ESG 水资源提取归类 Agent")
    parser.add_argument("--env-file", default=None, help="配置文件路径，默认 config/.env")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("smoke", help="Phase-1 环境冒烟（配置/Skill/Prompt/模型连通性）")

    p_extract = sub.add_parser("extract", help="单个 PDF：入库 + 提取归类 + 导出 CSV")
    p_extract.add_argument("--pdf", required=True)
    p_extract.add_argument("--company", default=None, help="股票代码或公司名，默认从文件名识别")
    p_extract.add_argument("--year", type=int, default=None)
    p_extract.add_argument("--force", action="store_true", help="忽略本地提取缓存，强制重新调用模型")
    p_extract.add_argument("--pdf-url", default=None, help="公网可读的 PDF 签名 URL（大文件 >20MB 时必须，走 file_url 而非 base64）")
    p_extract.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="大 PDF 分块并发数（1-4，默认取 EXTRACT_CHUNK_CONCURRENCY=3）",
    )

    p_batch = sub.add_parser("batch", help="目录批量处理")
    p_batch.add_argument("--dir", default="HKEX ESG Reports")
    p_batch.add_argument("--pattern", default="*.pdf")
    p_batch.add_argument("--year", type=int, default=None)
    p_batch.add_argument("--force", action="store_true")
    p_batch.add_argument("--concurrency", type=int, default=None, help="单份报告内部的分块并发数（1-4）")

    p_export = sub.add_parser("export", help="重新导出已有批次")
    p_export.add_argument("--report-id", required=True)

    sub.add_parser("list-reports", help="列出已入库报告")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ctx = AppContext(Settings.load(args.env_file))
        if args.command == "smoke":
            return cmd_smoke(ctx)
        if args.command == "extract":
            return cmd_extract(ctx, args)
        if args.command == "batch":
            return cmd_batch(ctx, args)
        if args.command == "export":
            return cmd_export(ctx, args)
        if args.command == "list-reports":
            return cmd_list_reports(ctx)
        raise AppError("SCHEMA_INVALID", f"未知命令: {args.command}")
    except AppError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        if exc.details:
            print(f"  details: {exc.details}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
