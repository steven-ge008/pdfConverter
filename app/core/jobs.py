"""进程内异步任务队列 + worker 池。

单镜像部署不依赖外部 Redis/MQ：队列驻内存，状态由 SQLite 持久化。
各转换类型独立信号量限流，避免一类任务拖垮另一类。
"""
import asyncio
import shutil
import uuid
from pathlib import Path

from ..db import init_store
from .downloader import DownloadError, download_to_file
from .excel_converter import ConversionError, convert_excel
from .html_converter import HtmlConversionError, convert_html
from .image_converter import ImageConversionError, convert_image
from .settings import settings
from .ssrf import SSRFError
from .word_converter import WordConversionError, convert_word

_queues = {}
_workers = []
_excel_sem = None
_html_sem = None
_word_sem = None
_image_sem = None

# 每类任务独占队列与 worker 池：running 状态数即真实转换数，
# 且一类任务不会阻塞另一类。信号量用于给同步请求（绕过队列）施加同样的
# 并发硬上限，防止同步调用绕过 worker 数限制打满 CPU/内存。


def _worker_count(kind: str) -> int:
    return {
        'excel': settings.EXCEL_WORKERS,
        'html': settings.HTML_WORKERS,
        'word': settings.WORD_WORKERS,
        'image': settings.IMAGE_WORKERS,
    }[kind]


def _store():
    from .. import db
    return db.store


def job_dir(job_id: str) -> Path:
    return settings.DATA_DIR / 'jobs' / job_id


def result_path(job_id: str) -> Path:
    return job_dir(job_id) / 'result.pdf'


async def startup() -> None:
    global _workers, _excel_sem, _html_sem, _word_sem, _image_sem
    settings.ensure_dirs()
    init_store()
    _store().mark_orphans_failed()
    _excel_sem = asyncio.Semaphore(settings.EXCEL_WORKERS)
    _html_sem = asyncio.Semaphore(settings.HTML_WORKERS)
    _word_sem = asyncio.Semaphore(settings.WORD_WORKERS)
    _image_sem = asyncio.Semaphore(settings.IMAGE_WORKERS)
    for kind in ('excel', 'html', 'word', 'image'):
        _queues[kind] = asyncio.Queue()
        for i in range(_worker_count(kind)):
            _workers.append(asyncio.create_task(_worker(kind, i)))
    _workers.append(asyncio.create_task(_reaper()))


async def shutdown() -> None:
    for w in _workers:
        w.cancel()
    for w in _workers:
        try:
            await w
        except (asyncio.CancelledError, Exception):
            pass
    _workers.clear()


async def submit(kind: str, payload: dict, job_id: str = None) -> str:
    job_id = job_id or uuid.uuid4().hex
    job_dir(job_id).mkdir(parents=True, exist_ok=True)
    _store().create(job_id, kind, payload)
    await _queues[kind].put(job_id)
    return job_id


async def run_now(kind: str, payload: dict, job_id: str = None) -> dict:
    """同步模式：直接执行并返回结果，跳过队列。失败时清理任务目录。"""
    job_id = job_id or uuid.uuid4().hex
    job_dir(job_id).mkdir(parents=True, exist_ok=True)
    try:
        return await _execute(job_id, kind, payload)
    except Exception:
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
        raise


async def _worker(kind: str, idx: int) -> None:
    queue = _queues[kind]
    while True:
        job_id = await queue.get()
        try:
            job = _store().get(job_id)
            if job is None:
                continue
            _store().update(job_id, status='running')
            try:
                result = await _execute(job_id, job['kind'], job['payload'])
            except (ConversionError, HtmlConversionError, WordConversionError,
                    ImageConversionError, DownloadError, SSRFError) as exc:
                _store().update(job_id, status='failed', error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 未预期错误也要落状态，避免调用方永远 pending
                _store().update(job_id, status='failed', error=f'内部错误: {exc}')
            else:
                _store().update(
                    job_id, status='done',
                    result_path=str(result_path(job_id)),
                    pages=result.get('pages'),
                )
        finally:
            queue.task_done()


async def _execute(job_id: str, kind: str, payload: dict) -> dict:
    workdir = job_dir(job_id) / 'work'
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        if kind == 'excel':
            src = await _resolve_excel_input(job_id, payload)
            async with _excel_sem:
                result = await convert_excel(
                    src, workdir, page_config=payload.get('page_config'))
        elif kind == 'html':
            async with _html_sem:
                result = await convert_html(
                    workdir / 'page.pdf',
                    url=payload.get('url'),
                    html=payload.get('html'),
                    page_format=payload.get('format', 'A4'),
                    landscape=bool(payload.get('landscape', False)),
                    print_background=bool(payload.get('print_background', True)),
                )
        elif kind == 'word':
            src = await _resolve_word_input(job_id, payload)
            async with _word_sem:
                result = await convert_word(src, workdir)
        elif kind == 'image':
            src = await _resolve_image_input(job_id, payload)
            async with _image_sem:
                result = await convert_image(src, workdir)
        else:
            raise ConversionError(f'未知任务类型: {kind}')

        dest = result_path(job_id)
        shutil.move(str(result['pdf']), dest)
        result['pdf'] = dest
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _resolve_excel_input(job_id: str, payload: dict) -> Path:
    if payload.get('source') == 'upload':
        return Path(payload['path'])
    url = payload.get('url')
    if not url:
        raise ConversionError('缺少输入：既无上传文件也无 url')
    dest = job_dir(job_id) / 'input.xlsx'
    await download_to_file(url, dest)
    return dest


async def _resolve_word_input(job_id: str, payload: dict) -> Path:
    if payload.get('source') == 'upload':
        return Path(payload['path'])
    url = payload.get('url')
    if not url:
        raise WordConversionError('缺少输入：既无上传文件也无 url')
    dest = job_dir(job_id) / f"input{payload['extension']}"
    await download_to_file(url, dest)
    return dest


async def _resolve_image_input(job_id: str, payload: dict) -> Path:
    if payload.get('source') == 'upload':
        return Path(payload['path'])
    url = payload.get('url')
    if not url:
        raise ImageConversionError('缺少输入：既无上传文件也无 url')
    dest = job_dir(job_id) / f"input{payload['extension']}"
    await download_to_file(url, dest)
    return dest


async def _reaper() -> None:
    """定期清理过期任务与结果文件，防止磁盘被打满。"""
    while True:
        try:
            await asyncio.sleep(settings.CLEANUP_INTERVAL)
            for row in _store().expired(settings.JOB_TTL):
                shutil.rmtree(job_dir(row['id']), ignore_errors=True)
                _store().delete(row['id'])
        except asyncio.CancelledError:
            raise
        except Exception:
            # 清理失败不影响主流程，下个周期重试
            continue
