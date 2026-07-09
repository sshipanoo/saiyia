"""Text-to-speech providers.

Unlike chat, TTS vendors don't share a common wire protocol, so each provider
gets its own adapter implementing the same small interface:

    async def synthesize(text, voice) -> bytes                    # one-shot, MP3
    def synthesize_stream(text, voice) -> AsyncIterator[bytes]     # streaming, raw PCM
    sample_rate: int                                               # PCM sample rate for the stream

To add a new provider: implement TTSProvider, register it in get_tts_provider()
below, and wire up a `tts_provider` value in config.py.
"""

import json
import logging
import uuid
from typing import AsyncIterator, Protocol

import httpx
import websockets

from app.config import Settings

logger = logging.getLogger("saiyia.tts")

DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


class TTSProvider(Protocol):
    sample_rate: int

    async def synthesize(self, text: str, voice: str) -> bytes: ...

    def synthesize_stream(self, text: str, voice: str) -> AsyncIterator[bytes]: ...


class DashScopeTTS:
    """DashScope CosyVoice, over its WebSocket synthesis protocol.

    CosyVoice only supports WebSocket (the synchronous HTTP endpoint returns
    InvalidParameter). Protocol: connect -> run-task -> receive task-started ->
    continue-task(text) + finish-task -> receive binary audio frames + task-finished.
    """

    sample_rate = 22050

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _connect(self):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # Different `websockets` versions use different header kwarg names
        try:
            return await websockets.connect(DASHSCOPE_WS_URL, additional_headers=headers, max_size=None)
        except TypeError:
            return await websockets.connect(DASHSCOPE_WS_URL, extra_headers=headers, max_size=None)

    async def synthesize(self, text: str, voice: str) -> bytes:
        audio = bytearray()
        async for chunk in self._run(text, voice, audio_format="mp3", sample_rate=22050):
            audio.extend(chunk)
        return bytes(audio)

    async def synthesize_stream(self, text: str, voice: str) -> AsyncIterator[bytes]:
        async for chunk in self._run(text, voice, audio_format="pcm", sample_rate=self.sample_rate):
            yield chunk

    async def _run(self, text: str, voice: str, audio_format: str, sample_rate: int) -> AsyncIterator[bytes]:
        task_id = uuid.uuid4().hex
        ws = await self._connect()
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
                    logger.warning("DashScope TTS failed: %s", json.dumps(event.get("header", {}), ensure_ascii=False))
                    break
        finally:
            await ws.close()


class OpenAITTS:
    """OpenAI's /audio/speech endpoint. `response_format=pcm` returns raw
    24kHz/16-bit/mono PCM and the HTTP response itself is already chunked as
    it's generated, so streaming is just relaying the response body."""

    sample_rate = 24000

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def synthesize(self, text: str, voice: str) -> bytes:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "input": text, "voice": voice or "alloy", "response_format": "mp3"},
            )
            resp.raise_for_status()
            return resp.content

    async def synthesize_stream(self, text: str, voice: str) -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"model": self.model, "input": text, "voice": voice or "alloy", "response_format": "pcm"},
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    yield chunk


def get_tts_provider(settings: Settings) -> TTSProvider:
    if settings.tts_provider == "openai":
        return OpenAITTS(settings.openai_api_key, settings.openai_base_url, settings.openai_tts_model)
    if settings.tts_provider == "dashscope":
        return DashScopeTTS(settings.alibaba_api_key)
    raise ValueError(f"Unknown tts_provider: {settings.tts_provider!r}")
