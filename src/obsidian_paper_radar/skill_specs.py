from __future__ import annotations

from pathlib import Path


DEFAULT_SPEC_FILES = ["风格-论文日报.md", "风格-论文精读笔记.md"]


def load_obsidian_skill_specs(vault_path: Path, filenames: list[str] | None = None, max_chars: int | None = None) -> str:
    """加载 Obsidian 写作规范，默认完整传递不截断。

    DeepSeek 上下文窗口 128K+ tokens，规范文件无需截断。白名单/截断会不可预测地
    丢弃规范内容，导致 LLM 不遵守写作规范。max_chars 仅作极端情况下的安全阀，默认
    None 表示传递完整原文。
    """
    skills_dir = vault_path / "Skills"
    files = filenames or DEFAULT_SPEC_FILES
    chunks: list[str] = []
    for filename in files:
        path = skills_dir / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
        chunks.append(f"## {filename}\n\n{text}")
    return "\n\n---\n\n".join(chunks)
