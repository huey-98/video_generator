"""配置加载：.env（密钥）+ config.yaml（模型与默认值）"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .registry import ModelSpec

ROOT_DIR = Path(__file__).resolve().parent.parent

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


@dataclass
class AppConfig:
    api_key: str
    base_url: str
    poll_interval: int
    task_timeout: int
    download_timeout: int
    upload_timeout: int
    output_dir: Path
    default_model: str
    defaults: dict = field(default_factory=dict)
    models: list = field(default_factory=list)  # list[ModelSpec]

    def get_model(self, model_id: str) -> ModelSpec:
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(f"config.yaml 中未找到模型：{model_id}")


def load_app_config() -> AppConfig:
    load_dotenv(ROOT_DIR / ".env")

    with open(ROOT_DIR / "config.yaml", "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    api = raw.get("api") or {}
    return AppConfig(
        api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
        base_url=(os.getenv("DASHSCOPE_BASE_URL") or api.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        poll_interval=int(api.get("poll_interval", 5)),
        task_timeout=int(api.get("task_timeout", 900)),
        download_timeout=int(api.get("download_timeout", 300)),
        upload_timeout=int(api.get("upload_timeout", 300)),
        output_dir=ROOT_DIR / (raw.get("output_dir") or "outputs"),
        default_model=(raw.get("defaults") or {}).get("model", ""),
        defaults=raw.get("defaults") or {},
        models=[ModelSpec.from_dict(m) for m in (raw.get("models") or [])],
    )
