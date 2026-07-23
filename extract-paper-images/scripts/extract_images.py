#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
论文图片提取脚本 - 支持从arXiv源码包优先提取
优先级：
1. arXiv源码包中的pics/或figures/目录（真正的论文图片）
2. 源码包中的PDF图片（架构图、实验图等）
3. PDF直接提取的图片（最后备选）
"""

import fitz  # PyMuPDF
import os
import json
import sys
import re
import shutil
import tarfile
import tempfile
import logging
from pathlib import Path

# 计划任务通过 VBS/PowerShell 捕获输出，子进程 stdout/stderr 默认随系统区域用
# GBK(cp936) 编码，打印含中文或 \xb2 等字符的路径时会 UnicodeEncodeError 崩溃。
# 强制 UTF-8 并对无法编码的字符降级替换，保证打印永不中断（父进程也按 UTF-8 解码）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    import urllib.request
    HAS_REQUESTS = False
    logger.warning("requests not found, using urllib")


def extract_arxiv_source(arxiv_id, temp_dir):
    """下载并提取arXiv源码包"""
    source_url = f"https://arxiv.org/e-print/{arxiv_id}"
    print(f"正在下载arXiv源码包: {source_url}")

    try:
        if HAS_REQUESTS:
            response = requests.get(source_url, timeout=60)
            content = response.content if response.status_code == 200 else None
            status = response.status_code
        else:
            try:
                req = urllib.request.urlopen(source_url, timeout=60)
                content = req.read()
                status = req.status
            except urllib.error.HTTPError as http_err:
                logger.error("HTTP错误 %d: %s", http_err.code, http_err.reason)
                return False

        if status == 200 and content:
            tar_path = os.path.join(temp_dir, f"{arxiv_id}.tar.gz")
            with open(tar_path, 'wb') as f:
                f.write(content)
            print(f"源码包已下载: {tar_path}")

            with tarfile.open(tar_path, 'r:gz') as tar:
                # 过滤危险路径和符号链接，防止路径遍历攻击
                safe_members = []
                for member in tar.getmembers():
                    if member.name.startswith('/') or '..' in member.name:
                        continue
                    if member.issym() or member.islnk():
                        continue
                    safe_members.append(member)
                tar.extractall(path=temp_dir, members=safe_members)
            print(f"源码已提取到: {temp_dir}")
            return True
        else:
            print(f"下载失败: HTTP {status}")
            return False
    except Exception as e:
        logger.error("下载源码包失败: %s", e)
        return False


def find_figures_from_source(temp_dir):
    """从源码目录中查找图片（搜索所有匹配的目录）"""
    figures = []
    seen_files = set()

    figure_dirs = ['pics', 'figures', 'fig', 'images', 'img']

    for fig_dir in figure_dirs:
        fig_path = os.path.join(temp_dir, fig_dir)
        if os.path.exists(fig_path):
            print(f"找到图片目录: {fig_path}")
            for filename in os.listdir(fig_path):
                file_path = os.path.join(fig_path, filename)
                if os.path.isfile(file_path) and filename not in seen_files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in ['.png', '.jpg', '.jpeg', '.pdf', '.eps', '.svg']:
                        seen_files.add(filename)
                        figures.append({
                            'type': 'source',
                            'source': 'arxiv-source',
                            'path': file_path,
                            'filename': filename
                        })

    # 如果没有找到单独的目录，检查根目录的图片文件
    if not figures:
        for filename in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, filename)
            if os.path.isfile(file_path):
                ext = os.path.splitext(filename)[1].lower()
                if ext in ['.png', '.jpg', '.jpeg'] and 'logo' not in filename.lower() and 'icon' not in filename.lower():
                    figures.append({
                        'type': 'source',
                        'source': 'arxiv-source',
                        'path': file_path,
                        'filename': filename
                    })

    return figures


_CAPTION_RE = re.compile(r'(?im)^\s*((?:figure|fig\.?|table|算法|图|表)\s*\d+\s*[\.:：、]?\s*.{0,160})')


def _page_captions(page):
    """从页面文本提取 Figure/Table/图/表 的标题行，给图片筛选提供语义信号。"""
    try:
        text = page.get_text("text")
    except Exception:
        return []
    captions = []
    for match in _CAPTION_RE.finditer(text):
        cap = re.sub(r'\s+', ' ', match.group(1)).strip()
        if cap and cap not in captions:
            captions.append(cap)
    return captions


def extract_pdf_figures(pdf_path, output_dir, min_width=200, min_height=200, min_bytes=5000, max_figures=None):
    """从PDF中提取图片（备选方案）

    Args:
        pdf_path: PDF文件路径
        output_dir: 输出目录
        min_width: 最小宽度（像素），过滤图标/logo
        min_height: 最小高度（像素），过滤图标/logo
        min_bytes: 最小文件大小（字节），过滤小碎片
        max_figures: 最多提取多少张候选图片，None 表示不限制
    """
    print("从PDF直接提取图片（备选方案）...")

    try:
        pdf_doc = fitz.open(pdf_path)
    except Exception as e:
        logger.error("无法打开PDF文件: %s (%s)", pdf_path, e)
        return []

    image_list = []
    skipped = 0

    try:
        for page_num in range(len(pdf_doc)):
            page = pdf_doc[page_num]
            image_list_page = page.get_images(full=True)

            if image_list_page:
                page_caption = "；".join(_page_captions(page))[:300]
                for img_index, img in enumerate(image_list_page):
                    xref = img[0]
                    try:
                        base_image = pdf_doc.extract_image(xref)
                    except Exception as e:
                        logger.warning("  跳过无法提取的图片 (page %d, xref %d): %s", page_num + 1, xref, e)
                        continue

                    if base_image:
                        image_bytes = base_image['image']
                        image_ext = base_image['ext']
                        img_width = base_image.get('width', 0)
                        img_height = base_image.get('height', 0)

                        # 过滤小图标、logo和UI碎片
                        if img_width < min_width or img_height < min_height:
                            skipped += 1
                            continue
                        if len(image_bytes) < min_bytes:
                            skipped += 1
                            continue

                        filename = f'page{page_num + 1}_fig{img_index + 1}.{image_ext}'
                        filepath = os.path.join(output_dir, filename)

                        with open(filepath, 'wb') as img_file:
                            img_file.write(image_bytes)

                        image_list.append({
                            'page': page_num + 1,
                            'index': img_index + 1,
                            'filename': filename,
                            'path': f'images/{filename}',
                            'size': len(image_bytes),
                            'width': img_width,
                            'height': img_height,
                            'ext': image_ext,
                            'caption': page_caption,
                        })
                        if max_figures is not None and len(image_list) >= max_figures:
                            return image_list
    finally:
        pdf_doc.close()

    if skipped:
        print(f"  已过滤 {skipped} 张小图片/图标 (< {min_width}x{min_height}px 或 < {min_bytes/1024:.0f}KB)")

    return image_list


def extract_from_pdf_figures(figures_pdf, output_dir, max_figures=None):
    """从PDF格式图片文件中提取图片"""
    print(f"从PDF图片文件提取: {os.path.basename(figures_pdf)}")

    extracted = []
    doc = fitz.open(figures_pdf)
    filename = os.path.splitext(os.path.basename(figures_pdf))[0]

    try:
        for i in range(len(doc)):
            if max_figures is not None and len(extracted) >= max_figures:
                break
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            output_name = f'{filename}_page{i+1}.png'
            output_path = os.path.join(output_dir, output_name)
            pix.save(output_path)

            extracted.append({
                'filename': output_name,
                'path': f'images/{output_name}',
                'size': os.path.getsize(output_path),  # 使用实际文件大小
                'ext': 'png'
            })
    finally:
        doc.close()

    return extracted


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )

    if len(sys.argv) < 4:
        print("Usage: python extract_images.py <paper_id> <output_dir> <index_file> [max_figures]")
        print("  paper_id: arXiv ID (如: 2510.24701) 或本地PDF路径")
        print("  output_dir: 输出目录")
        print("  index_file: 索引文件路径")
        sys.exit(1)

    paper_input = sys.argv[1]
    output_dir = sys.argv[2]
    index_file = sys.argv[3]
    max_figures = 10
    if len(sys.argv) >= 5:
        try:
            max_figures = max(1, int(sys.argv[4]))
        except ValueError:
            logger.warning("max_figures 参数无效，使用默认值 10: %s", sys.argv[4])

    os.makedirs(output_dir, exist_ok=True)

    is_pdf_file = os.path.isfile(paper_input)
    arxiv_id = None
    pdf_path = None

    if is_pdf_file:
        pdf_path = paper_input
        filename = os.path.basename(pdf_path)
        match = re.search(r'(\d{4}\.\d+)', filename)
        if match:
            arxiv_id = match.group(1)
            print(f"检测到arXiv ID: {arxiv_id}")
    else:
        arxiv_id = paper_input

    with tempfile.TemporaryDirectory() as temp_dir:
        all_figures = []

        # 步骤1: 尝试从arXiv源码包提取
        if arxiv_id:
            if extract_arxiv_source(arxiv_id, temp_dir):
                source_figures = find_figures_from_source(temp_dir)
                if source_figures:
                    print(f"\n从arXiv源码找到 {len(source_figures)} 个图片文件")
                    for fig in source_figures:
                        if len(all_figures) >= max_figures:
                            break
                        output_file = os.path.join(output_dir, fig['filename'])
                        shutil.copy2(fig['path'], output_file)

                        all_figures.append({
                            'filename': fig['filename'],
                            'path': f'images/{fig["filename"]}',
                            'size': os.path.getsize(output_file),
                            'ext': os.path.splitext(fig['filename'])[1][1:].lower(),
                            'source': fig['source']
                        })
                        print(f"  - {fig['filename']}")

        # 步骤2: 如果源码包中没有找到足够的图片，从PDF中提取
        if len(all_figures) < 3 and pdf_path and len(all_figures) < max_figures:
            print(f"\n找到的图片数量较少，从PDF直接提取...")
            pdf_figures = extract_pdf_figures(pdf_path, output_dir, max_figures=max_figures - len(all_figures))
            for fig in pdf_figures:
                fig['source'] = 'pdf-extraction'
                all_figures.append(fig)

        # 步骤3: 检查源码包中的PDF图片文件并提取
        if arxiv_id and os.path.exists(temp_dir):
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    if len(all_figures) >= max_figures:
                        break
                    if file.endswith('.pdf') and 'logo' not in file.lower() and file != f'{arxiv_id}.tar.gz':
                        pdf_fig_path = os.path.join(root, file)
                        try:
                            extracted = extract_from_pdf_figures(pdf_fig_path, output_dir, max_figures=max_figures - len(all_figures))
                            for fig in extracted:
                                if len(all_figures) >= max_figures:
                                    break
                                fig['source'] = 'pdf-figure'
                                all_figures.append(fig)
                        except Exception as e:
                            logger.warning("  跳过无法处理的PDF: %s (%s)", file, e)
                if len(all_figures) >= max_figures:
                    break

    # 生成索引文件
    with open(index_file, 'w', encoding='utf-8') as f:
        f.write('# 图片索引\n\n')
        f.write(f'总计：{len(all_figures)} 张图片\n\n')

        sources = {}
        for fig in all_figures:
            source = fig.get('source', 'unknown')
            if source not in sources:
                sources[source] = []
            sources[source].append(fig)

        for source, figs in sources.items():
            f.write(f'\n## 来源: {source}\n')
            for fig in figs:
                caption = str(fig.get('caption', '')).strip()
                # 说明写在文件名同一行，供下游图片筛选按文件名定位到 caption。
                caption_suffix = f' ｜ 说明：{caption}' if caption else ''
                f.write(f'- 文件名：{fig["filename"]}{caption_suffix}\n')
                f.write(f'- 路径：{fig["path"]}\n')
                f.write(f'- 大小：{fig["size"] / 1024:.1f} KB\n')
                f.write(f'- 格式：{fig["ext"]}\n\n')

    print(f'\n成功提取 {len(all_figures)} 张图片')
    print(f'保存目录：{output_dir}')
    print(f'索引文件：{index_file}')
    print('\n图片列表：')
    for fig in all_figures:
        print(f'  - {fig["path"]} ({fig.get("source", "unknown")})')

    print('\nImage paths:')
    for fig in all_figures:
        print(fig["path"])


if __name__ == '__main__':
    main()
