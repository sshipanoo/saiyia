"""接口限流器。

用 slowapi 内存存储（单进程够用，硬件网关场景不需要 Redis 这种额外依赖）。
认证接口优先按 user_id 限流，未认证时回退到 IP。
"""
from slowapi import Limiter
from slowapi.util import get_remote_address


def _user_or_ip_key(request) -> str:
    from app.security import decode_token
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        payload = decode_token(auth[7:])
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    return get_remote_address(request)


limiter = Limiter(key_func=_user_or_ip_key, strategy="fixed-window")
