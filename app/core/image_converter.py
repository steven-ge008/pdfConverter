"""图片(jpg/jpeg/png/webp) → A4 PDF 转换。"""
import asyncio
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .settings import settings

Image.MAX_IMAGE_PIXELS = 50_000_000

_A4_DPI = 150
_A4_PORTRAIT = (1240, 1754)
_A4_LANDSCAPE = (1754, 1240)
_MARGIN = 59
_SUPPORTED_FORMATS = {'JPEG', 'PNG', 'WEBP'}


class ImageConversionError(RuntimeError):
    pass


def _convert(src: Path, dest: Path) -> None:
    try:
        with Image.open(src) as probe:
            image_format = probe.format
            probe.verify()
        if image_format not in _SUPPORTED_FORMATS:
            raise ImageConversionError('仅支持 JPG/JPEG、PNG、WebP 图片')

        with Image.open(src) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                flattened = Image.new('RGB', rgba.size, 'white')
                flattened.paste(rgba, mask=rgba.getchannel('A'))
                image = flattened
            else:
                image = image.convert('RGB')

            page_size = _A4_LANDSCAPE if image.width > image.height else _A4_PORTRAIT
            available = (page_size[0] - 2 * _MARGIN, page_size[1] - 2 * _MARGIN)
            image.thumbnail(available, Image.Resampling.LANCZOS)
            page = Image.new('RGB', page_size, 'white')
            position = ((page.width - image.width) // 2, (page.height - image.height) // 2)
            page.paste(image, position)
            page.save(dest, 'PDF', resolution=_A4_DPI)
    except ImageConversionError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ImageConversionError(f'图片读取或转换失败: {exc}') from exc


async def convert_image(src: Path, workdir: Path, timeout: int = None) -> dict:
    timeout = timeout or settings.IMAGE_TIMEOUT
    if not src.exists():
        raise ImageConversionError(f'输入文件不存在: {src}')
    if src.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise ImageConversionError('仅支持 .jpg / .jpeg / .png / .webp 格式')

    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / 'image.pdf'
    try:
        await asyncio.wait_for(asyncio.to_thread(_convert, src, dest), timeout=timeout)
    except asyncio.TimeoutError:
        raise ImageConversionError(f'图片转换超时（>{timeout} 秒）')
    if not dest.exists():
        raise ImageConversionError('未生成 PDF')
    return {'pdf': dest, 'pages': 1}
