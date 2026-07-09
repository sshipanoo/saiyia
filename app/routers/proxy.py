from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, FileResponse, Response
import httpx
import websockets
import asyncio
import base64
import uuid
import hmac
import hashlib
import time
import logging
from pathlib import Path

from app.config import get_settings
from app.routers.auth import get_current_user, User
from app.security import decode_token
from sqlalchemy import select
from app.ratelimit import limiter
from app.providers.chat import resolve_chat_endpoint
from app.providers.tts import get_tts_provider
from app.providers.asr import get_asr_provider

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("saiyia.asr")

# Temp directory for file-based transcription. Providers that need a public
# URL to fetch the audio from (e.g. DashScope's async task) get one via
# _write_temp_audio() below; files are deleted right after recognition
# finishes (success, failure, or timeout).
TEMP_AUDIO_DIR = Path("/tmp/voice_asr_audio")
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/chat/completions")
@limiter.limit("60/minute")
async def proxy_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Proxies an LLM chat request to whichever provider CHAT_PROVIDER selects.
    DashScope, OpenAI, and most other major LLM providers all expose an
    OpenAI-compatible /chat/completions endpoint, so this stays generic."""
    endpoint = resolve_chat_endpoint(settings)
    if not endpoint.api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.body()

    client = httpx.AsyncClient(timeout=60.0)

    req = client.build_request(
        "POST",
        f"{endpoint.base_url}{endpoint.completions_path}",
        headers={
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        },
        content=body,
    )
    response = await client.send(req, stream=True)

    async def stream_and_close():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_and_close(),
        status_code=response.status_code,
        headers={
            k: v for k, v in response.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        },
    )


@router.post("/audio/tts")
@limiter.limit("60/minute")
async def proxy_tts(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """One-shot speech synthesis via whichever provider TTS_PROVIDER selects,
    returns the audio bytes directly (audio/mpeg)."""
    provider = get_tts_provider(settings)

    body = await request.json()
    text = (body.get("input", {}) or {}).get("text", "")
    voice = body.get("voice", "longxiaochun")
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    logger.info("TTS request: provider=%s voice=%s chars=%d", settings.tts_provider, voice, len(text))
    try:
        audio = await asyncio.wait_for(provider.synthesize(text, voice), timeout=30.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="TTS timeout")
    except Exception as exc:
        logger.warning("TTS error: %s", exc)
        raise HTTPException(status_code=502, detail=f"TTS failed: {exc}")

    if not audio:
        raise HTTPException(status_code=502, detail="TTS returned empty audio")

    logger.info("TTS done: %d bytes", len(audio))
    return Response(content=audio, media_type="audio/mpeg")


@router.post("/audio/tts/stream")
@limiter.limit("60/minute")
async def proxy_tts_stream(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Streaming speech synthesis: returns raw PCM in chunks as it's
    synthesized, via whichever provider TTS_PROVIDER selects.

    Coexists with /audio/tts (one-shot MP3) — this one is specifically for
    clients that want to stream playback and reduce first-audio latency.
    Content-Type carries the provider's actual sample rate so the client
    doesn't have to hardcode it.
    """
    provider = get_tts_provider(settings)

    body = await request.json()
    text = (body.get("input", {}) or {}).get("text", "")
    voice = body.get("voice", "longxiaochun")
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    logger.info("TTS(stream) request: provider=%s voice=%s chars=%d", settings.tts_provider, voice, len(text))

    async def _gen():
        try:
            async for chunk in provider.synthesize_stream(text, voice):
                yield chunk
        except Exception as exc:
            logger.warning("TTS(stream) error: %s", exc)
            # Can't change the status code once streaming has started; just end
            # the stream and let the client fall back to on-device TTS if it
            # received empty/insufficient bytes

    return StreamingResponse(
        _gen(),
        media_type=f"audio/L16; rate={provider.sample_rate}; channels=1",
    )


def _audio_sig(audio_id: str, exp: int) -> str:
    """HMAC-signs a temporary audio URL (keyed by SECRET_KEY), to prevent
    unauthorized access even if the random file ID leaks."""
    msg = f"{audio_id}:{exp}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


@router.get("/asr/audio/{audio_id}")
async def serve_asr_audio(audio_id: str, exp: int = 0, sig: str = ""):
    """Exposes a temporary audio file for a provider's async task to fetch
    (used by DashScope-style providers; not all providers need this).

    Not JWT-authenticated (the upstream provider's fetch request carries no
    token) — instead uses an HMAC signature plus a 5-minute expiry as a
    short-lived credential: the URL carries ?exp=&sig=, and the file is only
    served if that checks out. Filenames are random UUIDs and get deleted
    right after recognition completes.
    """
    if "/" in audio_id or ".." in audio_id:
        raise HTTPException(status_code=400, detail="Bad audio id")
    if exp <= 0 or not sig:
        raise HTTPException(status_code=403, detail="Missing signature")
    if int(time.time()) > exp:
        raise HTTPException(status_code=403, detail="URL expired")
    if not hmac.compare_digest(sig, _audio_sig(audio_id, exp)):
        raise HTTPException(status_code=403, detail="Bad signature")
    path = TEMP_AUDIO_DIR / audio_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path)


def _make_temp_audio_writer(written_paths: list[Path]):
    """Returns a callback that writes audio to the temp dir and returns a
    signed, time-limited public URL for it — used by DashScope-style ASR
    providers that need to fetch the audio themselves rather than accepting
    bytes directly. Any path it writes gets appended to `written_paths` so the
    caller can clean it up afterward, regardless of which provider ran."""

    async def _write(audio_bytes: bytes, audio_format: str) -> str:
        audio_id = f"{uuid.uuid4().hex}.{audio_format}"
        audio_path = TEMP_AUDIO_DIR / audio_id
        audio_path.write_bytes(audio_bytes)
        written_paths.append(audio_path)
        exp = int(time.time()) + 300
        sig = _audio_sig(audio_id, exp)
        return f"{settings.public_base_url.rstrip('/')}/api/v1/asr/audio/{audio_id}?exp={exp}&sig={sig}"

    return _write


@router.post("/asr")
@limiter.limit("60/minute")
async def proxy_asr(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Full-recording transcription via whichever provider ASR_PROVIDER
    selects (DashScope: async task + polling + speaker diarization; OpenAI
    Whisper: a single synchronous multipart request, no diarization)."""
    body = await request.json()
    audio_base64 = body.get("audio", "")
    audio_format = body.get("format", "m4a")
    enable_diarization = bool(body.get("diarization", False))
    # Language hints are left to the caller rather than hardcoded to zh/en —
    # see the README's language support section for what each provider covers
    language_hints = body.get("language_hints") or ["zh", "en"]
    if not isinstance(language_hints, list) or not all(isinstance(x, str) for x in language_hints):
        raise HTTPException(status_code=400, detail="language_hints must be a list of strings")

    _ALLOWED_AUDIO_FORMATS = {"m4a", "wav", "mp3", "pcm", "ogg", "flac", "aac"}
    if audio_format not in _ALLOWED_AUDIO_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {audio_format}")
    if not audio_base64:
        raise HTTPException(status_code=400, detail="Missing audio data")

    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}")

    # Only providers that fetch the audio themselves (DashScope) will append
    # to this list via the callback; providers that take bytes directly
    # (OpenAI Whisper) leave it empty and there's nothing to clean up.
    written_paths: list[Path] = []
    provider = get_asr_provider(settings, _make_temp_audio_writer(written_paths))
    logger.info("ASR submit: provider=%s format=%s diarization=%s", settings.asr_provider, audio_format, enable_diarization)

    try:
        text = await provider.transcribe(audio_bytes, audio_format, language_hints, enable_diarization)
        logger.info("ASR done: chars=%d", len(text))
        return {"text": text}
    except Exception as exc:
        logger.warning("ASR error: %s", exc)
        return {"text": "", "error": str(exc)}
    finally:
        for p in written_paths:
            p.unlink(missing_ok=True)


# --- Real-time ASR WebSocket relay -----------------------------------------
# The client connects to wss://<your-deployment-domain>/api/v1/asr/stream,
# and the server transparently proxies to REALTIME_ASR_WS_URL (DashScope by
# default), forwarding every message in both directions (JSON text
# instructions + binary PCM audio frames). Because the server doesn't
# interpret the protocol at all, this works with any realtime speech API that
# speaks WebSocket — just point REALTIME_ASR_WS_URL and
# REALTIME_ASR_AUTH_HEADER at a different provider.

async def _ws_authenticate(ws: WebSocket) -> User | None:
    """Validates the JWT from the WebSocket handshake's Authorization header.
    WebSocket connections can't use FastAPI's Depends, so this is extracted
    manually. Native browser WebSocket can't set custom headers at handshake
    time (unlike, say, iOS's Starscream library, which can), so web clients
    have to pass the token via query string instead; other clients keep using
    the header. Both paths are supported and don't interfere with each other.
    """
    auth = ws.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ws.query_params.get("token", "")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        return None
    try:
        uid = int(payload["sub"])
    except (ValueError, TypeError):
        return None

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == uid))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            return None
        if payload.get("tv", 0) != user.token_version:
            return None
        return user


def _realtime_asr_auth_header() -> str:
    if settings.realtime_asr_auth_header:
        return settings.realtime_asr_auth_header
    return f"Bearer {settings.alibaba_api_key}"


@router.websocket("/asr/stream")
async def asr_stream_relay(ws: WebSocket):
    """Real-time ASR WebSocket transparent relay: client <-> this server <-> upstream provider."""
    user = await _ws_authenticate(ws)
    if not user:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    logger.info("ASR stream: user=%d connected", user.id)

    headers = {"Authorization": _realtime_asr_auth_header()}
    upstream = None
    try:
        try:
            upstream = await websockets.connect(
                settings.realtime_asr_ws_url, additional_headers=headers, max_size=None
            )
        except TypeError:
            upstream = await websockets.connect(
                settings.realtime_asr_ws_url, extra_headers=headers, max_size=None
            )

        async def client_to_upstream():
            """Client -> upstream provider"""
            try:
                while True:
                    data = await ws.receive()
                    if data["type"] == "websocket.receive":
                        if "text" in data and data["text"]:
                            await upstream.send(data["text"])
                        elif "bytes" in data and data["bytes"]:
                            await upstream.send(data["bytes"])
                    elif data["type"] == "websocket.disconnect":
                        break
            except (WebSocketDisconnect, Exception):
                pass

        async def upstream_to_client():
            """Upstream provider -> client"""
            try:
                async for msg in upstream:
                    if isinstance(msg, (bytes, bytearray)):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)
            except (WebSocketDisconnect, Exception):
                pass

        done, pending = await asyncio.wait(
            [asyncio.create_task(client_to_upstream()),
             asyncio.create_task(upstream_to_client())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    except Exception as exc:
        logger.warning("ASR stream error: user=%d %s", user.id, exc)
    finally:
        if upstream:
            await upstream.close()
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("ASR stream: user=%d disconnected", user.id)
