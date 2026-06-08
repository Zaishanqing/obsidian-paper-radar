from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]


@dataclass
class AppConfig:
    profile: dict[str, Any]
    daily: dict[str, Any]
    vault_path: Path
    daily_dir: str
    paper_dir: str
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model_fast: str
    deepseek_model_pro: str


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        _apply_proxy_env()
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
    _apply_proxy_env()


def _apply_proxy_env() -> None:
    """让 requests 能读取 .env 中的代理配置。

    requests 默认读取 HTTP_PROXY/HTTPS_PROXY 等环境变量，但不会读取 Windows
    系统代理设置。这里支持 NETWORK_PROXY 简写，并同步大小写变量。
    """

    proxy = (
        os.environ.get("NETWORK_PROXY")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.setdefault(key, proxy)
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if no_proxy:
        os.environ.setdefault("NO_PROXY", no_proxy)
        os.environ.setdefault("no_proxy", no_proxy)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML object: {path}")
    return data


def _config_path(name: str) -> Path:
    """优先用个人配置（不带 example，已 gitignore），缺失时回退到示例配置。"""
    custom = ROOT / "config" / f"{name}.yaml"
    return custom if custom.exists() else ROOT / "config" / f"{name}.example.yaml"


def default_profile_path() -> Path:
    return _config_path("paper_profile")


def default_daily_path() -> Path:
    return _config_path("daily_papers")


def load_app_config(profile_path: Path | None = None, daily_path: Path | None = None) -> AppConfig:
    load_dotenv()
    profile = load_yaml(profile_path or default_profile_path())
    daily = load_yaml(daily_path or default_daily_path())

    vault_raw = os.environ.get("OBSIDIAN_VAULT_PATH") or daily.get("output", {}).get("vault_path")
    if not vault_raw:
        raise RuntimeError("缺少 OBSIDIAN_VAULT_PATH。请复制 .env.example 为 .env 并填写 Vault 路径。")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填写 DeepSeek API Key。")

    return AppConfig(
        profile=profile,
        daily=daily,
        vault_path=Path(vault_raw).expanduser(),
        daily_dir=daily.get("output", {}).get("daily_dir") or os.environ.get("OBSIDIAN_DAILY_DIR", "每日科研论文/日报"),
        paper_dir=daily.get("output", {}).get("paper_dir") or os.environ.get("OBSIDIAN_PAPER_DIR", "每日科研论文/笔记"),
        deepseek_api_key=api_key,
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_model_fast=os.environ.get("DEEPSEEK_MODEL_FAST", "deepseek-v4-flash"),
        deepseek_model_pro=os.environ.get("DEEPSEEK_MODEL_PRO", "deepseek-v4-pro"),
    )
