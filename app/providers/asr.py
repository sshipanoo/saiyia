"""File-based (whole-recording) speech-to-text providers.

Real-time streaming ASR is handled separately (see routers/proxy.py's
WebSocket relay) — it's a transparent byte/text forwarder, not something that
needs a Python-level adapter, so it's configured directly via
REALTIME_ASR_WS_URL / REALTIME_ASR_AUTH_HEADER rather than through this module.

To add a new provider: implement ASRProvider and register it in
get_asr_provider() below.
"""

import logging
from typing import Awaitable, Callable, Protocol

import httpx

from app.config import Settings

logger = logging.getLogger("saiyia.asr")

# Callback the DashScope provider uses to get a temporary public URL for the
# audio it needs to upload — DashScope's async transcription task fetches the
# file itself rather than accepting raw bytes, so the caller (routers/proxy.py,
# which owns the temp-file-serving endpoint) has to supply this.
PublicUrlFactory = Callable[[bytes, str], Awaitable[str]]


class ASRProvider(Protocol):
    async def transcribe(
        self, audio_bytes: bytes, audio_format: str, language_hints: list[str], diarization: bool
    ) -> str: ...


class DashScopeASR:
    """DashScope paraformer-v2: async task submitted with a public file URL,
    polled until it completes. Natively supports speaker diarization."""

    def __init__(self, api_key: str, base_url: str, public_url_factory: PublicUrlFactory):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.public_url_factory = public_url_factory

    async def transcribe(
        self, audio_bytes: bytes, audio_format: str, language_hints: list[str], diarization: bool
    ) -> str:
        file_url = await self.public_url_factory(audio_bytes, audio_format)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            submit = await client.post(
                f"{self.base_url}/api/v1/services/audio/asr/transcription",
                headers={**headers, "X-DashScope-Async": "enable"},
                json={
                    "model": "paraformer-v2",
                    "input": {"file_urls": [file_url]},
                    "parameters": {"language_hints": language_hints, "diarization_enabled": diarization},
                },
            )
            if submit.status_code != 200:
                logger.warning("DashScope ASR submit failed: %s %s", submit.status_code, submit.text)
                return ""

            task_id = submit.json().get("output", {}).get("task_id")
            if not task_id:
                return ""

            import asyncio
            for _ in range(30):
                await asyncio.sleep(1.0)
                query = await client.post(f"{self.base_url}/api/v1/tasks/{task_id}", headers=headers)
                out = query.json().get("output", {})
                status = out.get("task_status")
                if status == "SUCCEEDED":
                    results = out.get("results") or []
                    if not results:
                        return ""
                    trans_url = results[0].get("transcription_url")
                    if not trans_url:
                        return ""
                    trans = await client.get(trans_url)
                    return _extract_text_from_dashscope_transcription(trans.json())
                if status in ("FAILED", "CANCELED"):
                    logger.warning("DashScope ASR task %s: %s", task_id, query.json())
                    return ""
            logger.warning("DashScope ASR task %s timed out", task_id)
            return ""


def _extract_text_from_dashscope_transcription(payload: dict) -> str:
    """DashScope's transcription_url points to JSON shaped like:
    {"transcripts": [{"text": "...", "sentences": [{"text": "...", "speaker_id": 0}, ...]}]}
    With diarization on, each sentence gets a "Speaker N:" prefix when the
    speaker changes; otherwise the whole-text field is used as-is."""
    transcripts = payload.get("transcripts") or []
    if not transcripts:
        return ""
    first = transcripts[0]
    sentences = first.get("sentences") or []
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


class OpenAIWhisperASR:
    """OpenAI's /audio/transcriptions endpoint (Whisper). Synchronous, takes
    the audio directly as multipart form data — no public URL hosting needed,
    which is simpler than DashScope's async-task-plus-polling flow. Does not
    support speaker diarization; the `diarization` flag is accepted for
    interface compatibility but has no effect."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def transcribe(
        self, audio_bytes: bytes, audio_format: str, language_hints: list[str], diarization: bool
    ) -> str:
        if diarization:
            logger.info("OpenAIWhisperASR: diarization was requested but Whisper doesn't support it; ignoring")

        files = {"file": (f"audio.{audio_format}", audio_bytes)}
        data = {"model": self.model}
        # Whisper takes a single ISO-639-1 language code, not a list of hints;
        # use the first hint if one was given and let it auto-detect otherwise
        if language_hints:
            data["language"] = language_hints[0]

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            return resp.json().get("text", "")


def get_asr_provider(settings: Settings, public_url_factory: PublicUrlFactory) -> ASRProvider:
    if settings.asr_provider == "openai":
        return OpenAIWhisperASR(settings.openai_api_key, settings.openai_base_url, settings.openai_asr_model)
    if settings.asr_provider == "dashscope":
        return DashScopeASR(settings.alibaba_api_key, settings.dashscope_base_url, public_url_factory)
    raise ValueError(f"Unknown asr_provider: {settings.asr_provider!r}")
