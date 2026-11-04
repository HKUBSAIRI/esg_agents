"""运行配置。

本地开发从 `config/.env` 读取；生产通过环境变量注入。配置文件缺失时给出明确错误，
不静默使用空 API Key。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_ENV_FILE = PROJECT_ROOT / "config" / ".env"


class Settings(BaseModel):
    """全局配置（Pydantic 校验，避免缺失关键项时运行到一半才失败）。"""

    app_env: str = "local"

    # 阿里云百炼 / DashScope（OpenAI 兼容模式）
    dashscope_api_key: str = Field(min_length=8)
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.8-flash"
    qwen_vl_model: str = "qwen3-vl-plus"

    # 模型调用参数
    qwen_timeout_s: float = 600.0
    qwen_max_output_tokens: int = 32768
    qwen_temperature: float = 0.0
    model_max_retries: int = 3
    model_retry_base_s: float = 2.0

    # 契约版本
    schema_version: str = "1"
    prompt_version: str = "v1"

    # PDF 限制（官方单 PDF 上限 150MB / 256 页）
    max_pdf_bytes: int = 150 * 1024 * 1024
    max_pdf_pages: int = 256

    # 平台单次 PDF 解析上限实测：<=70 页可，>=90 页返回 "file format illegal"。
    # 超过阈值时按页分块多次调用（页码回填原报告物理页），并在 coverage.notes 记录。
    extract_chunk_threshold: int = 80
    extract_chunk_pages: int = 63
    # 分块并发数（仅作用于单份报告内部；限制 1..4 避免触发账号限流）
    extract_chunk_concurrency: int = Field(default=3, ge=1, le=4)

    project_root: Path = PROJECT_ROOT
    object_store_dir: Path | None = None
    output_dir: Path | None = None
    logs_dir: Path | None = None

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def skills_dir(self) -> Path:
        return self.project_root / "skills"

    @property
    def prompts_dir(self) -> Path:
        return self.project_root / "prompts"

    @property
    def data_dir(self) -> Path:
        return self.project_root / "data"

    @property
    def objects_dir(self) -> Path:
        return (self.object_store_dir or (self.data_dir / "objects")).resolve()

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"

    @property
    def out_dir(self) -> Path:
        return (self.output_dir or (self.project_root / "outputs")).resolve()

    @property
    def log_dir(self) -> Path:
        return (self.logs_dir or (self.project_root / "logs")).resolve()

    @classmethod
    def load(cls, env_file: Path | str | None = None, **overrides) -> "Settings":
        """加载配置。显式传入的 overrides 优先于 .env 与环境变量。"""
        path = Path(env_file) if env_file else DEFAULT_ENV_FILE
        if path.exists():
            load_dotenv(path, override=False)
        elif env_file is not None:
            raise FileNotFoundError(f"配置文件不存在: {path}")

        values: dict = {
            "app_env": os.getenv("APP_ENV", "local"),
            "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY", ""),
            "dashscope_base_url": os.getenv(
                "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            "qwen_model": os.getenv("QWEN_MODEL", "qwen3.8-flash"),
            "qwen_vl_model": os.getenv("QWEN_VL_MODEL", "qwen3-vl-plus"),
        }
        for env_key, field in (
            ("QWEN_TIMEOUT_S", "qwen_timeout_s"),
            ("QWEN_MAX_OUTPUT_TOKENS", "qwen_max_output_tokens"),
            ("QWEN_TEMPERATURE", "qwen_temperature"),
            ("MODEL_MAX_RETRIES", "model_max_retries"),
            ("EXTRACT_CHUNK_THRESHOLD", "extract_chunk_threshold"),
            ("EXTRACT_CHUNK_PAGES", "extract_chunk_pages"),
            ("EXTRACT_CHUNK_CONCURRENCY", "extract_chunk_concurrency"),
        ):
            raw = os.getenv(env_key)
            if raw:
                if field.endswith("_s") or field == "qwen_temperature":
                    values[field] = float(raw)
                else:
                    values[field] = int(raw)
        values.update(overrides)
        return cls(**values)


def ensure_dirs(settings: Settings) -> None:
    """创建运行期需要写入的目录。"""
    for p in (settings.objects_dir, settings.reports_dir, settings.batches_dir, settings.out_dir, settings.log_dir):
        p.mkdir(parents=True, exist_ok=True)
