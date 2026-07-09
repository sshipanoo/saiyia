# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

An open-source voice AI gateway server.

It does one thing: give any client that can open a WebSocket or make an HTTP request (phone, web, ESP32-class hardware) a unified account system plus a proxy for AI chat / speech recognition / speech synthesis, without that client ever holding a third-party AI provider's key. A good fit for hardware projects — e.g. a companion robot with a microphone and speaker.

## Design choices

Kept deliberately minimal — it only does "accounts + AI capability proxy": no payment/subscription system (the `User` model has no subscription fields at all, every account is equal, only rate-limited), no multi-device data sync, no admin panel. Whether to charge, how to charge, whether to persist conversation history — all of that is left for you to decide and implement on top.

## Provider support

Chat, speech-to-text, and text-to-speech each pick a provider **independently** — you're not locked into one vendor for everything. Set `CHAT_PROVIDER` / `ASR_PROVIDER` / `TTS_PROVIDER` in your `.env`:

| Provider | Chat | File-based ASR | Streaming TTS | Notes |
|---|---|---|---|---|
| `dashscope` (default) | ✅ | ✅ (native speaker diarization) | ✅ | Alibaba Cloud Model Studio |
| `openai` | ✅ | ✅ (Whisper, no diarization) | ✅ | Also works with any OpenAI-compatible endpoint (Groq, Together, DeepSeek, a self-hosted vLLM server, etc.) for chat by pointing `OPENAI_BASE_URL` elsewhere |

**Adding a provider isn't a fork-the-whole-project affair** — see `app/providers/`: `chat.py` just resolves a `(base_url, api_key)` pair (most LLM providers are OpenAI-compatible, so this usually needs no new code at all), while `tts.py` and `asr.py` each define a small `Protocol` interface you implement once per vendor. Real-time streaming ASR (`WS /asr/stream`) needs no adapter at all — it's a transparent byte/text relay, so it works with *any* WebSocket-based realtime speech API; just point `REALTIME_ASR_WS_URL` / `REALTIME_ASR_AUTH_HEADER` at it.

## API surface

| Endpoint | Description |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | Account system, JWT auth, `token_version` mechanism supports instant revocation of old tokens on logout |
| `POST /api/v1/chat/completions` | Proxies to whichever provider `CHAT_PROVIDER` selects (OpenAI-compatible format, streaming supported) |
| `POST /api/v1/audio/tts` | One-shot speech synthesis, returns a complete MP3 |
| `POST /api/v1/audio/tts/stream` | Streaming speech synthesis, emits raw PCM as it's generated (sample rate depends on the provider — carried in the response's `Content-Type`), low first-byte latency, good for play-as-you-receive |
| `POST /api/v1/asr` | Full-recording transcription |
| `WS /api/v1/asr/stream` | Real-time streaming speech recognition relay, text comes back as you speak |

## Quick start

```bash
cp .env.example .env   # fill in SECRET_KEY, ALIBABA_API_KEY (or OPENAI_API_KEY), DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## Hardware integration guide (e.g. ESP32)

### Auth

Call `/api/v1/auth/register` or `/login` first to get a JWT, then send it as `Authorization: Bearer <token>` on every request.

If your WebSocket client can't set custom headers on the handshake (this is true of native browser WebSocket), you can pass the token as a query string instead: `wss://.../asr/stream?token=xxx` — the server accepts either.

### Real-time speech recognition protocol

`WS /api/v1/asr/stream` is a **transparent relay** to whichever WebSocket endpoint `REALTIME_ASR_WS_URL` points at (Alibaba Cloud DashScope's paraformer-realtime-v2 by default) — the server only handles auth and forwarding, it doesn't touch message content. With the default DashScope provider, after connecting:

1. Send a JSON text frame to start a recognition task:

```json
{
  "header": { "action": "run-task", "task_id": "<32-char hex random id>", "streaming": "duplex" },
  "payload": {
    "task_group": "audio",
    "task": "asr",
    "function": "recognition",
    "model": "paraformer-realtime-v2",
    "parameters": {
      "format": "pcm",
      "sample_rate": 16000,
      "punctuation_prediction_enabled": true
    },
    "input": {}
  }
}
```

2. Once you receive `{"header":{"event":"task-started"}}`, start streaming **binary frames**: raw PCM at 16kHz / 16-bit / mono / little-endian. Frame size is up to you — anywhere from tens to hundreds of milliseconds, smaller means lower latency.

3. The server will push text frames back as recognition progresses:

```json
{"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"text":"hello","sentence_end":false}}}}
```

`sentence_end: false` is an interim result (still speaking), `true` means that sentence is final.

4. When done, send `finish-task`:

```json
{"header":{"action":"finish-task","task_id":"<same as above>","streaming":"duplex"},"payload":{"input":{}}}
```

If you point `REALTIME_ASR_WS_URL` at a different provider (e.g. OpenAI's Realtime API), follow that provider's own message format instead — the relay itself is protocol-agnostic.

**On ESP32, we recommend using [ESP-SR](https://github.com/espressif/esp-sr) for local wake-word detection and hardware-level acoustic echo cancellation (AEC).** Open this WebSocket and start streaming after wake-word detection fires — this is what makes "the user can interrupt while the AI is talking" work well; the AEC is handled by ESP-SR's audio front-end, no extra work needed at the server or application layer.

### Speech synthesis

`POST /api/v1/audio/tts/stream`, body:

```json
{ "input": { "text": "text to synthesize" }, "voice": "longxiaochun" }
```

The response is a raw PCM stream — check the `Content-Type` header (`audio/L16; rate=<N>; channels=1`) for the actual sample rate, since it depends on the provider (DashScope: 22050Hz, OpenAI: 24000Hz). Feed it straight to your audio output (e.g. ESP32's I2S playback) as you receive it, no decoding needed. The `voice` value is provider-specific — DashScope's CosyVoice voices vs. OpenAI's (`alloy`, `echo`, `fable`, ...).

### Chat

`POST /api/v1/chat/completions`, OpenAI-compatible format (a `messages` array), supports `"stream": true` for token-by-token streaming output. Pipe the streamed tokens straight into TTS and you have a full "listen → think → speak" loop.

## Language support

Language coverage depends entirely on which provider each capability uses — the gateway itself doesn't restrict anything.

| Capability | How language is controlled | DashScope coverage | OpenAI coverage |
|---|---|---|---|
| `chat/completions` | No restriction for either provider — the model replies in whatever language your prompt uses | Chinese, English, Japanese, Korean, French, German, Spanish and other mainstream languages all work | Same — broad multilingual coverage |
| `asr` (file transcription) | `language_hints` field in the request body (DashScope) or auto-detect / a single language code (OpenAI Whisper) | Chinese (incl. dialects like Cantonese), English, Japanese, Korean — see the [paraformer-v2 docs](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) | Whisper covers 50+ languages including Spanish, French, German, and most of the languages DashScope doesn't — if you need broad European-language ASR, `ASR_PROVIDER=openai` is the easier path |
| `asr/stream` (real-time) | Whatever the upstream `REALTIME_ASR_WS_URL` provider supports | paraformer-realtime-v2's language set (same as above) | N/A by default — point `REALTIME_ASR_WS_URL` at an OpenAI-compatible realtime endpoint if you want this |
| `audio/tts` / `/tts/stream` | `voice` parameter, provider-specific voice list | Chinese (incl. regional accents) and English are the primary coverage — check the [CosyVoice voice list](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) | OpenAI's voices are English-first but produce reasonable output in many other languages too |

**tl;dr**: if your hardware targets users speaking European languages, `ASR_PROVIDER=openai` and `TTS_PROVIDER=openai` will get you there faster than trying to force it through DashScope. Chat works well on either.

All server-side strings (error messages, log lines, etc.) are in English by default, with no i18n layer — the gateway just returns the raw text in the `detail` field. If your client targets non-English-speaking users, translate on the client side. PRs adding i18n for server-side messages are welcome.

## Project structure

```
app/
├── config.py           # Environment variable configuration, provider selection
├── database.py         # User model + database connection
├── security.py         # Password hashing, JWT
├── ratelimit.py         # Rate limiting
├── main.py              # FastAPI entrypoint
├── providers/
│   ├── chat.py           # Chat endpoint resolver (OpenAI-compatible)
│   ├── tts.py             # TTS provider adapters (DashScope, OpenAI)
│   └── asr.py             # File-based ASR provider adapters (DashScope, OpenAI)
└── routers/
    ├── auth.py            # Register/login/account management
    ├── proxy.py            # Core: chat/ASR/TTS proxy, dispatches to providers
    └── health.py
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free to use, modify, and distribute for any noncommercial purpose (personal projects, research, hobby hardware builds, etc.). Commercial use requires a separate license from the copyright holder.
