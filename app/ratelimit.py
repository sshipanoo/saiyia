"""Endpoint rate limiter.

Uses slowapi's in-memory storage (fine for a single process — a hardware
gateway doesn't need an extra dependency like Redis for this). Authenticated
requests are rate-limited by user_id first, falling back to IP when unauthenticated.
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
