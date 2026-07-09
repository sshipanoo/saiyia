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

    # Alibaba Cloud Model Studio (DashScope): chat, speech recognition, and
    # speech synthesis all go through this one key
    alibaba_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"

    # This service's own public-facing address. File-based transcription is an
    # async task, and DashScope needs a public URL to fetch the audio from, so
    # temporary audio files need to be exposed as a URL for it to download.
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
    if not s.alibaba_api_key:
        _log.warning("ALIBABA_API_KEY is not set — chat/speech endpoints will all fail")


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    _validate_settings(s)
    return s
