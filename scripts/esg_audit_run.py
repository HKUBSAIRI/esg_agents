"""B-only CLI. Fake mode is explicit and cannot certify A contract compatibility."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from esg_semantic.audit.common import RESOURCE_DIR, canonical, read_json
from esg_semantic.audit.external_contracts import ExternalContracts
from esg_semantic.audit.integration import integration_context
from esg_semantic.auditor import run_audit_file


def fake_client():
    source = ROOT / "tests" / "fixtures" / "audit" / "fake_client.py"
    spec = importlib.util.spec_from_file_location("b_audit_fixture_client", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeClient()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if callable(getattr(stream, "reconfigure", None)):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="开发者B：独立审查Agent，无网站/数据库；正式运行须绑定A契约。")
    parser.add_argument("--pid")
    parser.add_argument("--input-dir", type=Path, help="包含A的JSON/JSONL文件的目录")
    parser.add_argument("--policy", type=Path, default=RESOURCE_DIR / "policy.json")
    parser.add_argument("--catalogue", type=Path, help="indicator_id到定义/variables的映射；正式内容由A提供")
    parser.add_argument("--llm", default="fake", help="fake或A桥接工厂module:function；工厂返回{llm,...integration bindings}")
    parser.add_argument("--contracts", type=Path, help="A提供的正式Schema目录，支持b_contract_map.json文件名映射")
    parser.add_argument("--fixture-mode", action="store_true", help="明确标记未通过A公共契约的夹具运行")
    parser.add_argument("--demo", action="store_true", help="复制内置合成夹具并运行，无需密钥")
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true", help="只显示输入/输出计划，不写日志、不渲染、不调模型")
    args = parser.parse_args(argv)
    if args.demo and args.dry_run:
        parser.error("--demo会复制夹具；dry-run请用 --input-dir tests/fixtures/audit/normal --dry-run")
    try:
        fixture = args.fixture_mode or args.demo
        directory = args.input_dir or (ROOT / "intermediate" / "esg_extract" / args.pid if args.pid else None)
        if args.demo:
            if args.llm != "fake" or args.contracts:
                raise ValueError("合成演示只使用fake，不冒充正式A契约运行。")
            directory = ROOT / "data" / "b_audit_demo" / ("demo_" + uuid.uuid4().hex[:12])
            directory.mkdir(parents=True)
            fixture_dir = ROOT / "tests" / "fixtures" / "audit" / "normal"
            for source in fixture_dir.iterdir():
                if source.is_file(): shutil.copy2(source, directory / source.name)
        if directory is None:
            parser.error("请提供 --pid、--input-dir 或 --demo")
        catalogue = read_json(args.catalogue.read_text(encoding="utf-8-sig")) if args.catalogue else {}
        if args.demo:
            catalogue = read_json((ROOT / "tests" / "fixtures" / "audit" / "catalogue.json").read_text(encoding="utf-8"))
        bindings = {"fixture": fixture, "catalogue": catalogue}
        client = None
        if not args.dry_run:
            if args.llm == "fake":
                if not fixture:
                    raise ValueError("fake仅限显式 --fixture-mode 或 --demo；不用于真实模型验收。")
                client = fake_client()
            else:
                if fixture:
                    raise ValueError("fixture模式不加载真实模型桥接，防止误发送合成/未验证输入。")
                module_name, separator, name = args.llm.partition(":")
                if not separator:
                    raise ValueError("真实客户端需指定A桥接工厂module:function")
                config = getattr(importlib.import_module(module_name), name)()
                config = dict(config)
                client = config.pop("llm")
                bindings.update(config)
            if args.contracts:
                contracts = ExternalContracts(args.contracts)
                bindings.update(validator=contracts, contract_fingerprint=contracts.fingerprint)
        if args.dry_run:
            result = run_audit_file(directory, policy_path=args.policy, llm=None, run_id=args.run_id, dry_run=True)
        else:
            with integration_context(**bindings):
                result = run_audit_file(directory, policy_path=args.policy, llm=client, run_id=args.run_id)
        print(canonical(result if args.dry_run else {k: result[k] for k in ("audit_run_id", "pid", "status", "stats", "coverage")}))
        if fixture and not args.dry_run:
            print("\n仅合成夹具验证：未接入A正式契约、未调用真实模型，不能用于正式入库或评分。")
        if not args.dry_run:
            print("结果目录：", directory.resolve() / "audit_runs" / result["audit_run_id"])
        if result.get("status") == "FAILED":
            return 1
        # PARTIAL fixture is intentional; formal partial must be visible to automation.
        return 2 if not fixture and result.get("status") == "PARTIAL" else 0
    except Exception as exc:
        code = getattr(exc, "code", "ERR_AUDIT_STARTUP")
        # Exception messages from a third-party client factory may contain secrets; don't echo them.
        print(f"{code}: 审查未完成。请检查输入、依赖及A契约/客户端配置；未将失败当作通过。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
