"""服务配置：全部通过环境变量注入，提供安全的本地默认值。"""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    # 监听地址
    HOST: str = os.environ.get('HOST', '0.0.0.0')
    PORT: int = _int('PORT', 8000)

    # 鉴权：逗号分隔，可配置多个 Key；为空则关闭鉴权（仅限内网）
    API_KEYS: tuple = tuple(k.strip() for k in os.environ.get('API_KEYS', '').split(',') if k.strip())

    # 数据目录：任务元数据与结果文件，生产环境应挂载 volume 持久化
    DATA_DIR: Path = Path(os.environ.get('DATA_DIR', '/data'))

    # 转换超时（秒）
    EXCEL_TIMEOUT: int = _int('EXCEL_TIMEOUT', 180)
    HTML_TIMEOUT: int = _int('HTML_TIMEOUT', 90)
    WORD_TIMEOUT: int = _int('WORD_TIMEOUT', 180)
    IMAGE_TIMEOUT: int = _int('IMAGE_TIMEOUT', 30)

    # worker 并发上限（LibreOffice CPU 密集、Chromium 内存密集，均须限流）
    EXCEL_WORKERS: int = _int('EXCEL_WORKERS', 2)
    HTML_WORKERS: int = _int('HTML_WORKERS', 2)
    WORD_WORKERS: int = _int('WORD_WORKERS', 2)
    IMAGE_WORKERS: int = _int('IMAGE_WORKERS', 2)

    # URL 下载限制
    DOWNLOAD_TIMEOUT: int = _int('DOWNLOAD_TIMEOUT', 60)
    MAX_DOWNLOAD_BYTES: int = _int('MAX_DOWNLOAD_BYTES', 50 * 1024 * 1024)
    MAX_REDIRECTS: int = _int('MAX_REDIRECTS', 3)

    # 上传限制
    MAX_UPLOAD_BYTES: int = _int('MAX_UPLOAD_BYTES', 50 * 1024 * 1024)

    # 任务与结果保留期（秒），到期由后台协程清理，防止磁盘打满
    JOB_TTL: int = _int('JOB_TTL', 24 * 3600)
    CLEANUP_INTERVAL: int = _int('CLEANUP_INTERVAL', 600)

    # 中文字体：镜像内实际安装的字体，用于消除 LibreOffice 随机 fallback
    CJK_FONT: str = os.environ.get('CJK_FONT', 'Noto Sans CJK SC')

    # soffice 可执行文件（Debian libreoffice-calc 包安装于 /usr/bin/soffice）
    SOFFICE: str = os.environ.get('SOFFICE', '/usr/bin/soffice')

    # 复用的既有转换内核路径（Dockerfile 以只读方式 COPY 进镜像）
    EXCEL2PDF_PY: Path = Path(os.environ.get('EXCEL2PDF_PY', '/opt/excel2pdf/excel2pdf.py'))

    def ensure_dirs(self) -> None:
        (self.DATA_DIR / 'jobs').mkdir(parents=True, exist_ok=True)
        (self.DATA_DIR / 'tmp').mkdir(parents=True, exist_ok=True)


settings = Settings()
