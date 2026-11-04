"""并发安全与脱敏测试：分块并行会多线程写同一份 JSONL 运行日志。"""

from __future__ import annotations

import json
import threading

from app.core.logging import JsonlWriter, sanitize


def test_jsonl_writer_thread_safe(tmp_path):
    writer = JsonlWriter(tmp_path / "run.jsonl")
    total_per_worker = 50
    workers = 3

    def worker(worker_id: int) -> None:
        for i in range(total_per_worker):
            writer.write({"event": "test", "worker": worker_id, "i": i})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [line for line in (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == total_per_worker * workers
    records = [json.loads(line) for line in lines]
    assert all(record["event"] == "test" for record in records)
    # 每个 worker 的 50 条都在，没有因为并发丢写
    for worker_id in range(workers):
        assert sorted(r["i"] for r in records if r["worker"] == worker_id) == list(range(total_per_worker))


def test_sanitize_redacts_secrets_and_long_urls():
    payload = {
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "url": "https://example.com/" + "x" * 200,
        "normal": "keep me",
    }
    cleaned = sanitize(payload)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(cleaned)
    assert "REDACTED" in json.dumps(cleaned)
    assert cleaned["normal"] == "keep me"
