"""带 SSRF 防护的远程文件下载。

连接前把主机名固定为已校验通过的 IP（IP pinning），HTTP 层不再发起 DNS
查询，从机制上消除 DNS rebinding 的校验-连接竞态窗口。
"""
import ipaddress
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .settings import settings
from .ssrf import SSRFError, resolve_checked


class DownloadError(RuntimeError):
    pass


def _pinned_url(url: str, ip: str) -> str:
    """把 URL 的主机名替换为已校验 IP，保留端口/路径/查询串。"""
    parts = urlsplit(url)
    bracketed = f'[{ip}]' if ':' in ip else ip
    netloc = bracketed if parts.port is None else f'{bracketed}:{parts.port}'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ''))


async def download_to_file(url: str, dest: Path, max_bytes: int = None) -> Path:
    """下载 url 到 dest，返回实际路径。任何一环不安全即抛异常。"""
    limit = max_bytes or settings.MAX_DOWNLOAD_BYTES
    current = url
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(
        follow_redirects=False,          # 重定向自行逐跳处理并重新校验
        timeout=httpx.Timeout(settings.DOWNLOAD_TIMEOUT, connect=15.0),
        verify=True,                     # 保留证书校验；SNI 经 extension 固定为原域名
        headers={'User-Agent': 'converter-service/1.0'},
    ) as client:
        for hop in range(settings.MAX_REDIRECTS + 1):
            target = await resolve_checked(current)
            ip = target.ips[0]
            request_headers = {
                'Host': target.host if target.port in (80, 443)
                else f'{target.host}:{target.port}',
            }
            try:
                async with client.stream(
                    'GET',
                    _pinned_url(current, ip),
                    headers=request_headers,
                    # SNI 固定为原域名，使证书校验按真实域名进行
                    extensions={'sni_hostname': target.host},
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get('location')
                        if not location:
                            raise DownloadError('重定向响应缺少 Location 头')
                        if hop >= settings.MAX_REDIRECTS:
                            raise DownloadError(f'重定向次数超过上限 {settings.MAX_REDIRECTS}')
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        raise DownloadError(f'远程返回 HTTP {resp.status_code}')

                    declared = resp.headers.get('content-length')
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise DownloadError(f'远程文件超过大小上限 {limit} 字节')

                    written = 0
                    with open(dest, 'wb') as fh:
                        async for chunk in resp.aiter_bytes(65536):
                            written += len(chunk)
                            if written > limit:
                                raise DownloadError(f'下载内容超过大小上限 {limit} 字节')
                            fh.write(chunk)
                    if written == 0:
                        raise DownloadError('远程文件为空')
                    return dest
            except httpx.HTTPError as exc:
                raise DownloadError(f'下载失败: {exc}') from exc
        raise DownloadError('重定向处理异常终止')


__all__ = ['download_to_file', 'DownloadError', 'SSRFError']
