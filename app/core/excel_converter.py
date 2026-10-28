"""Excel(xlsx) → PDF 转换。

复用项目既有内核 excel2pdf.py 的 build_fixed_workbook()（打印区域推算、
fitToPage、页边距校准、CJK 字体统一替换），**不修改原文件**。

不复用其 convert_pdf()：该函数超时硬编码为 300 秒且在线程中无法强杀，
这里改用 asyncio 子进程自行调用 soffice，使超时与并发完全可控。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

from .settings import settings

# 以只读方式复用既有转换内核（Dockerfile COPY 至 /opt/excel2pdf/）
_KERNEL_DIR = str(settings.EXCEL2PDF_PY.parent)
if _KERNEL_DIR not in sys.path:
    sys.path.insert(0, _KERNEL_DIR)

import excel2pdf  # noqa: E402

# build_fixed_workbook 读取模块级 SHEET_PAGE_CONFIG；调用方定制配置时需
# 临时替换，用锁保证该窗口内不会有其他线程读到被替换的配置。
_CONFIG_LOCK = threading.Lock()


class ConversionError(RuntimeError):
    pass


def _build_workbook(src: Path, dst: Path, page_config: dict = None):
    logs = []
    if page_config:
        with _CONFIG_LOCK:
            original = excel2pdf.SHEET_PAGE_CONFIG
            excel2pdf.SHEET_PAGE_CONFIG = {**original, **page_config}
            try:
                values, pics = excel2pdf.build_fixed_workbook(
                    str(src), str(dst), logs.append, font=settings.CJK_FONT)
            finally:
                excel2pdf.SHEET_PAGE_CONFIG = original
    else:
        values, pics = excel2pdf.build_fixed_workbook(
            str(src), str(dst), logs.append, font=settings.CJK_FONT)
    return values, pics, logs


async def _run_soffice(xlsx_path: Path, outdir: Path, profile_dir: Path, timeout: int) -> Path:
    """独立 profile 目录保证并发转换互不干扰；超时强杀子进程树。"""
    cmd = [
        settings.SOFFICE, '--headless', '--norestore', '--nologo',
        f'-env:UserInstallation={profile_dir.as_uri()}',
        '--convert-to', 'pdf:calc_pdf_Export',
        '--outdir', str(outdir), str(xlsx_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,   # 独立进程组，超时可整组终止
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 15)
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
        raise ConversionError(f'LibreOffice 转换超时（>{timeout} 秒），已终止')

    pdf = outdir / (xlsx_path.stem + '.pdf')
    if not pdf.exists():
        detail = (stderr or stdout or b'').decode('utf-8', 'replace')[-800:]
        raise ConversionError(f'LibreOffice 未生成 PDF（退出码 {proc.returncode}）。{detail}')
    return pdf


async def convert_excel(
    src: Path,
    workdir: Path,
    timeout: int = None,
    page_config: dict = None,
) -> dict:
    """转换单个 xlsx，返回 {pdf, pages, fonts, pic_count, text_ok, logs}。"""
    timeout = timeout or settings.EXCEL_TIMEOUT
    if not src.exists():
        raise ConversionError(f'输入文件不存在: {src}')
    if src.suffix.lower() not in ('.xlsx', '.xlsm'):
        raise ConversionError('仅支持 .xlsx / .xlsm 格式')

    workdir.mkdir(parents=True, exist_ok=True)
    try:
        fixed = workdir / f'{src.stem}_fixed.xlsx'
        values, pic_count, logs = await asyncio.wait_for(
            asyncio.to_thread(_build_workbook, src, fixed, page_config),
            timeout=60,
        )

        outdir = workdir / 'out'
        outdir.mkdir(exist_ok=True)
        pdf = await _run_soffice(fixed, outdir, workdir / 'lo_profile', timeout)

        pages = excel2pdf.pdf_page_count(str(pdf))
        fonts = excel2pdf.pdf_embedded_fonts(str(pdf)) or []

        # 数据完整性抽查：复用既有校验逻辑，poppler 缺失时自动跳过
        text_ok, missing = True, []
        text = excel2pdf.pdf_plain_text(str(pdf))
        if text is not None:
            tokens, _ = excel2pdf.collect_tokens(values)
            norm = excel2pdf.normalize_text(text)
            missing = sorted(t for t in tokens if excel2pdf.normalize_text(t) not in norm)
            text_ok = not missing

        return {
            'pdf': pdf,
            'pages': pages,
            'fonts': fonts,
            'pic_count': pic_count,
            'text_ok': text_ok,
            'missing': missing,
            'logs': logs,
        }
    except asyncio.TimeoutError:
        raise ConversionError('工作簿分析超时')
    finally:
        # 固定副本与 profile 体积较大，转换后立即清理；PDF 由调用方移走
        for name in ('lo_profile',):
            shutil.rmtree(workdir / name, ignore_errors=True)
