"""SQLite repository. Input/result snapshots are append-only; reviews use version CAS."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import uuid

from .auditor import now
from .contracts import canonical, digest, loads


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "workbench.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS bundles (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, is_demo INTEGER NOT NULL,
                created_at TEXT NOT NULL, pdf_name TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, bundle_id TEXT NOT NULL REFERENCES bundles(id),
                provider TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'OPEN', version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                run_id TEXT NOT NULL REFERENCES runs(id), payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS tasks_run ON tasks(run_id);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
            CREATE INDEX IF NOT EXISTS bundles_pdf ON bundles(pdf_name);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE runs SET status='INTERRUPTED', error='PROCESS_INTERRUPTED' WHERE status='RUNNING'")

    def import_bundle(self, bundle, is_demo=False):
        # Demo identity is not a caller-controlled field inside a bundle.
        identity = digest({"bundle": bundle, "is_demo": is_demo})
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO bundles(id,payload,is_demo,created_at) VALUES(?,?,?,?)",
                       (identity, canonical(bundle), int(is_demo), now()))
        return identity

    def get_bundle(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM bundles WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise NotFound("未找到导入数据")
        return {**dict(row), "payload": loads(row["payload"])}

    def attach_pdf(self, identity, filename):
        with self.connect() as db:
            row = db.execute("SELECT pdf_name FROM bundles WHERE id=?", (identity,)).fetchone()
            if not row:
                raise NotFound("未找到导入数据")
            if row["pdf_name"] and row["pdf_name"] != filename:
                raise Conflict("输入已有不同 PDF 附件；不覆盖原附件")
            db.execute("UPDATE bundles SET pdf_name=? WHERE id=?", (filename, identity))

    def create_run(self, identity, provider):
        self.get_bundle(identity)
        run_id = "ar_" + uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,bundle_id,provider,status,created_at) VALUES(?,?,?,'RUNNING',?)",
                       (run_id, identity, provider, now()))
        return run_id

    def progress(self, run_id, done, total):
        with self.connect() as db:
            db.execute("UPDATE runs SET progress=?,total=? WHERE id=?", (done, total, run_id))

    def complete(self, run_id, result, tasks):
        with self.connect() as db:
            cursor = db.execute("UPDATE runs SET result=?,status=? WHERE id=? AND status='RUNNING' AND result IS NULL",
                                (canonical(result), result["status"], run_id))
            if cursor.rowcount != 1:
                raise Conflict("审查结果已写入或任务已结束，不可覆盖")
            for task in tasks:
                db.execute("INSERT INTO tasks(id,run_id,payload) VALUES(?,?,?)", (task["task_id"], run_id, canonical(task)))

    def fail(self, run_id, error, tasks=()):
        with self.connect() as db:
            cursor = db.execute("UPDATE runs SET status='FAILED',error=? WHERE id=? AND result IS NULL AND status='RUNNING'", (error, run_id))
            if cursor.rowcount:
                for task in tasks:
                    db.execute("INSERT INTO tasks(id,run_id,payload) VALUES(?,?,?)", (task["task_id"], run_id, canonical(task)))

    def list_runs(self):
        with self.connect() as db:
            rows = db.execute("SELECT r.*, b.payload,b.is_demo FROM runs r JOIN bundles b ON r.bundle_id=b.id ORDER BY r.created_at DESC LIMIT 100").fetchall()
        runs = []
        for row in rows:
            bundle = loads(row["payload"])
            report = {}
            if row["provider"].startswith("b_"):
                raw_report = bundle.get("b_source_files", {}).get("audit_report.json")
                if raw_report:
                    report = loads(raw_report)
            runs.append({"run_id": row["id"], "bundle_id": row["bundle_id"], "provider": row["provider"], "status": row["status"],
                         "created_at": row["created_at"], "is_demo": bool(row["is_demo"]),
                         "execution_mode": bundle.get("b_execution_mode") or report.get("execution_mode"),
                         "model_records_reviewed": report.get("stats", {}).get("records_reviewed"),
                         "report_context": bundle["report_context"]})
        return runs

    def detail(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise NotFound("未找到审查运行")
            tasks = db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
            events = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        bundle = self.get_bundle(row["bundle_id"])
        return {"run_id": run_id, "bundle_id": row["bundle_id"], "provider": row["provider"], "status": row["status"],
                "created_at": row["created_at"], "progress": row["progress"], "total": row["total"], "error": row["error"],
                "is_demo": bool(bundle["is_demo"]), "has_pdf": bool(bundle["pdf_name"]),
                "pdf_sha256": bundle["pdf_name"][:-4] if bundle["pdf_name"] else None, "bundle": bundle["payload"],
                "result": loads(row["result"]) if row["result"] else None,
                "tasks": [{**loads(t["payload"]), "state": t["state"], "version": t["version"]} for t in tasks],
                "events": [{**loads(e["payload"]), "seq": e["seq"], "created_at": e["created_at"]} for e in events]}

    def review_for_export(self,run_id):
        """Carry unresolved human blocks across reruns of identical PDF bytes."""
        current=self.detail(run_id)
        if not current["pdf_sha256"]:return current
        with self.connect() as db:
            related=db.execute("""SELECT t.payload,t.state,t.run_id FROM tasks t
                JOIN runs r ON r.id=t.run_id JOIN bundles b ON b.id=r.bundle_id
                WHERE b.pdf_name=? AND t.run_id<>? AND t.state IN ('REAUDIT_REQUIRED','EXCLUDED_FROM_SCORING')""",
                (current["pdf_sha256"]+".pdf",run_id)).fetchall()
        current["tasks"].extend({**loads(t["payload"]),"state":t["state"],"review_origin_run_id":t["run_id"]} for t in related)
        current["inherited_block_run_ids"]=sorted({t["run_id"] for t in related})
        return current

    def review(self, run_id, task_id, decision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE run_id=? AND id=?", (run_id, task_id)).fetchone()
            if not task:
                raise NotFound("未找到复核任务")
            if task["version"] != decision["expected_version"]:
                raise Conflict("此任务已更新，请刷新后重新核对")
            if task["state"] in ("REAUDIT_REQUIRED", "EXCLUDED_FROM_SCORING"):
                raise Conflict("此任务已形成修订/排除请求；请在新审查运行处理，不可直接改为通过")
            action = decision["action"]
            state = {"KEEP": "RESOLVED", "REMAP": "REAUDIT_REQUIRED", "CORRECT_VALUE": "REAUDIT_REQUIRED",
                     "EXCLUDE_FROM_SCORING": "EXCLUDED_FROM_SCORING", "DEFER": "OPEN"}[action]
            db.execute("UPDATE tasks SET state=?,version=version+1 WHERE id=? AND version=?",
                       (state, task_id, decision["expected_version"]))
            payload = {**decision, "task_id": task_id, "run_id": run_id, "resulting_state": state,
                       "event_type": "RECLASSIFY_AND_REAUDIT" if action in ("REMAP", "CORRECT_VALUE") else "HUMAN_REVIEW"}
            db.execute("INSERT INTO events(task_id,run_id,payload,created_at) VALUES(?,?,?,?)",
                       (task_id, run_id, canonical(payload), now()))
        return payload
