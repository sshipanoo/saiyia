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

# 阿里云 DashScope 代理端点
DASHSCOPE_BASE = settings.dashscope_base_url

# 录音文件识别用的临时音频目录。DashScope 异步任务会从公网 URL 回源下载这些文件，
# 识别完成（或失败/超时）后立即删除。
TEMP_AUDIO_DIR = Path("/tmp/voice_asr_audio")
TEMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/chat/completions")
@limiter.limit("60/minute")
async def proxy_chat_completions(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """代理 LLM 聊天请求到阿里云 DashScope"""
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
    """通过 DashScope CosyVoice WebSocket 合成语音，返回完整音频二进制。

    CosyVoice 系列只支持 WebSocket（HTTP 同步端点会报 InvalidParameter）。协议：
    连接 → run-task → 收 task-started → continue-task(文本) + finish-task → 收音频二进制帧 + task-finished。
    """
    task_id = uuid.uuid4().hex
    audio = bytearray()
    headers = {"Authorization": f"Bearer {settings.alibaba_api_key}"}

    # websockets 不同版本 header 参数名不同（additional_headers / extra_headers）
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
    """与 _cosyvoice_tts 同协议，但请求 PCM 格式并把每个音频帧**边收边 yield**，
    用于流式播放（首字延迟≈首帧合成时长，而非整句合成时长）。

    返回裸 PCM（16-bit little-endian / mono / sample_rate），客户端无需解码即可投给
    AVAudioPlayerNode 增量播放。失败/无音频时直接结束（不 yield），由调用方/客户端回退。
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
    """CosyVoice 语音合成（WebSocket），直接返回音频二进制（audio/mpeg）。"""
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
    """CosyVoice 流式语音合成：边合成边把裸 PCM（16-bit LE / mono / 22050Hz）分块返回。

    与 /audio/tts（一次性 MP3）并存，专供客户端流式播放降低首字延迟。
    Content-Type 用 audio/L16 表明是裸 PCM，客户端按帧投给 AVAudioPlayerNode。
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
            # 流已开始无法再改状态码；直接结束，客户端按收到字节为空/不足回退系统 TTS

    return StreamingResponse(
        _gen(),
        media_type=f"audio/L16; rate={sample_rate}; channels=1",
    )


def _audio_sig(audio_id: str, exp: int) -> str:
    """对临时音频 URL 做 HMAC 签名（key=SECRET_KEY），防止 UUID 泄露被未授权访问。"""
    msg = f"{audio_id}:{exp}".encode()
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()


@router.get("/asr/audio/{audio_id}")
async def serve_asr_audio(audio_id: str, exp: int = 0, sig: str = ""):
    """把临时音频暴露给 DashScope 异步任务回源下载。

    无鉴权（DashScope 回源不带 token），改用 HMAC 签名 + 5 分钟过期作为临时凭证：
    URL 带 ?exp=&sig=，校验通过才返回文件。文件名随机 UUID、识别完成即删除。
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
    """从 paraformer-v2 录音文件识别结果里抽取纯文本（可含说话人前缀）。

    transcription_url 指向的 JSON 结构形如：
    {"transcripts": [{"text": "...", "sentences": [{"text": "...", "speaker_id": 0}, ...]}]}
    开启说话人分离时，逐句加上 "说话人N：" 前缀；否则直接用整段 text。
    """
    transcripts = payload.get("transcripts") or []
    if not transcripts:
        return ""
    first = transcripts[0]
    sentences = first.get("sentences") or []
    # 句子里带 speaker_id 时，按发言人分段
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
                parts.append(f"\n说话人{spk}：{txt}")
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
    """录音文件识别：base64 音频 → 临时公网 URL → DashScope paraformer-v2 异步任务 → 轮询取结果。

    之所以不再用旧的 `/services/asr/recognition` 同步端点：那个端点配 paraformer-realtime-v2
    会报 "task can not be null"（realtime 模型只能走 WebSocket）。录音文件识别（paraformer-v2）
    是整段音频识别、原生支持说话人分离，正好匹配 app「录完再识别」的流程。
    """
    if not settings.alibaba_api_key:
        raise HTTPException(status_code=503, detail="Service not configured")

    body = await request.json()
    audio_base64 = body.get("audio", "")
    audio_format = body.get("format", "m4a")
    enable_diarization = bool(body.get("diarization", False))

    _ALLOWED_AUDIO_FORMATS = {"m4a", "wav", "mp3", "pcm", "ogg", "flac", "aac"}
    if audio_format not in _ALLOWED_AUDIO_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {audio_format}")
    if not audio_base64:
        raise HTTPException(status_code=400, detail="Missing audio data")

    # 1. 落地临时音频文件，构造公网回源 URL
    audio_id = f"{uuid.uuid4().hex}.{audio_format}"
    audio_path = TEMP_AUDIO_DIR / audio_id
    try:
        audio_path.write_bytes(base64.b64decode(audio_base64))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 audio: {exc}")

    # 带 HMAC 签名 + 5 分钟过期，DashScope 回源时需带上 ?exp=&sig= 才能下载
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
            # 2. 提交异步任务
            submit = await client.post(
                f"{DASHSCOPE_BASE}/api/v1/services/audio/asr/transcription",
                headers={**headers, "X-DashScope-Async": "enable"},
                json={
                    "model": "paraformer-v2",
                    "input": {"file_urls": [file_url]},
                    "parameters": {
                        "language_hints": ["zh", "en"],
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

            # 3. 轮询任务（录音文件识别通常几秒内完成；最多约 30s）
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
        # 4. 不论成功失败都清理临时音频
        audio_path.unlink(missing_ok=True)


# ─── WebSocket 实时 ASR 中继 ─────────────────────────────────────────
# 客户端连 wss://<你的部署域名>/api/v1/asr/stream，
# 服务端透明代理到 wss://dashscope.aliyuncs.com/api-ws/v1/inference，
# 双向转发所有消息（文本 JSON 指令 + 二进制 PCM 音频帧）。

async def _ws_authenticate(ws: WebSocket) -> User | None:
    """从 WebSocket 握手的 Authorization header 中校验 JWT。
    WebSocket 不能用 FastAPI Depends，需手动提取。
    浏览器原生 WebSocket API 没法在握手时自定义请求头（不像 iOS 用 Starscream 能自由设置
    Authorization），所以网页端只能把 token 放在 query string 里；iOS 继续走 header，
    两条路径都支持，谁能用就用谁，不互相影响。"""
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
    """实时 ASR WebSocket 透明中继：客户端 ⇄ 服务端 ⇄ DashScope。"""
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
            """客户端 → DashScope"""
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
            """DashScope → 客户端"""
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
