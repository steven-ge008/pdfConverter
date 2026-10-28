"""Excel / Word / 图片 / HTML 转 PDF 服务入口。

接口：
  GET  /healthz                          存活探针（无需鉴权）
  GET  /readyz                           就绪探针（无需鉴权）
  POST /api/v1/convert                   multipart 统一转换入口
  GET  /api/v1/jobs/{job_id}             查询异步任务状态
  GET  /api/v1/jobs/{job_id}/download    下载结果 PDF

转换接口支持 sync=true 直接返回 PDF 文件流；默认返回 job_id 走异步轮询。
"""
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db
from .core import html_converter, jobs
from .core.downloader import DownloadError, download_to_file
from .core.excel_converter import ConversionError
from .core.html_converter import HtmlConversionError
from .core.image_converter import ImageConversionError
from .core.security import require_api_key
from .core.settings import settings
from .core.ssrf import SSRFError
from .core.word_converter import WordConversionError


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    await jobs.startup()
    try:
        await html_converter.startup()   # 预热 Chromium；失败不阻断 Excel 能力
    except Exception:
        pass
    yield
    await jobs.shutdown()
    await html_converter.shutdown()


app = FastAPI(title='converter-service', version='1.0.0', lifespan=lifespan)


# ---------------------------------------------------------------------------
# 探针
# ---------------------------------------------------------------------------

@app.get('/healthz')
async def healthz():
    return {'status': 'ok'}


@app.get('/readyz')
async def readyz():
    return {
        'status': 'ok',
        'soffice': Path(settings.SOFFICE).exists(),
        'chromium': html_converter._browser is not None,
        'data_dir_writable': settings.DATA_DIR.exists(),
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _job_view(job: dict) -> dict:
    status = job['status']
    view = {
        'job_id': job['id'],
        'kind': job['kind'],
        'status': status,
        'created_at': job['created_at'],
        'updated_at': job['updated_at'],
    }
    if status == 'done':
        view['pages'] = job['pages']
        view['download_url'] = f"/api/v1/jobs/{job['id']}/download"
    if status == 'failed':
        view['error'] = job['error']
    return view


def _pdf_response(pdf: Path, job_id: str, background: BackgroundTasks) -> FileResponse:
    # 文件流发送完毕后清理任务目录，避免同步请求残留临时文件
    background.add_task(shutil.rmtree, jobs.job_dir(job_id), ignore_errors=True)
    return FileResponse(
        path=str(pdf),
        media_type='application/pdf',
        filename=f'{job_id}.pdf',
        background=background,
    )


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    """流式落盘并强制大小上限，避免超大上传占满磁盘。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with open(dest, 'wb') as fh:
            while chunk := await upload.read(1024 * 1024):
                written += len(chunk)
                if written > settings.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f'上传文件超过大小上限 {settings.MAX_UPLOAD_BYTES} 字节')
                fh.write(chunk)
    finally:
        await upload.close()
    if written == 0:
        raise HTTPException(400, '上传文件为空')


# ---------------------------------------------------------------------------
# 统一转换入口
# ---------------------------------------------------------------------------

_EXTENSIONS = {
    'excel': ('.xlsx', '.xlsm'),
    'word': ('.docx', '.doc'),
    'image': ('.jpg', '.jpeg', '.png', '.webp'),
}


@app.post('/api/v1/convert', dependencies=[Depends(require_api_key)])
async def convert_api(
    background: BackgroundTasks,
    sourceType: str = Form(default=None),
    file: UploadFile = File(default=None),
    url: str = Form(default=None),
    html: str = Form(default=None),
    sync: bool = Form(default=False),
    page_config: str = Form(default=None),
    format: str = Form(default='A4'),
    landscape: bool = Form(default=False),
    print_background: bool = Form(default=True),
):
    if sourceType not in (*_EXTENSIONS, 'html'):
        raise HTTPException(400, 'sourceType 必须为 excel / word / image / html')

    if sourceType == 'html':
        if file is not None:
            raise HTTPException(400, 'HTML 不支持文件上传，请提供 url 或 html')
        if bool(url) == bool(html):
            raise HTTPException(400, 'url 与 html 必须且只能提供其一')
        payload = {
            'url': url,
            'html': html,
            'format': format,
            'landscape': landscape,
            'print_background': print_background,
        }
    else:
        if (file is None) == (not url):
            raise HTTPException(400, '请提供上传文件 file 或远程地址 url，且只能二选一')
        filename = file.filename if file is not None else urlsplit(url).path
        extension = Path(filename or '').suffix.lower()
        if extension not in _EXTENSIONS[sourceType]:
            supported = ' / '.join(_EXTENSIONS[sourceType])
            raise HTTPException(400, f'文件扩展名与 sourceType={sourceType} 不匹配，仅支持 {supported}')

    cfg = None
    if sourceType == 'excel' and page_config:
        try:
            cfg = json.loads(page_config)
        except json.JSONDecodeError:
            raise HTTPException(400, 'page_config 必须是合法 JSON')

    probe_id = uuid.uuid4().hex
    workdir = jobs.job_dir(probe_id)
    workdir.mkdir(parents=True, exist_ok=True)

    if sourceType != 'html':
        try:
            if file is not None:
                filename = 'input.xlsx' if sourceType == 'excel' else f'input{extension}'
                src = workdir / filename
                await _save_upload(file, src)
                payload = {'source': 'upload', 'path': str(src)}
            elif sync:
                filename = 'input.xlsx' if sourceType == 'excel' else f'input{extension}'
                src = workdir / filename
                await download_to_file(url, src)
                payload = {'source': 'upload', 'path': str(src)}
            else:
                payload = {'source': 'url', 'url': url, 'extension': extension}
            if sourceType == 'excel':
                payload['page_config'] = cfg
        except (SSRFError, DownloadError) as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            raise HTTPException(400, str(exc))
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    if sync:
        try:
            result = await jobs.run_now(sourceType, payload, job_id=probe_id)
        except (ConversionError, HtmlConversionError, WordConversionError,
                ImageConversionError, DownloadError, SSRFError) as exc:
            raise HTTPException(422, str(exc))
        return _pdf_response(result['pdf'], probe_id, background)

    job_id = await jobs.submit(sourceType, payload, job_id=probe_id)
    return {'job_id': job_id, 'status': 'pending', 'poll_url': f'/api/v1/jobs/{job_id}'}


# ---------------------------------------------------------------------------
# 任务查询与下载
# ---------------------------------------------------------------------------

@app.get('/api/v1/jobs/{job_id}', dependencies=[Depends(require_api_key)])
async def get_job(job_id: str):
    job = db.store.get(job_id)
    if job is None:
        raise HTTPException(404, '任务不存在或已过期清理')
    return _job_view(job)


@app.get('/api/v1/jobs/{job_id}/download', dependencies=[Depends(require_api_key)])
async def download_job(job_id: str):
    job = db.store.get(job_id)
    if job is None:
        raise HTTPException(404, '任务不存在或已过期清理')
    if job['status'] != 'done':
        raise HTTPException(409, f"任务尚未完成，当前状态: {job['status']}")
    pdf = jobs.result_path(job_id)
    if not pdf.exists():
        raise HTTPException(410, '结果文件已不存在')
    return FileResponse(path=str(pdf), media_type='application/pdf', filename=f'{job_id}.pdf')
