"""Chat completion provider config.

Chat doesn't need a per-provider adapter class the way TTS/ASR do: DashScope,
OpenAI, and most other major LLM providers (Groq, Together, DeepSeek, Mistral,
a self-hosted vLLM server, etc.) all expose an OpenAI-compatible
`/chat/completions` endpoint. So instead of one class per vendor, this is just
a (base_url, api_key) resolver — the actual HTTP forwarding in
routers/proxy.py stays generic.

To add a provider that isn't in this list, set:
    CHAT_PROVIDER=openai
    OPENAI_BASE_URL=https://your-provider.example.com/v1
    OPENAI_API_KEY=...
(reusing the "openai" slot works for anything OpenAI-compatible — you don't
need to add a new provider name unless you want it selectable by its own name).
"""

from dataclasses import dataclass

from app.config import Settings


@dataclass
class ChatEndpoint:
    base_url: str
    api_key: str
    completions_path: str


def resolve_chat_endpoint(settings: Settings) -> ChatEndpoint:
    if settings.chat_provider == "openai":
        return ChatEndpoint(
            base_url=settings.openai_base_url.rstrip("/"),
            api_key=settings.openai_api_key,
            completions_path="/chat/completions",
        )
    if settings.chat_provider == "dashscope":
        return ChatEndpoint(
            base_url=settings.dashscope_base_url.rstrip("/"),
            api_key=settings.alibaba_api_key,
            completions_path="/compatible-mode/v1/chat/completions",
        )
    raise ValueError(f"Unknown chat_provider: {settings.chat_provider!r}")
