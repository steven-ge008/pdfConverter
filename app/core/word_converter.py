"""Word(doc/docx) → PDF 转换，使用 LibreOffice Writer。"""
import asyncio
import os
import shutil
from pathlib import Path

from .settings import settings


class WordConversionError(RuntimeError):
    pass


async def _run_soffice(src: Path, outdir: Path, profile_dir: Path, timeout: int) -> Path:
    cmd = [
        settings.SOFFICE, '--headless', '--norestore', '--nologo',
        f'-env:UserInstallation={profile_dir.as_uri()}',
        '--convert-to', 'pdf:writer_pdf_Export',
        '--outdir', str(outdir), str(src),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
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
        raise WordConversionError(f'LibreOffice 转换超时（>{timeout} 秒），已终止')

    pdf = outdir / (src.stem + '.pdf')
    if not pdf.exists():
        detail = (stderr or stdout or b'').decode('utf-8', 'replace')[-800:]
        raise WordConversionError(f'LibreOffice 未生成 PDF（退出码 {proc.returncode}）。{detail}')
    return pdf


async def _pdf_page_count(pdf: Path) -> int:
    proc = await asyncio.create_subprocess_exec(
        'pdfinfo', str(pdf),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    for line in stdout.decode('utf-8', 'replace').splitlines():
        if line.startswith('Pages:'):
            return int(line.split(':', 1)[1].strip())
    return 0


async def convert_word(src: Path, workdir: Path, timeout: int = None) -> dict:
    timeout = timeout or settings.WORD_TIMEOUT
    if not src.exists():
        raise WordConversionError(f'输入文件不存在: {src}')
    if src.suffix.lower() not in ('.docx', '.doc'):
        raise WordConversionError('仅支持 .docx / .doc 格式')

    workdir.mkdir(parents=True, exist_ok=True)
    try:
        outdir = workdir / 'out'
        outdir.mkdir(exist_ok=True)
        pdf = await _run_soffice(src, outdir, workdir / 'lo_profile', timeout)
        return {'pdf': pdf, 'pages': await _pdf_page_count(pdf)}
    finally:
        shutil.rmtree(workdir / 'lo_profile', ignore_errors=True)
