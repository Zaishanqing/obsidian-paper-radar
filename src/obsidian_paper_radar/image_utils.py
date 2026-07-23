from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from .deepseek_client import DeepSeekClient
from .models import Paper, RerankResult

logger = logging.getLogger(__name__)

# 高宽比极端值：小于此值（太扁）或大于此值（太窄）的图片很可能是页眉/页脚/分割线
_MIN_ASPECT = 0.15
_MAX_ASPECT = 8.0
_MIN_DIMENSION = 60  # 宽或高小于此像素的图片视为无效
_MIN_FILE_SIZE = 1_000  # 文件体积只做轻量兜底，主要依赖尺寸/比例过滤。


def extract_images_for_papers(
    papers: list[Paper],
    asset_paths: dict[str, Path],
    results: dict[str, RerankResult],
    max_papers: int,
    max_images_per_paper: int,
    repo_root: Path,
    client: DeepSeekClient | None = None,
    extract_limit_per_paper: int = 10,
) -> tuple[dict[str, list[Path]], int, int]:
    script = repo_root / "extract-paper-images" / "scripts" / "extract_images.py"
    if not script.exists():
        logger.warning("Image extraction script not found: %s", script)
        return {}, 0, min(max_papers, len(papers))

    extracted: dict[str, list[Path]] = {}
    success = 0
    failed = 0
    for paper in papers[:max_papers]:
        out_dir = asset_paths[paper.paper_id]
        index_file = out_dir / "index.md"
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp_pdf: Path | None = None
        try:
            if paper.source == "arxiv":
                paper_input = paper.paper_id.removeprefix("arxiv:")
            elif paper.pdf_url:
                paper_input, tmp_pdf = _download_pdf_temp(paper)
                if paper_input is None:
                    logger.warning("PDF 下载失败，跳过图片提取：%s", paper.pdf_url)
                    failed += 1
                    extracted[paper.paper_id] = []
                    continue
            else:
                failed += 1
                extracted[paper.paper_id] = []
                continue

            cmd = [sys.executable, str(script), paper_input, str(out_dir), str(index_file), str(extract_limit_per_paper)]
            # 显式 UTF-8 解码，避免在中文 Windows 上按 cp936 解码子进程输出而乱码/丢字。
            result = subprocess.run(
                cmd, cwd=repo_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180,
            )
            if result.returncode == 0:
                raw_count = len(_existing_images(out_dir))
                _select_and_rename_stable(out_dir, results.get(paper.paper_id), max_images_per_paper, client)
                images = _existing_images(out_dir)
                extracted[paper.paper_id] = images
                if images:
                    success += 1
                elif raw_count > 0:
                    # 提取成功，只是没有合适的架构/原理图被选中——不算失败（宁缺毋滥）。
                    logger.info("提取到 %d 张图但无合适架构/原理图，不放图：%s", raw_count, paper.paper_id)
                else:
                    logger.warning("提取脚本成功但未产出任何图片：%s", paper.paper_id)
                    failed += 1
            else:
                logger.warning("Image extraction failed for %s: %s", paper.paper_id, result.stderr[-800:])
                extracted[paper.paper_id] = []
                failed += 1
        except Exception as exc:
            logger.warning("Image extraction failed for %s: %s", paper.paper_id, exc)
            extracted[paper.paper_id] = []
            failed += 1
        finally:
            if tmp_pdf is not None:
                try:
                    tmp_pdf.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove temp PDF %s", tmp_pdf)
    return extracted, success, failed


def _download_pdf_temp(paper: Paper) -> tuple[str | None, Path | None]:
    """下载非 arXiv 论文 PDF 到临时文件，返回 (路径字符串, 临时文件 Path)。"""
    try:
        suffix = ".pdf"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        logger.info("正在下载 PDF: %s", paper.pdf_url)
        resp = requests.get(paper.pdf_url, timeout=120, stream=True)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" in content_type:
            logger.warning("PDF URL 返回 HTML 而非 PDF：%s", paper.pdf_url)
            tmp_path.unlink(missing_ok=True)
            return None, None
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return str(tmp_path), tmp_path
    except Exception as exc:
        logger.warning("PDF 下载失败 %s: %s", paper.pdf_url, exc)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        return None, None


def _select_and_rename_stable(
    out_dir: Path,
    result: RerankResult | None,
    max_images: int,
    client: DeepSeekClient | None = None,
) -> None:
    all_images = _existing_images(out_dir)
    index_text = (out_dir / "index.md").read_text(encoding="utf-8", errors="ignore") if (out_dir / "index.md").exists() else ""
    candidates = _drop_decorative(all_images, index_text)  # 先剔除图标/logo/纯色黑图，再做语义排序
    images = _rank_images(out_dir, result, max_images, client=client, images=candidates)
    selected = images[:max_images]
    selected_names = {path.name for path in selected}
    for image in all_images:
        if image.name not in selected_names:
            try:
                image.unlink()
            except OSError:
                logger.debug("Could not remove unselected image %s", image)
    for idx, image in enumerate(selected, 1):
        if not image.exists():
            continue
        target = out_dir / f"fig{idx}{image.suffix.lower()}"
        if image.name == target.name or target.exists():
            continue
        try:
            shutil.move(str(image), str(target))
        except OSError:
            logger.debug("Could not rename image %s", image)


_MIN_FIGURE_LONG_SIDE = 450  # 长边小于此像素的多半是图标/缩略图/小配图，不是论文架构图


def _is_decorative_image(path: Path, caption_text: str = "") -> bool:
    """识别图标 / logo / 纯色或深底图 / 小配图：论文架构图通常是白底、较大、颜色丰富。

    判据（任一命中即视为装饰图，剔除）：
    - 长边 < 450px：太小，多半是 flaticon 图标、缩略图或小配图（论文图通常更大）；
    - 透明像素占比 > 30%：带透明底的图标/logo（论文图基本不透明）；
    - 偏暗像素占比 > 55%：深色/黑底图（如 OpenAI logo、纯黑图）——论文架构图几乎都是白底；
    - 缩略后不同颜色数 <= 8：扁平纯色块/空白图；
    - 单一主色占比 > 95% 且颜色数 < 40：近乎纯色/空白图；
    - 文件名或局部说明带 icon/avatar/flaticon 等图标信号；
    - 白底占比高、前景集中且尺寸适中的拍平 RGB 图标。
    """
    hint = f"{path.name}\n{caption_text}".lower()
    icon_terms = (
        "flaticon", "avatar", "clipart", "logo", "profile picture",
        "user illustration", "person icon", "people icon", "human icon",
    )
    if any(term in hint for term in icon_terms) or re.search(r"(^|[_\-\s/])icon([_.\-\s/]|$)", hint):
        return True
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return False
    try:
        with Image.open(path) as img:
            w, h = img.size
            if max(w, h) < _MIN_FIGURE_LONG_SIDE:
                return True
            mode = img.mode
            has_alpha = mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in img.info)
            thumb = img.convert("RGBA") if has_alpha else img.convert("RGB")
            thumb.thumbnail((64, 64))
            if has_alpha:
                alpha = list(thumb.getchannel("A").getdata())
                if alpha and sum(1 for a in alpha if a < 16) / len(alpha) > 0.30:
                    return True
                thumb = thumb.convert("RGB")
            pixels = list(thumb.getdata())
            colors = thumb.getcolors(maxcolors=4096)
            white_pixels = [(idx, px) for idx, px in enumerate(pixels) if min(px[:3]) > 245]
            non_white_indices = [idx for idx, px in enumerate(pixels) if min(px[:3]) <= 245]
    except Exception:
        return False
    if not pixels:
        return False
    white_ratio = len(white_pixels) / len(pixels)
    non_white_ratio = len(non_white_indices) / len(pixels)
    path_aspect = 1.0
    try:
        w, h = _image_size(path)
        path_aspect = w / max(h, 1)
    except Exception:
        w, h = 0, 0
    dark = sum(1 for px in pixels if max(px[:3]) < 45) / len(pixels)
    if dark > 0.55:
        return True
    if not colors:
        return False  # 颜色非常丰富（>4096 种）→ 真实图，保留
    total = sum(count for count, _ in colors) or 1
    distinct = len(colors)
    dominant = max(count for count, _ in colors) / total
    if distinct <= 8:
        return True
    if dominant > 0.95 and distinct < 40:
        return True
    # Flattened browser/app logos often arrive from PDFs as opaque square JPEGs:
    # alpha is gone, the background may be black, and a few large color blocks
    # fill the whole image.  They are not paper figures even when the LLM is
    # tempted by surrounding web-agent captions.
    if 0.82 <= path_aspect <= 1.22 and max(w, h) <= 900 and distinct < 1200:
        if dark > 0.12 and dominant > 0.12:
            return True
        if dominant > 0.18 and white_ratio < 0.12:
            return True
    if non_white_indices and 0.75 <= path_aspect <= 1.35 and max(w, h) <= 900 and white_ratio > 0.30:
        xs = [idx % thumb.width for idx in non_white_indices]
        ys = [idx // thumb.width for idx in non_white_indices]
        bbox_area = ((max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)) / max(len(pixels), 1)
        # 拍平成 RGB 的图标常见特征：白底较多、前景集中、有效颜色不算很丰富。
        if non_white_ratio < 0.70 and bbox_area < 0.72 and distinct < 900:
            return True
    return False


def _drop_decorative(images: list[Path], index_text: str = "") -> list[Path]:
    kept: list[Path] = []
    for path in images:
        if _is_decorative_image(path, _caption_for_image(index_text, path, window=240)):
            logger.info("剔除装饰性图片（图标/logo/纯色/黑图）：%s", path.name)
            continue
        kept.append(path)
    return kept


def _existing_images(out_dir: Path) -> list[Path]:
    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    if not out_dir.exists():
        return []
    images = [p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in image_exts]
    # 过滤过小图片
    images = [p for p in images if p.stat().st_size >= _MIN_FILE_SIZE]
    # 去重（基于完整文件 SHA256）
    images = _dedupe_by_hash(images)
    # 过滤高宽比异常或尺寸过小的图片
    images = _filter_by_dimensions(images)
    return sorted(images, key=lambda p: p.name)


def _dedupe_by_hash(images: list[Path]) -> list[Path]:
    """基于文件前 8KB 的 SHA256 去重，保留先出现的。"""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in images:
        try:
            h = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            unique.append(path)
            continue
        if h not in seen:
            seen.add(h)
            unique.append(path)
        else:
            logger.debug("去除重复图片 %s", path.name)
            try:
                path.unlink()
            except OSError:
                pass
    return unique


def _filter_by_dimensions(images: list[Path]) -> list[Path]:
    """过滤高宽比极端或尺寸过小的无效图片（页眉/页脚/公式碎片等）。"""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return images  # Pillow 未安装则跳过维度过滤

    valid: list[Path] = []
    for path in images:
        try:
            with Image.open(path) as img:
                w, h = img.size
        except Exception:
            valid.append(path)
            continue
        if w < _MIN_DIMENSION or h < _MIN_DIMENSION:
            logger.debug("去除过小图片 %s (%dx%d)", path.name, w, h)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        aspect = w / max(h, 1)
        if aspect < _MIN_ASPECT or aspect > _MAX_ASPECT:
            logger.debug("去除高宽比异常图片 %s (%dx%d, ratio=%.2f)", path.name, w, h, aspect)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        valid.append(path)
    return valid


def _rank_images(
    out_dir: Path,
    result: RerankResult | None,
    max_images: int | None = None,
    *,
    client: DeepSeekClient | None = None,
    images: list[Path] | None = None,
) -> list[Path]:
    images = images if images is not None else _existing_images(out_dir)
    index_text = (out_dir / "index.md").read_text(encoding="utf-8", errors="ignore") if (out_dir / "index.md").exists() else ""
    limit = max_images if max_images and max_images > 0 else len(images)
    if not images:
        return []
    if client is not None:
        selected_by_llm = _classify_images_with_llm(client, images, index_text, result, limit)
        if selected_by_llm is not None:
            # 严格：LLM 判定没有合适架构/原理图就返回空，不强塞首图，宁缺毋滥。
            return _prefer_landscape(selected_by_llm)[:limit]
    # 关键词兜底（仅在无 client 或 LLM 调用失败时）：同样不强塞首图。
    arch = [img for img in images if _is_architecture_like(img, _caption_for_image(index_text, img), result)]
    return _prefer_landscape(arch)[:limit]


def _aspect(path: Path) -> float:
    w, h = _image_size(path)
    if not w or not h:
        return 1.0  # 无法判断尺寸时视为中性，不沉底也不优先
    return w / h


def _prefer_landscape(paths: list[Path]) -> list[Path]:
    """候选排序：横图优先、再偏向适中比例（~1.5）；竖图沉底但不丢弃。"""
    return sorted(paths, key=lambda p: (_aspect(p) < 1.0, abs(_aspect(p) - 1.5)))


def _is_architecture_like(image: Path, caption_text: str, result: RerankResult | None) -> bool:
    text = f"{image.name}\n{caption_text}".lower()
    architecture_terms = (
        "architecture", "framework", "pipeline", "overview", "diagram",
        "schematic", "structure", "workflow", "block", "mechanism",
    )
    result_terms = (
        "result", "ablation", "performance", "accuracy", "table", "plot",
        "curve", "bar", "comparison", "benchmark", "example", "sample",
        "visualization", "qualitative", "teaser",
    )
    preferred: list[str] = []
    avoid: list[str] = []
    if result and result.image_guidance:
        preferred = [str(x).lower() for x in result.image_guidance.get("preferred_types", []) if isinstance(x, str)]
        avoid = [str(x).lower() for x in result.image_guidance.get("avoid_types", []) if isinstance(x, str)]
    has_arch_signal = any(word in text for word in architecture_terms) or any(word in text for word in preferred)
    has_result_signal = any(word in text for word in result_terms) or any(word in text for word in avoid)
    return has_arch_signal and not has_result_signal


def _classify_images_with_llm(
    client: DeepSeekClient,
    images: list[Path],
    index_text: str,
    result: RerankResult | None,
    limit: int,
) -> list[Path] | None:
    items = [_image_prompt_item(path, index_text) for path in images]
    messages = [
        {
            "role": "system",
            "content": (
                "你是论文图片类型筛选器，只返回合法 JSON。先判断这批图片里到底有没有真正的论文方法架构/机制/流程图；"
                "如果没有，把 has_architecture 设为 false，items 可以全标 other/low，不要为了凑数硬选。"
                "再结合 paper_hint（论文标题与方法概要）和每张图的文件名、页码、尺寸、局部说明，判断每张图类型。"
                "type 只能是 architecture/result_chart/ablation/example/table/other；"
                "usefulness 只能是 high/medium/low。只有真正展示方法架构、机制、整体流程、模块结构、workflow/pipeline 的示意图才算 architecture，"
                "且要和 paper_hint 的方法对得上。风景照、真实照片、人物/头像/图标、实验曲线、消融、benchmark 表格、数据样例、"
                "qualitative 可视化、teaser、纯文字截图一律不算 architecture。"
                "拿不准就归为 other 并给 low，宁可少选也不要误选。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "paper_hint": {
                        "title": (result.note_title_zh if result else "") or "",
                        "method_hint": ((result.method_overview or result.summary_zh) if result else "")[:300],
                        "preferred_types": (result.image_guidance.get("preferred_types", []) if result and result.image_guidance else []),
                        "avoid_types": (result.image_guidance.get("avoid_types", []) if result and result.image_guidance else []),
                    },
                    "images": items,
                    "schema": {
                        "has_architecture": "boolean，这批图片里是否存在真正的方法架构/机制/流程图",
                        "items": [
                            {
                                "filename": "string",
                                "type": "architecture|result_chart|ablation|example|table|other",
                                "usefulness": "high|medium|low",
                                "reason": "string",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        payload = client.chat_json(messages, model="fast")
    except Exception as exc:
        logger.warning("图片语义分类调用/解析失败，回退到关键词筛选：%s", exc)
        return None
    raw_items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        logger.warning("图片语义分类返回结构异常，回退到关键词筛选")
        return None
    if payload.get("has_architecture") is False:
        logger.info("图片语义分类判断本批无架构/机制/流程图")
        return []
    by_name = {path.name: path for path in images}
    selected: list[Path] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename", "")).strip()
        image_type = str(item.get("type", "")).strip().lower()
        usefulness = str(item.get("usefulness", "")).strip().lower()
        path = by_name.get(filename)
        if (
            path
            and image_type == "architecture"
            and usefulness in {"high", "medium"}
            and not _is_decorative_image(path, _caption_for_image(index_text, path, window=240))
        ):
            selected.append(path)
            if len(selected) >= limit:
                break
    if selected:
        logger.info("图片语义分类选中 %d/%d 张架构图", len(selected), len(images))
    return selected


def _image_prompt_item(path: Path, index_text: str) -> dict[str, object]:
    width, height = _image_size(path)
    page = _page_number(path.name)
    return {
        "filename": path.name,
        "page": page,
        "width": width,
        "height": height,
        "caption_or_index_line": _caption_for_image(index_text, path, window=240)[:500],
    }


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return 0, 0
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return 0, 0


def _page_number(filename: str) -> int | None:
    match = re.search(r"page(\d+)", filename.lower())
    return int(match.group(1)) if match else None


def _caption_for_image(index_text: str, image: Path, window: int = 500) -> str:
    if not index_text:
        return ""
    lowered = index_text.lower()
    candidates = {image.name.lower(), image.stem.lower()}
    for line in index_text.splitlines():
        line_lower = line.lower()
        if any(candidate and candidate in line_lower for candidate in candidates):
            return line
    positions = [lowered.find(candidate) for candidate in candidates if candidate and lowered.find(candidate) >= 0]
    if not positions:
        return ""
    pos = min(positions)
    start = max(0, pos - window)
    end = min(len(index_text), pos + window)
    return index_text[start:end]
