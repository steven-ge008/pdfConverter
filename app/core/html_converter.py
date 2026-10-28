"""HTML → PDF 转换（无头 Chromium，支持 JavaScript 渲染）。

browser 进程常驻复用，每请求新建独立 context（Cookie/缓存/存储完全隔离）。
所有网络子请求经 route 拦截后过 SSRF 校验，阻止页面内脚本探测内网。
"""
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

from .settings import settings
from .ssrf import SSRFError, check_url


class HtmlConversionError(RuntimeError):
    pass


_browser = None
_playwright = None
_browser_lock = asyncio.Lock()

# 统一中文字体回退链，避免页面未声明字体时渲染为方块
_FONT_CSS = (
    'html, body, * {'
    ' font-family: "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC",'
    ' "Microsoft YaHei", sans-serif !important;'
    ' }'
)


async def startup() -> None:
    """服务启动时预热 Chromium，避免首个请求承担启动开销。"""
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            return
        _playwright = await async_playwright().start()
        # 默认使用镜像内自带的 Chromium；可通过 CHROME_CHANNEL 指定本机已装
        # 浏览器（如 "chrome"），便于在容器外做开发自测
        channel = os.environ.get('CHROME_CHANNEL') or None
        _browser = await _playwright.chromium.launch(
            headless=True,
            channel=channel,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',      # 容器 /dev/shm 默认仅 64MB
                '--disable-gpu',
                '--font-render-hinting=none',
            ],
        )


async def shutdown() -> None:
    global _browser, _playwright
    async with _browser_lock:
        if _browser is not None:
            await _browser.close()
            _browser = None
        if _playwright is not None:
            await _playwright.stop()
            _playwright = None


async def _guarded_route(route):
    """逐条校验子请求目标；内网/保留地址直接中止。"""
    url = route.request.url
    # data:/about: 等非网络协议无需出网校验，blob 由页面内部生成
    if not url.startswith(('http://', 'https://')):
        if url.startswith(('data:', 'about:', 'blob:')):
            return await route.continue_()
        return await route.abort('blockedbyclient')
    try:
        await check_url(url)
    except SSRFError:
        return await route.abort('blockedbyclient')
    except Exception:
        # 解析异常按失败关闭处理，宁可拦截也不放行未知目标
        return await route.abort('blockedbyclient')
    return await route.continue_()


async def convert_html(
    dest: Path,
    url: str = None,
    html: str = None,
    timeout: int = None,
    page_format: str = 'A4',
    landscape: bool = False,
    print_background: bool = True,
) -> dict:
    """渲染 URL 或 HTML 字符串为 PDF。二者必须且只能提供其一。"""
    if bool(url) == bool(html):
        raise HtmlConversionError('url 与 html 必须且只能提供其一')
    timeout = timeout or settings.HTML_TIMEOUT

    if url:
        # 主文档先校验一次，尽早失败并给出明确错误（子请求仍逐条拦截）
        try:
            await check_url(url)
        except SSRFError as exc:
            raise HtmlConversionError(str(exc))

    if _browser is None:
        await startup()

    dest.parent.mkdir(parents=True, exist_ok=True)
    context = await _browser.new_context(
        viewport={'width': 1280, 'height': 960},
        ignore_https_errors=False,
        java_script_enabled=True,
    )
    try:
        await context.route('**/*', _guarded_route)
        page = await context.new_page()
        page.set_default_timeout(timeout * 1000)

        if url:
            await page.goto(url, wait_until='networkidle', timeout=timeout * 1000)
        else:
            await page.set_content(html, wait_until='networkidle', timeout=timeout * 1000)

        await page.add_style_tag(content=_FONT_CSS)
        # 切到 print 媒体，使 @media print 样式生效，与真实打印一致
        await page.emulate_media(media='print')

        await page.pdf(
            path=str(dest),
            format=page_format,
            landscape=landscape,
            print_background=print_background,
            prefer_css_page_size=False,
        )
        if not dest.exists() or dest.stat().st_size == 0:
            raise HtmlConversionError('Chromium 未生成有效 PDF')
        return {'pdf': dest}
    except PlaywrightError as exc:
        raise HtmlConversionError(f'页面渲染失败: {exc}') from exc
    finally:
        await context.close()
