# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

An open-source voice AI gateway server.

It does one thing: give any client that can open a WebSocket or make an HTTP request (phone, web, ESP32-class hardware) a unified account system plus a proxy for AI chat / speech recognition / speech synthesis, without that client ever holding a third-party AI provider's key. A good fit for hardware projects — e.g. a companion robot with a microphone and speaker.

## Design choices

Kept deliberately minimal — it only does "accounts + AI capability proxy": no payment/subscription system (the `User` model has no subscription fields at all, every account is equal, only rate-limited), no multi-device data sync, no admin panel. Whether to charge, how to charge, whether to persist conversation history — all of that is left for you to decide and implement on top.

## API surface

| Endpoint | Description |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | Account system, JWT auth, `token_version` mechanism supports instant revocation of old tokens on logout |
| `POST /api/v1/chat/completions` | Proxies to Alibaba Cloud Model Studio (DashScope) chat completion (OpenAI-compatible format, streaming supported) |
| `POST /api/v1/audio/tts` | One-shot speech synthesis, returns a complete MP3 |
| `POST /api/v1/audio/tts/stream` | Streaming speech synthesis, emits raw PCM (16-bit/mono/22050Hz) as it's generated, low first-byte latency, good for play-as-you-receive |
| `POST /api/v1/asr` | Full-recording transcription (native multi-speaker diarization support) |
| `WS /api/v1/asr/stream` | Real-time streaming speech recognition relay, text comes back as you speak |

## Quick start

```bash
cp .env.example .env   # fill in SECRET_KEY, ALIBABA_API_KEY, DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## Hardware integration guide (e.g. ESP32)

### Auth

Call `/api/v1/auth/register` or `/login` first to get a JWT, then send it as `Authorization: Bearer <token>` on every request.

If your WebSocket client can't set custom headers on the handshake (this is true of native browser WebSocket), you can pass the token as a query string instead: `wss://.../asr/stream?token=xxx` — the server accepts either.

### Real-time speech recognition protocol

`WS /api/v1/asr/stream` is a **transparent relay** to Alibaba Cloud DashScope's real-time speech recognition protocol (paraformer-realtime-v2) — the server only handles auth and forwarding, it doesn't touch message content. After connecting:

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

**On ESP32, we recommend using [ESP-SR](https://github.com/espressif/esp-sr) for local wake-word detection and hardware-level acoustic echo cancellation (AEC).** Open this WebSocket and start streaming after wake-word detection fires — this is what makes "the user can interrupt while the AI is talking" work well; the AEC is handled by ESP-SR's audio front-end, no extra work needed at the server or application layer.

### Speech synthesis

`POST /api/v1/audio/tts/stream`, body:

```json
{ "input": { "text": "text to synthesize" }, "voice": "longxiaochun" }
```

The response is a raw PCM stream with `Content-Type: audio/L16; rate=22050; channels=1` — feed it straight to your audio output (e.g. ESP32's I2S playback) as you receive it, no decoding needed.

### Chat

`POST /api/v1/chat/completions`, OpenAI-compatible format (a `messages` array), supports `"stream": true` for token-by-token streaming output. Pipe the streamed tokens straight into TTS and you have a full "listen → think → speak" loop.

## Language support

The gateway itself doesn't lock you into any language — the ceiling is set by the DashScope models it proxies to. **One thing worth clarifying up front**: DashScope's speech models (recognition/synthesis) are primarily built for Chinese and Asian languages — this is not "every mainstream language is supported." European languages like Spanish, French, or German are currently outside the main coverage of paraformer / CosyVoice on the speech side; test against the official Playground before relying on it. Text chat is not affected by this — any language works there.

| Capability | How language is controlled | Known supported mainstream languages |
|---|---|---|
| `chat/completions` | No language restriction — the model understands and replies in whatever language your prompt uses, no extra configuration needed | Chinese, English, Japanese, Korean, French, German, Spanish and other mainstream languages all work for chat (this is the LLM's general language ability, a separate thing from the speech-specific models below) |
| `asr` (full-recording transcription) | Controlled by the `language_hints` field in the request body, defaults to `["zh", "en"]`; pass other language codes as recognition hints | Chinese (including dialects like Cantonese), English, Japanese, Korean — check the [paraformer-v2 docs](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) for the current, up-to-date language list |
| `asr/stream` (real-time recognition) | Transparent relay — language/model is entirely up to what the client specifies in the `run-task` message's `parameters`; the gateway does not restrict or rewrite anything | Same as above (paraformer-realtime-v2) |
| `audio/tts` / `/tts/stream` | Determined by the `voice` parameter in the request body — different voices correspond to different languages/accents | Chinese (including regional-accent voices) and English are the primary coverage; Japanese/Korean voices vary — check the [CosyVoice voice list](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) for what's currently available |

If your hardware targets users speaking European languages, swap out the speech recognition/synthesis legs for a different provider (the proxy layer is swappable — just point the relevant functions in `proxy.py` at a different API; the account system and overall architecture don't need to change). Chat isn't affected and works as-is.

All server-side strings (error messages, log lines, etc.) are in English by default, with no i18n layer — the gateway just returns the raw text in the `detail` field. If your client targets non-English-speaking users, translate on the client side. PRs adding i18n for server-side messages are welcome.

## Project structure

```
app/
├── config.py       # Environment variable configuration
├── database.py     # User model + database connection
├── security.py     # Password hashing, JWT
├── ratelimit.py     # Rate limiting
├── main.py          # FastAPI entrypoint
└── routers/
    ├── auth.py       # Register/login/account management
    ├── proxy.py       # Core: chat/ASR/TTS proxy
    └── health.py
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free to use, modify, and distribute for any noncommercial purpose (personal projects, research, hobby hardware builds, etc.). Commercial use requires a separate license from the copyright holder.
