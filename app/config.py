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

    # 阿里云百炼（DashScope）：对话、语音识别、语音合成都走这一个 key
    alibaba_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"

    # 服务自身的公网地址。录音文件识别是异步任务，DashScope 需要从一个公网 URL
    # 回源下载音频，所以要把临时音频文件暴露成 URL 供它下载。
    public_base_url: str = "http://localhost:8000"

    app_name: str = "赛鸭"
    debug: bool = False

    # 限流
    rate_limit_per_minute: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


def _validate_settings(s: Settings) -> None:
    if not s.debug and s.secret_key == _INSECURE_SECRET:
        raise RuntimeError(
            "SECRET_KEY 使用了不安全的默认值。生产环境必须通过环境变量或 .env 设置 SECRET_KEY"
        )
    if not s.debug and len(s.secret_key) < 32:
        raise RuntimeError("SECRET_KEY 长度不足 32 字符")
    if not s.alibaba_api_key:
        _log.warning("ALIBABA_API_KEY 未设置，对话/语音接口会全部失败")


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    _validate_settings(s)
    return s
