"""Local-only HTTP adapter. Replace this module when integrating a production API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import hashlib
import secrets
import time
from urllib.parse import urlsplit, parse_qs, unquote

from .contracts import ContractError, SCHEMAS, canonical, loads
from .store import Conflict, NotFound

STATIC = Path(__file__).parent / "web"
MAX_BODY = 8 * 1024 * 1024


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app):
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.app.pipeline.recover_interrupted()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Do not log URLs, query strings, request bodies, or model credentials.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send(self, code, data, content_type="application/json; charset=utf-8", attachment=None):
        raw = data if isinstance(data, bytes) else canonical(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(raw)

    def reject(self,code,payload):
        # On Windows, closing a socket with an unread small POST body can reset
        # the connection before the client receives the explanatory 403.
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:length=0
        if self.command=='POST' and 0<length<=64*1024*1024:
            deadline=time.monotonic()+2
            try:
                self.connection.settimeout(1)
                while length>0 and time.monotonic()<deadline:
                    chunk=self.rfile.read(min(length,65536))
                    if not chunk:break
                    length-=len(chunk)
            except OSError:pass
            finally:self.connection.settimeout(15)
        self.send(code,payload)

    def guard(self, write=False):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self.reject(403, {"error": "HOST_NOT_ALLOWED", "message": "仅允许本机访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed_hosts}:
            self.reject(403, {"error": "ORIGIN_NOT_ALLOWED", "message": "请求来源不符"})
            return False
        if write and not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
            self.reject(403, {"error": "TOKEN_INVALID", "message": "页面已过期，请刷新重试"})
            return False
        return True

    def do_GET(self):
        if self.guard():
            self.dispatch(False)

    def do_POST(self):
        if self.guard(True):
            self.dispatch(True)

    def dispatch(self, write):
        try:
            self.route(write)
        except ContractError as exc:
            self.send(400, {"error": "CONTRACT_INVALID", "message": str(exc)})
        except Conflict as exc:
            self.send(409, {"error": "CONFLICT", "message": str(exc)})
        except NotFound as exc:
            self.send(404, {"error": "NOT_FOUND", "message": str(exc)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self.send(500, {"error": "INTERNAL_ERROR", "message": "操作未完成，原始数据已保留；请重试或重启工作台"})

    def body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ContractError("请求必须使用 application/json")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ContractError("请求长度无效") from None
        if not 0 <= length <= MAX_BODY:
            raise ContractError("JSON 文件不能超过 8 MB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ContractError("请求未接收完整")
        try:
            return loads(raw.decode("utf-8-sig"))
        except UnicodeError:
            raise ContractError("文件须为 UTF-8 JSON") from None

    def route(self, write):
        path = urlsplit(self.path).path
        app = self.server.app
        if not write:
            if path in ("/", "/app.js", "/pipeline.js", "/batch.js", "/styles.css"):
                filename, mime = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                                  "/pipeline.js": ("pipeline.js", "text/javascript"),
                                  "/batch.js": ("batch.js", "text/javascript"),
                                  "/styles.css": ("styles.css", "text/css")}[path]
                self.send(200, (STATIC / filename).read_bytes(), mime + "; charset=utf-8")
                return
            if path == "/api/bootstrap":
                self.send(200, {"token": self.server.token, "process_id": os.getpid(), "http_model_configured": app.http_provider is not None,
                                "b_audit": app.b.capabilities(),
                                "pipeline": app.pipeline.capabilities(),
                                "model": app.http_provider.model if app.http_provider else None,
                                "runs": app.store.list_runs()})
                return
            if path == "/api/example":
                from .service import demo_bundle
                self.send(200, demo_bundle(), attachment="example.bundle.json")
                return
            if path.startswith("/api/schemas/"):
                key = path.rsplit("/", 1)[1].replace(".schema.json", "")
                if key not in SCHEMAS:
                    raise NotFound("未找到 Schema")
                self.send(200, {"$schema": "https://json-schema.org/draft/2020-12/schema", **SCHEMAS[key]},
                          attachment=key + ".schema.json")
                return
            parts = path.strip("/").split("/")
            if path == "/api/pipeline/reports":
                self.send(200, {"reports":app.pipeline.public_reports()}); return
            if path == "/api/pipeline/jobs":
                self.send(200, {"jobs":app.pipeline.jobs()}); return
            if path == "/api/pipeline/batches":
                self.send(200,{"batches":app.pipeline.batches.list()});return
            if len(parts)>=4 and parts[:3]==["api","pipeline","batches"]:
                if len(parts)==4:
                    self.send(200,app.pipeline.batches.public(parts[3]));return
                if len(parts)==5 and parts[4]=="export.xlsx":
                    self.send(200,app.pipeline.batches.export(parts[3]),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","batch-water-2025.xlsx");return
                if len(parts)==5 and parts[4]=="receipt.json":
                    self.send(200,app.pipeline.batches.public(parts[3]),"application/json","batch-receipt.json");return
            if path == "/api/water/collection":
                self.send(200, app.pipeline.annual()); return
            if path == "/api/water/2025.xlsx":
                all_companies=parse_qs(urlsplit(self.path).query).get("all_companies",["0"])[0]=="1"
                self.send(200, app.pipeline.annual_export(all_companies=all_companies),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "water-2025.xlsx"); return
            if len(parts) == 4 and parts[:3] == ["api", "pipeline", "jobs"]:
                self.send(200, app.pipeline.public_job(parts[3])); return
            if len(parts) >= 4 and parts[:3] == ["api", "pipeline", "runs"]:
                if len(parts) == 4:
                    query = parse_qs(urlsplit(self.path).query)
                    try:
                        offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [25])[0])
                    except (TypeError, ValueError):
                        raise ContractError("分页参数无效。") from None
                    self.send(200, app.pipeline.results(parts[3], query.get("tab",["extract"])[0], offset, limit)); return
                if len(parts) == 5 and parts[4] == "export":
                    self.send(200, app.pipeline.export(parts[3]), "application/zip", "esg-full-pipeline.zip"); return
                if len(parts) == 5 and parts[4] == "annual":
                    self.send(200, app.pipeline.annual(parts[3])); return
                if len(parts) == 5 and parts[4] == "water-2025.xlsx":
                    self.send(200, app.pipeline.annual_export(parts[3]),
                              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "water-2025.xlsx"); return
                if len(parts) == 6 and parts[4] == "pages" and parts[5].isdigit():
                    self.send(200, app.pipeline.page_image(parts[3], int(parts[5])), "image/png"); return
            if len(parts) in (3, 4) and parts[:2] == ["api", "runs"]:
                run_id = parts[2]
                if len(parts) == 3:
                    self.send(200, app.detail(run_id))
                    return
                if parts[3] == "export":
                    self.send(200, app.export(run_id), "application/zip", "s4-audit-export.zip")
                    return
                if parts[3] == "pdf":
                    detail = app.detail(run_id)
                    bundle = app.store.get_bundle(detail["bundle_id"])
                    if not bundle["pdf_name"]:
                        raise NotFound("此输入未附加 PDF，当前可查看证据文本")
                    filename = app.store.directory / "pdfs" / bundle["pdf_name"]
                    self.send(200, filename.read_bytes(), "application/pdf")
                    return
        else:
            parts = path.strip("/").split("/")
            if path == "/api/pipeline/upload":
                if self.headers.get("Content-Type", "").split(";")[0] != "application/pdf":
                    raise ContractError("请选择PDF文件。")
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    raise ContractError("PDF长度无效。") from None
                if not 5 <= length <= 64 * 1024 * 1024:
                    raise ContractError("PDF不能超过64 MB。")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ContractError("PDF上传不完整。")
                self.send(201, app.pipeline.upload(raw, unquote(self.headers.get("X-Filename", "report.pdf")))); return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "pdf":
                detail = app.detail(parts[2])
                if detail.get("engine") != "b" or detail["status"] == "RUNNING":
                    raise ContractError("请在B审查完成后附加PDF。")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/pdf":
                    raise ContractError("请选择PDF文件。")
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    raise ContractError("PDF长度无效。") from None
                if not 5 <= length <= 64 * 1024 * 1024:
                    raise ContractError("PDF附件不能超过64 MB。")
                raw = self.rfile.read(length)
                if len(raw) != length or not raw.startswith(b"%PDF-"):
                    raise ContractError("PDF附件无效或上传不完整。")
                name = hashlib.sha256(raw).hexdigest() + ".pdf"
                directory = app.store.directory / "pdfs"
                directory.mkdir(exist_ok=True)
                target = directory / name
                try:
                    with target.open("xb") as output:
                        output.write(raw)
                except FileExistsError:
                    if target.read_bytes() != raw:
                        raise Conflict("PDF附件校验不符。")
                app.store.attach_pdf(detail["bundle_id"], name)
                self.send(201, {"sha256": name[:-4]})
                return
            body = self.body()
            if path == "/api/water/jobs":
                self.send(202,{"job_id":app.pipeline.start_collection(body)}); return
            if path == "/api/pipeline/jobs":
                self.send(202, {"job_id":app.pipeline.start(body)}); return
            if path == "/api/pipeline/batches":
                self.send(202,{"batch_id":app.pipeline.batches.start(body)});return
            if len(parts)==5 and parts[:3]==["api","pipeline","batches"] and parts[4]=="cancel":
                if body!={}:raise ContractError("停止队列不接受其他参数。")
                self.send(200,app.pipeline.batches.cancel(parts[3]));return
            if len(parts) == 5 and parts[:3] == ["api", "pipeline", "jobs"] and parts[4] == "cancel":
                if body != {}:
                    raise ContractError("停止操作不接受其他参数。")
                self.send(200, app.pipeline.cancel(parts[3])); return
            if path in ("/api/b/import", "/api/b/run", "/api/b/demo"):
                if not isinstance(body, dict) or (path != "/api/b/demo" and set(body) != {"files"}) or (path == "/api/b/demo" and body):
                    raise ContractError("B请求需提供files对象；合成演示使用空对象。")
                if path == "/api/b/import":
                    run_id = app.b.import_results(body["files"])
                elif path == "/api/b/run":
                    run_id = app.b.start(body["files"])
                else:
                    run_id = app.b.start(demo=True)
                self.send(202, {"run_id": run_id})
                return
            if path == "/api/demo":
                self.send(202, {"run_id": app.demo()})
                return
            if path == "/api/import":
                self.send(201, {"bundle_id": app.import_data(body)})
                return
            if path == "/api/runs":
                if not isinstance(body, dict) or set(body) != {"bundle_id", "mode"} or not all(isinstance(v, str) for v in body.values()):
                    raise ContractError("需要 bundle_id 和 mode 两个字符串字段")
                self.send(202, {"run_id": app.start(body["bundle_id"], body["mode"])})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "tasks" and parts[5] == "review":
                self.send(200, app.review(parts[2], parts[4], body))
                return
        raise NotFound("未找到此接口")
