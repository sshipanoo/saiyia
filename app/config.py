import logging
from pydantic_settings import BaseSettings
from functools import lru_cache

_log = logging.getLogger("saiyia.config")

_INSECURE_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://voice:voice@localhost:5432/voice"

    # JWT
    secret_key: str = _INSECURE_SECRET
    access_token_expire_days: int = 7

    # --- Provider selection -------------------------------------------------
    # Each capability (chat / TTS / file-based ASR) can independently pick a
    # provider — you don't have to use the same vendor for all three. See
    # app/providers/ and the README for how to add a new provider.
    chat_provider: str = "dashscope"   # dashscope | openai | <custom, see README>
    tts_provider: str = "dashscope"    # dashscope | openai
    asr_provider: str = "dashscope"    # dashscope | openai

    # Alibaba Cloud Model Studio (DashScope)
    alibaba_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"

    # OpenAI (or any OpenAI-compatible endpoint — just point openai_base_url
    # at it, e.g. Groq/Together/DeepSeek/a local vLLM server all work for chat)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_chat_model: str = "gpt-4o-mini"
    openai_tts_model: str = "tts-1"
    openai_asr_model: str = "whisper-1"

    # Real-time streaming ASR is implemented as a transparent WebSocket relay
    # (see app/routers/proxy.py) — the server doesn't interpret the protocol,
    # it just forwards bytes/text both ways. That means it works with *any*
    # realtime speech API that speaks WebSocket, not just DashScope's; point
    # these two at whichever provider you want and the relay doesn't change.
    realtime_asr_ws_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    realtime_asr_auth_header: str = ""  # defaults to "Bearer {alibaba_api_key}" if left blank

    # This service's own public-facing address. File-based transcription is an
    # async task for some providers (e.g. DashScope), which need a public URL
    # to fetch the audio from, so temporary audio files get exposed as a URL.
    public_base_url: str = "http://localhost:8000"

    app_name: str = "saiyia"
    debug: bool = False

    # Rate limiting
    rate_limit_per_minute: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def _validate_settings(s: Settings) -> None:
    if not s.debug and s.secret_key == _INSECURE_SECRET:
        raise RuntimeError(
            "SECRET_KEY is using the insecure default value. "
            "You must set SECRET_KEY via an environment variable or .env in production"
        )
    if not s.debug and len(s.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters long")

    _providers_used = {s.chat_provider, s.tts_provider, s.asr_provider}
    if "dashscope" in _providers_used and not s.alibaba_api_key:
        _log.warning("ALIBABA_API_KEY is not set but a DashScope provider is selected")
    if "openai" in _providers_used and not s.openai_api_key:
        _log.warning("OPENAI_API_KEY is not set but an OpenAI provider is selected")


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    _validate_settings(s)
    return s
