"""API Key 鉴权。支持 X-API-Key 头或 Authorization: Bearer，恒定时间比较。"""
import hmac

from fastapi import Header, HTTPException, status

from .settings import settings


async def require_api_key(
    x_api_key: str = Header(default=None, alias='X-API-Key'),
    authorization: str = Header(default=None),
) -> str:
    # 未配置 Key 时关闭鉴权，仅限受信内网部署
    if not settings.API_KEYS:
        return 'anonymous'

    presented = x_api_key
    if not presented and authorization:
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() == 'bearer':
            presented = token.strip()

    if presented and any(hmac.compare_digest(presented, k) for k in settings.API_KEYS):
        return presented

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='无效或缺失的 API Key',
        headers={'WWW-Authenticate': 'Bearer'},
    )
