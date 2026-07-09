from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse, FileResponse, Response
import httpx
import websockets
import json
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
from app.database import get_db
from sqlalchemy import select
from app.ratelimit import limiter

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("saiyia.asr")

# Alibaba Cloud DashScope proxy endpoint
DASHSCOPE_BASE = settings.dashscope_base_url

# Temp directory for file-based transcription. DashScope's async task fetches
# these files from a public URL; they're deleted right after recognition
# finishes (success, failure, or timeout).
TEMP_AUDIO_DIR = Path("/tmp/voice_asr_audio")
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/chat/completions")
@limiter.limit("60/minute")
async def proxy_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Proxies an LLM chat request to Alibaba Cloud DashScope."""
    if not settings.alibaba_api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.body()

    client = httpx.AsyncClient(timeout=60.0)

    req = client.build_request(
        "POST",
        f"{DASHSCOPE_BASE}/compatible-mode/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.alibaba_api_key}",
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


DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


async def _cosyvoice_tts(text: str, voice: str, audio_format: str = "mp3", sample_rate: int = 22050) -> bytes:
    """Synthesizes speech via DashScope's CosyVoice WebSocket API, returning the
    complete audio as bytes.

    CosyVoice models only support WebSocket (the synchronous HTTP endpoint
    returns InvalidParameter). Protocol: connect -> run-task -> receive
    task-started -> continue-task(text) + finish-task -> receive binary audio
    frames + task-finished.
    """
    task_id = uuid.uuid4().hex
    audio = bytearray()
    headers = {"Authorization": f"Bearer {settings.alibaba_api_key}"}

    # Different versions of the `websockets` package use different header
    # kwarg names (additional_headers / extra_headers)
    try:
        ws = await websockets.connect(DASHSCOPE_WS_URL, additional_headers=headers, max_size=None)
    except TypeError:
        ws = await websockets.connect(DASHSCOPE_WS_URL, extra_headers=headers, max_size=None)

    try:
        await ws.send(json.dumps({
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                "model": "cosyvoice-v1",
                "parameters": {"text_type": "PlainText", "voice": voice,
                               "format": audio_format, "sample_rate": sample_rate},
                "input": {},
            },
        }))
        started = False
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                audio.extend(msg)
                continue
            event = json.loads(msg)
            ev = event.get("header", {}).get("event")
            if ev == "task-started" and not started:
                started = True
                await ws.send(json.dumps({
                    "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {"text": text}},
                }))
                await ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {}},
                }))
            elif ev == "task-finished":
                break
            elif ev == "task-failed":
                logger.warning("CosyVoice TTS failed: %s", json.dumps(event.get("header", {}), ensure_ascii=False))
                break
    finally:
        await ws.close()

    return bytes(audio)


async def _cosyvoice_tts_pcm_stream(text: str, voice: str, sample_rate: int = 22050):
    """Same protocol as _cosyvoice_tts, but requests PCM and yields each audio
    frame **as it arrives**, for streaming playback (first-audio latency is
    roughly the first chunk's synthesis time, not the whole sentence's).

    Yields raw PCM (16-bit little-endian / mono / sample_rate) that the client
    can feed straight into incremental playback (e.g. AVAudioPlayerNode)
    without decoding. On failure or empty audio it just ends without
    yielding anything, and it's up to the caller/client to fall back.
    """
    task_id = uuid.uuid4().hex
    headers = {"Authorization": f"Bearer {settings.alibaba_api_key}"}

    try:
        ws = await websockets.connect(DASHSCOPE_WS_URL, additional_headers=headers, max_size=None)
    except TypeError:
        ws = await websockets.connect(DASHSCOPE_WS_URL, extra_headers=headers, max_size=None)

    try:
        await ws.send(json.dumps({
            "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "tts", "function": "SpeechSynthesizer",
                "model": "cosyvoice-v1",
                "parameters": {"text_type": "PlainText", "voice": voice,
                               "format": "pcm", "sample_rate": sample_rate},
                "input": {},
            },
        }))
        started = False
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                yield bytes(msg)
                continue
            event = json.loads(msg)
            ev = event.get("header", {}).get("event")
            if ev == "task-started" and not started:
                started = True
                await ws.send(json.dumps({
                    "header": {"action": "continue-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {"text": text}},
                }))
                await ws.send(json.dumps({
                    "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                    "payload": {"input": {}},
                }))
            elif ev == "task-finished":
                break
            elif ev == "task-failed":
                logger.warning("CosyVoice TTS(stream) failed: %s", json.dumps(event.get("header", {}), ensure_ascii=False))
                break
    finally:
        await ws.close()


@router.post("/audio/tts")
@limiter.limit("60/minute")
async def proxy_tts(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """CosyVoice speech synthesis (over WebSocket), returns the audio bytes
    directly (audio/mpeg)."""
    if not settings.alibaba_api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.json()
    text = (body.get("input", {}) or {}).get("text", "")
    voice = body.get("voice", "longxiaochun")
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    logger.info("TTS request: voice=%s chars=%d", voice, len(text))
    try:
        audio = await asyncio.wait_for(_cosyvoice_tts(text, voice), timeout=30.0)
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
    """CosyVoice streaming speech synthesis: returns raw PCM (16-bit LE / mono
    / 22050Hz) in chunks as it's synthesized.

    Coexists with /audio/tts (one-shot MP3) — this one is specifically for
    clients that want to stream playback and reduce first-audio latency.
    Content-Type is audio/L16 to signal raw PCM; the client feeds it frame by
    frame into something like AVAudioPlayerNode.
    """
    if not settings.alibaba_api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.json()
    text = (body.get("input", {}) or {}).get("text", "")
    voice = body.get("voice", "longxiaochun")
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    sample_rate = 22050
    logger.info("TTS(stream) request: voice=%s chars=%d", voice, len(text))

    async def _gen():
        try:
            async for chunk in _cosyvoice_tts_pcm_stream(text, voice, sample_rate):
                yield chunk
        except Exception as exc:
            logger.warning("TTS(stream) error: %s", exc)
            # Can't change the status code once streaming has started; just end
            # the stream and let the client fall back to on-device TTS if it
            # received empty/insufficient bytes

    return StreamingResponse(
        _gen(),
        media_type=f"audio/L16; rate={sample_rate}; channels=1",
    )


def _audio_sig(audio_id: str, exp: int) -> str:
    """HMAC-signs a temporary audio URL (keyed by SECRET_KEY), to prevent
    unauthorized access even if the random file ID leaks."""
    msg = f"{audio_id}:{exp}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


@router.get("/asr/audio/{audio_id}")
async def serve_asr_audio(audio_id: str, exp: int = 0, sig: str = ""):
    """Exposes a temporary audio file for DashScope's async task to fetch.

    Not JWT-authenticated (DashScope's fetch request carries no token) —
    instead uses an HMAC signature plus a 5-minute expiry as a short-lived
    credential: the URL carries ?exp=&sig=, and the file is only served if
    that checks out. Filenames are random UUIDs and get deleted right after
    recognition completes.
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


def _extract_text_from_transcription(payload: dict) -> str:
    """Extracts plain text (optionally with speaker labels) from a
    paraformer-v2 file-transcription result.

    The JSON at transcription_url looks like:
    {"transcripts": [{"text": "...", "sentences": [{"text": "...", "speaker_id": 0}, ...]}]}
    When diarization is enabled, each sentence gets a "Speaker N:" prefix when
    the speaker changes; otherwise we just use the whole-text field.
    """
    transcripts = payload.get("transcripts") or []
    if not transcripts:
        return ""
    first = transcripts[0]
    sentences = first.get("sentences") or []
    # Group by speaker when sentences carry a speaker_id
    has_speaker = any("speaker_id" in s for s in sentences)
    if has_speaker:
        parts = []
        last_speaker = None
        for s in sentences:
            spk = s.get("speaker_id")
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            if spk != last_speaker:
                parts.append(f"\nSpeaker {spk}: {txt}")
                last_speaker = spk
            else:
                parts.append(txt)
        return "".join(parts).strip()
    return (first.get("text") or "").strip()


@router.post("/asr")
@limiter.limit("60/minute")
async def proxy_asr(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Full-recording transcription: base64 audio -> temporary public URL ->
    DashScope paraformer-v2 async task -> poll for the result.

    Why not the older synchronous `/services/asr/recognition` endpoint:
    that one errors with "task can not be null" when configured for
    paraformer-realtime-v2 (the realtime model only works over WebSocket).
    File-based transcription (paraformer-v2) recognizes a whole recording at
    once and natively supports speaker diarization, which matches a
    "record, then transcribe" flow.
    """
    if not settings.alibaba_api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.json()
    audio_base64 = body.get("audio", "")
    audio_format = body.get("format", "m4a")
    enable_diarization = bool(body.get("diarization", False))
    # Language hints are left to the caller rather than hardcoded to zh/en —
    # paraformer-v2 supports more languages than that (see the README's
    # language support section); hardcoding would arbitrarily lock out
    # non-Chinese/English users
    language_hints = body.get("language_hints") or ["zh", "en"]
    if not isinstance(language_hints, list) or not all(isinstance(x, str) for x in language_hints):
        raise HTTPException(status_code=400, detail="language_hints must be a list of strings")

    _ALLOWED_AUDIO_FORMATS = {"m4a", "wav", "mp3", "pcm", "ogg", "flac", "aac"}
    if audio_format not in _ALLOWED_AUDIO_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {audio_format}")
    if not audio_base64:
        raise HTTPException(status_code=400, detail="Missing audio data")

    # 1. Write the temp audio file and build a public URL for it
    audio_id = f"{uuid.uuid4().hex}.{audio_format}"
    audio_path = TEMP_AUDIO_DIR / audio_id
    try:
        audio_path.write_bytes(base64.b64decode(audio_base64))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}")

    # HMAC-signed with a 5-minute expiry; DashScope's fetch must include ?exp=&sig=
    _exp = int(time.time()) + 300
    _sig = _audio_sig(audio_id, _exp)
    file_url = f"{settings.public_base_url.rstrip('/')}/api/v1/asr/audio/{audio_id}?exp={_exp}&sig={_sig}"
    logger.info("ASR submit: audio_id=%s diarization=%s", audio_id, enable_diarization)

    headers = {
        "Authorization": f"Bearer {settings.alibaba_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 2. Submit the async task
            submit = await client.post(
                f"{DASHSCOPE_BASE}/api/v1/services/audio/asr/transcription",
                headers={**headers, "X-DashScope-Async": "enable"},
                json={
                    "model": "paraformer-v2",
                    "input": {"file_urls": [file_url]},
                    "parameters": {
                        "language_hints": language_hints,
                        "diarization_enabled": enable_diarization,
                    },
                },
            )
            if submit.status_code != 200:
                logger.warning("ASR submit failed: %s %s", submit.status_code, submit.text)
                return {"text": "", "error": submit.text, "status_code": submit.status_code}

            task_id = submit.json().get("output", {}).get("task_id")
            if not task_id:
                return {"text": "", "error": "no task_id", "raw": submit.json()}

            # 3. Poll for completion (file transcription usually finishes within
            # a few seconds; cap at roughly 30s)
            for _ in range(30):
                await asyncio.sleep(1.0)
                query = await client.post(
                    f"{DASHSCOPE_BASE}/api/v1/tasks/{task_id}",
                    headers=headers,
                )
                out = query.json().get("output", {})
                status = out.get("task_status")
                if status == "SUCCEEDED":
                    results = out.get("results") or []
                    if not results:
                        return {"text": "", "error": "empty results", "raw": query.json()}
                    trans_url = results[0].get("transcription_url")
                    if not trans_url:
                        return {"text": "", "error": "no transcription_url", "raw": query.json()}
                    trans = await client.get(trans_url)
                    text = _extract_text_from_transcription(trans.json())
                    logger.info("ASR done: task=%s chars=%d", task_id, len(text))
                    return {"text": text}
                if status in ("FAILED", "CANCELED"):
                    logger.warning("ASR task %s: %s", task_id, query.json())
                    return {"text": "", "error": f"task {status}", "raw": query.json()}
            return {"text": "", "error": "timeout polling ASR task"}
    finally:
        # 4. Clean up the temp audio file regardless of success or failure
        audio_path.unlink(missing_ok=True)


# --- Real-time ASR WebSocket relay -----------------------------------------
# The client connects to wss://<your-deployment-domain>/api/v1/asr/stream,
# and the server transparently proxies to
# wss://dashscope.aliyuncs.com/api-ws/v1/inference, forwarding every message
# in both directions (JSON text instructions + binary PCM audio frames).

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


@router.websocket("/asr/stream")
async def asr_stream_relay(ws: WebSocket):
    """Real-time ASR WebSocket transparent relay: client <-> this server <-> DashScope."""
    user = await _ws_authenticate(ws)
    if not user:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    logger.info("ASR stream: user=%d connected", user.id)

    headers = {"Authorization": f"Bearer {settings.alibaba_api_key}"}
    upstream = None
    try:
        try:
            upstream = await websockets.connect(
                DASHSCOPE_WS_URL, additional_headers=headers, max_size=None
            )
        except TypeError:
            upstream = await websockets.connect(
                DASHSCOPE_WS_URL, extra_headers=headers, max_size=None
            )

        async def client_to_upstream():
            """Client -> DashScope"""
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
            """DashScope -> Client"""
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
