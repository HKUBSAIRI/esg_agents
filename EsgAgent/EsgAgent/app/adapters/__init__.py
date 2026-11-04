"""外部适配器：模型、对象存储（存储/模型实现可替换）。"""

from app.adapters.object_store import LocalObjectStore
from app.adapters.qwen import ModelResult, QwenPDFClient

__all__ = ["LocalObjectStore", "ModelResult", "QwenPDFClient"]
