"""SSRF 防护：URL 协议白名单 + DNS 解析后逐条校验 IP。

Excel 下载与 Chromium 子请求共用本模块。任何解析到内网/环回/链路本地/
云元数据地址的目标一律拒绝，防止服务被用作内网探测跳板。
"""
import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

# 显式枚举需拒绝的网段，不依赖 ipaddress 各判定属性在不同版本间的差异
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        '0.0.0.0/8',            # 本网络
        '10.0.0.0/8',           # RFC1918 私网
        '100.64.0.0/10',        # CGNAT 运营商级 NAT
        '127.0.0.0/8',          # 环回
        '169.254.0.0/16',       # 链路本地（含云元数据 169.254.169.254）
        '172.16.0.0/12',        # RFC1918 私网
        '192.0.0.0/24',         # IETF 协议分配
        '192.168.0.0/16',       # RFC1918 私网
        '198.18.0.0/15',        # 网络设备基准测试
        '224.0.0.0/4',          # 组播
        '240.0.0.0/4',          # 保留
        '::/128',               # 未指定
        '::1/128',              # IPv6 环回
        '::ffff:0:0/96',        # IPv4 映射地址（绕过 IPv4 规则的常见手法）
        '64:ff9b::/96',         # NAT64
        '100::/64',             # 丢弃前缀
        '2001:db8::/32',        # 文档保留
        'fc00::/7',             # IPv6 ULA 私网
        'fe80::/10',            # IPv6 链路本地
        'ff00::/8',             # IPv6 组播
    )
]


class SSRFError(ValueError):
    """目标地址被 SSRF 策略拒绝。"""


@dataclass(frozen=True)
class ResolvedTarget:
    scheme: str
    host: str
    port: int
    ips: tuple


def is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    # 先剥离 IPv4-mapped 外壳，避免 ::ffff:127.0.0.1 这类绕过
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _BLOCKED_NETWORKS if addr.version == net.version)


def parse_http_url(url: str) -> tuple:
    """校验协议与基本结构，返回 (scheme, host, port)。"""
    if not url or len(url) > 4096:
        raise SSRFError('URL 为空或过长')
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise SSRFError(f'仅允许 http/https 协议，收到: {scheme or "（无协议）"}')
    if parts.username or parts.password:
        raise SSRFError('URL 不允许携带 userinfo 凭据')
    host = parts.hostname
    if not host:
        raise SSRFError('URL 缺少主机名')
    host = host.strip('.').lower()
    if not host:
        raise SSRFError('URL 主机名非法')
    # 末尾加点、全数字主机、十六进制等形式统一交给解析与 IP 校验处理
    try:
        port = parts.port or (443 if scheme == 'https' else 80)
    except ValueError:
        raise SSRFError('端口非法')
    return scheme, host, port


async def resolve_checked(url: str, dns_timeout: float = 10.0) -> ResolvedTarget:
    """解析并校验 URL：所有解析结果必须全部是公网地址。

    只要有一条记录落入内网即整体拒绝（防混合记录与 rebinding 变体）。
    """
    scheme, host, port = parse_http_url(url)

    # IP 字面量：无需 DNS，直接校验
    try:
        ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        pass
    else:
        if is_blocked_ip(host.strip('[]')):
            raise SSRFError(f'目标为内网/保留地址，已拒绝: {host}')
        return ResolvedTarget(scheme, host, port, (host.strip('[]'),))

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=dns_timeout,
        )
    except asyncio.TimeoutError:
        raise SSRFError(f'域名解析超时: {host}')
    except socket.gaierror:
        raise SSRFError(f'域名无法解析: {host}')

    ips = tuple(sorted({info[4][0] for info in infos}))
    if not ips:
        raise SSRFError(f'域名无可用解析记录: {host}')
    for ip in ips:
        if is_blocked_ip(ip):
            raise SSRFError(f'域名 {host} 解析到内网/保留地址 {ip}，已拒绝')
    return ResolvedTarget(scheme, host, port, ips)


async def check_url(url: str) -> ResolvedTarget:
    """供 Chromium 子请求等只需校验、不需要下载的场景使用。"""
    return await resolve_checked(url)
