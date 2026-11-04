"""用真实 API 验证 QwenPDFClient（小 PDF，一次调用）。"""
import json
from pathlib import Path
from app.adapters.qwen import QwenPDFClient
from app.core.logging import RunLogger
from app.core.settings import Settings

settings = Settings.load()
logger = RunLogger(settings.log_dir, "smoke_adapter")
client = QwenPDFClient(settings, logger)
pdf = Path("outputs/_smoke.pdf").read_bytes()
prompt = ('读取所附 PDF，输出 JSON：{"pages":[{"page":<物理页码>,"items":[{"metric":"...","value":"...","unit":"...","evidence":"逐字原文"}]}]}')
res = client.extract_pdf(pdf, "smoke.pdf", prompt)
print("model:", res.model, "| request_id:", res.request_id, "| latency_ms:", res.latency_ms, "| attempts:", res.attempts)
print("usage:", json.dumps(res.usage, ensure_ascii=False))
print("finish:", res.finish_reason, "| text_len:", len(res.text))
print("json_ok:", bool(json.loads(res.text)))
print(res.text[:400])
