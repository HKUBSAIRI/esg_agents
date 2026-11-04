"""python -m review_workbench [serve|import|pack|schemas|check]."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import webbrowser

from .contracts import ContractError, SCHEMAS, canonical, loads, validate_bundle
from .instance_lock import InstanceLock
from .providers import HTTPProvider
from .server import LocalServer
from .service import Application
from .store import Store, Conflict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = ROOT / "data" / "review_workbench"


def read_json(path):
    return loads(Path(path).read_text(encoding="utf-8-sig"))


def load_provider():
    path = ROOT / "review_workbench.local.json"
    config = read_json(path) if path.exists() else {}
    if not isinstance(config, dict) or set(config) - {"endpoint", "api_key", "model"} or not all(isinstance(v, str) for v in config.values()):
        raise ContractError("本机模型配置只接受 endpoint、api_key、model 三个字符串字段")
    endpoint = os.environ.get("ESG_AUDIT_ENDPOINT", config.get("endpoint", ""))
    key = os.environ.get("ESG_AUDIT_API_KEY", config.get("api_key", ""))
    model = os.environ.get("ESG_AUDIT_MODEL", config.get("model", ""))
    if not any((endpoint, key, model)):
        return None
    return HTTPProvider(endpoint, key, model)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESG 水资源 S4 审查工作台（Python 3.10+，无需安装依赖）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="本机审查数据库目录")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-open", dest="open", action="store_false", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="启动本地工作台（默认）")
    check = sub.add_parser("check", help="检查输入文件契约，不调用模型")
    check.add_argument("bundle", type=Path)
    imp = sub.add_parser("import", help="导入 bundle，可显式附加本机 PDF")
    imp.add_argument("bundle", type=Path)
    imp.add_argument("--pdf", type=Path)
    imp.add_argument("--audit", choices=["offline", "http"], default="offline")
    pack = sub.add_parser("pack", help="将同事的 JSONL 与元数据组装为 bundle")
    pack.add_argument("--metadata", required=True, type=Path, help="包含 schema_version/report_context/catalogue/review_policy 的 JSON")
    pack.add_argument("--evidence", required=True, type=Path)
    pack.add_argument("--records", required=True, type=Path)
    pack.add_argument("--out", required=True, type=Path)
    schemas = sub.add_parser("schemas", help="导出公共 JSON Schema")
    schemas.add_argument("--out", type=Path, default=Path("schemas"))
    args = parser.parse_args(argv)
    try:
        if args.command == "schemas":
            args.out.mkdir(parents=True, exist_ok=True)
            for name, schema in SCHEMAS.items():
                target = args.out / (name + ".schema.json")
                target.write_text(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("契约已导出到：", args.out.resolve())
            return 0
        if args.command == "pack":
            bundle = read_json(args.metadata)
            for name in ("evidence", "records"):
                rows = []
                for line_no, line in enumerate(getattr(args, name).read_text(encoding="utf-8-sig").splitlines(), 1):
                    if line.strip():
                        try:
                            rows.append(loads(line))
                        except ContractError as exc:
                            raise ContractError(f"{name} 第 {line_no} 行：{exc}") from exc
                bundle[name] = rows
            validate_bundle(bundle)
            if args.out.exists():
                raise ContractError("输出文件已存在；请选择新路径以保留历史输入")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            print("已生成：", args.out.resolve())
            return 0
        if args.command == "check":
            bundle = validate_bundle(read_json(args.bundle))
            print(f"契约检查通过：{len(bundle['records'])} 条记录，{len(bundle['evidence'])} 条证据；未调用模型。")
            return 0
        store = Store(args.data_dir)
        if args.command == "import":
            bundle = validate_bundle(read_json(args.bundle))
            synthetic = bundle["report_context"].get("extensions", {}).get("is_synthetic") is True
            from .service import demo_bundle
            from .contracts import digest
            identity = store.import_bundle(bundle, synthetic or digest(bundle) == digest(demo_bundle()))
            if args.pdf:
                if not args.pdf.is_file():
                    raise ContractError("指定 PDF 不存在")
                hasher = hashlib.sha256()
                with args.pdf.open("rb") as source:
                    if source.read(5) != b"%PDF-":
                        raise ContractError("附件不是标准 PDF 文件")
                    source.seek(0)
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        hasher.update(block)
                name = hasher.hexdigest() + ".pdf"
                existing = store.get_bundle(identity)["pdf_name"]
                if existing and existing != name:
                    raise Conflict("此输入已附加其他 PDF，不覆盖")
                target = store.directory / "pdfs" / name
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    shutil.copy2(args.pdf, target)
                store.attach_pdf(identity, name)
            app = Application(store, load_provider() if args.audit == "http" else None)
            try:
                run_id = app.start(identity, args.audit)
                app.active.result()
                print("已导入并审查。运行编号：", run_id)
                status = app.detail(run_id)["status"]
                print("启动工作台即可查看。状态：", status)
            finally:
                app.close()
            return 1 if status == "FAILED" else 0
        if not 0 <= args.port <= 65535:
            raise ContractError("端口须在 0–65535 之间")
        try:
            lock = InstanceLock(store.directory)
            lock.__enter__()
        except ValueError as exc:
            raise ContractError(str(exc)) from None
        try:
            store.recover()
            app = Application(store, load_provider())
            try:
                server = LocalServer(("127.0.0.1", args.port), app)
                url = f"http://127.0.0.1:{server.server_port}"
                print("\nESG 水资源审查工作台已启动：", url, flush=True)
                print("演示无需密钥。请保持此窗口打开，按 Ctrl+C 停止。", flush=True)
                if args.open:
                    threading.Timer(.4, lambda: webbrowser.open(url)).start()
                try:
                    server.serve_forever(poll_interval=.2)
                except KeyboardInterrupt:
                    print("\n正在保存并关闭…")
                finally:
                    server.server_close()
            finally:
                app.close()
        finally:
            lock.__exit__(None, None, None)
        return 0
    except (ContractError, Conflict) as exc:
        print("未完成：", str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("无法启动或读写文件：请检查文件路径、权限，或用 --port 8766 更换端口。", file=sys.stderr)
        return 1
    except ValueError:
        print("模型配置无效，请核对本机配置的 HTTPS endpoint、api_key 和 model。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
