from __future__ import annotations

from pathlib import Path


DEFAULT_SPEC_FILES = ["风格-论文日报.md", "风格-论文精读笔记.md"]

# 项目自带默认规范文件目录，当 Vault Skills/ 下没有时作为 fallback
_PROJECT_SPEC_DIR = Path(__file__).resolve().parents[2] / "config"


def load_obsidian_skill_specs(vault_path: Path, filenames: list[str] | None = None, max_chars: int | None = None) -> str:
    """加载 Obsidian 写作规范，默认完整传递不截断。

    DeepSeek 上下文窗口 128K+ tokens，规范文件无需截断。白名单/截断会不可预测地
    丢弃规范内容，导致 LLM 不遵守写作规范。max_chars 仅作极端情况下的安全阀，默认
    None 表示传递完整原文。

    查找优先级：Vault Skills/{文件名} > 项目 config/{文件名} > 跳过。
    """
    skills_dir = vault_path / "Skills"
    files = filenames or DEFAULT_SPEC_FILES
    chunks: list[str] = []
    for filename in files:
        path = _resolve_spec_path(skills_dir, filename)
        if path is None:
            continue
        text = path.read_text(encoding="utf-8")
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        chunks.append(f"## {filename}\n\n{text}")
    return "\n\n---\n\n".join(chunks)


def _resolve_spec_path(skills_dir: Path, filename: str) -> Path | None:
    """按优先级查找规范文件：Vault Skills/ > 项目 config/。"""
    vault_path = skills_dir / filename
    if vault_path.exists():
        return vault_path
    project_path = _PROJECT_SPEC_DIR / filename
    if project_path.exists():
        return project_path
    return None
