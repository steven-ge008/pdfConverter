"""Excel / HTML 转 PDF 服务入口。

接口：
  GET  /healthz                          存活探针（无需鉴权）
  GET  /readyz                           就绪探针（无需鉴权）
  POST /api/v1/convert/excel             multipart 上传 file，或表单 url
  POST /api/v1/convert/html              JSON：url 或 html
  GET  /api/v1/jobs/{job_id}             查询异步任务状态
  GET  /api/v1/jobs/{job_id}/download    下载结果 PDF

转换接口均支持 sync=true 直接返回 PDF 文件流；默认返回 job_id 走异步轮询。
"""
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .core import html_converter, jobs
from .core.downloader import DownloadError, download_to_file
from .core.excel_converter import ConversionError
from .core.html_converter import HtmlConversionError
from .core.security import require_api_key
from .core.settings import settings
from .core.ssrf import SSRFError


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
# 请求模型
# ---------------------------------------------------------------------------

class HtmlRequest(BaseModel):
    url: str = Field(default=None, description='待渲染网页地址，与 html 二选一')
    html: str = Field(default=None, description='HTML 源码字符串，与 url 二选一')
    sync: bool = Field(default=False, description='true 时直接返回 PDF 文件流')
    format: str = Field(default='A4', description='纸张：A4/Letter/Legal/A3 等')
    landscape: bool = False
    print_background: bool = True


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
# Excel 转换
# ---------------------------------------------------------------------------

@app.post('/api/v1/convert/excel', dependencies=[Depends(require_api_key)])
async def convert_excel_api(
    background: BackgroundTasks,
    file: UploadFile = File(default=None),
    url: str = Form(default=None),
    sync: bool = Form(default=False),
    page_config: str = Form(default=None),
):
    if (file is None) == (not url):
        raise HTTPException(400, '请提供上传文件 file 或远程地址 url，且只能二选一')

    cfg = None
    if page_config:
        try:
            cfg = json.loads(page_config)
        except json.JSONDecodeError:
            raise HTTPException(400, 'page_config 必须是合法 JSON')

    # 预建任务目录，同步/异步共用同一套输入落地逻辑
    probe_id = uuid.uuid4().hex
    workdir = jobs.job_dir(probe_id)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        if file is not None:
            if not (file.filename or '').lower().endswith(('.xlsx', '.xlsm')):
                raise HTTPException(400, '仅支持 .xlsx / .xlsm 文件')
            src = workdir / 'input.xlsx'
            await _save_upload(file, src)
            payload = {'source': 'upload', 'path': str(src), 'page_config': cfg}
        else:
            # 同步模式下先下载再转；异步模式下由 worker 下载，快速返回 job_id
            if sync:
                src = workdir / 'input.xlsx'
                await download_to_file(url, src)
                payload = {'source': 'upload', 'path': str(src), 'page_config': cfg}
            else:
                payload = {'source': 'url', 'url': url, 'page_config': cfg}
    except (SSRFError, DownloadError) as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(400, str(exc))
    except Exception:
        # 上传超限、格式不符等失败路径同样要清理预建目录，避免磁盘泄漏
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    # 已用 probe_id 建好目录，直接复用该 id 作为任务 id
    if sync:
        try:
            result = await jobs.run_now('excel', payload, job_id=probe_id)
        except (ConversionError, DownloadError, SSRFError) as exc:
            raise HTTPException(422, str(exc))
        return _pdf_response(result['pdf'], probe_id, background)

    job_id = await jobs.submit('excel', payload, job_id=probe_id)
    return {'job_id': job_id, 'status': 'pending', 'poll_url': f'/api/v1/jobs/{job_id}'}


# ---------------------------------------------------------------------------
# HTML 转换
# ---------------------------------------------------------------------------

@app.post('/api/v1/convert/html', dependencies=[Depends(require_api_key)])
async def convert_html_api(req: HtmlRequest, background: BackgroundTasks):
    if bool(req.url) == bool(req.html):
        raise HTTPException(400, 'url 与 html 必须且只能提供其一')

    payload = {
        'url': req.url,
        'html': req.html,
        'format': req.format,
        'landscape': req.landscape,
        'print_background': req.print_background,
    }
    if req.sync:
        try:
            result = await jobs.run_now('html', payload)
        except (HtmlConversionError, SSRFError) as exc:
            raise HTTPException(422, str(exc))
        # run_now 生成的临时 id 仅用于目录定位
        job_id = Path(result['pdf']).parent.name
        return _pdf_response(result['pdf'], job_id, background)

    job_id = await jobs.submit('html', payload)
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
