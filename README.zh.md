# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

一个开源的语音 AI 网关服务端。

它只做一件事：给任何能开 WebSocket / 发 HTTP 请求的客户端（手机、网页、ESP32 等硬件）提供统一的账号体系 + AI 对话/语音识别/语音合成代理，不用自己直接持有第三方 AI 服务的密钥。适合拿来接硬件项目——比如一个带麦克风和喇叭的语音陪伴机器人。

## 设计取舍

刻意保持精简，只做"账号 + AI 能力代理"这一件事：不含支付/订阅体系（`User` 模型没有任何订阅字段，所有账号默认平等，只做限流），不含多端数据云同步，不含后台管理面板。要不要收费、怎么收费、要不要存对话记录，都留给使用者自己决定和实现。

## 服务商支持

对话、语音识别、语音合成这三项能力**各自独立**选择服务商，不会被绑死在一家。在 `.env` 里设置 `CHAT_PROVIDER` / `ASR_PROVIDER` / `TTS_PROVIDER`：

| 服务商 | 对话 | 整段录音识别 | 流式语音合成 | 备注 |
|---|---|---|---|---|
| `dashscope`（默认） | ✅ | ✅（原生支持说话人分离） | ✅ | 阿里云百炼 |
| `openai` | ✅ | ✅（Whisper，不支持说话人分离） | ✅ | 对话侧也兼容任何 OpenAI 协议的接口（Groq、Together、DeepSeek、自建 vLLM 服务等），改 `OPENAI_BASE_URL` 指向别处即可 |

**新增服务商不需要改动整个项目**——看 `app/providers/`：`chat.py` 只是解析出一对 `(base_url, api_key)`（大多数大模型服务商都兼容 OpenAI 协议，通常根本不用写新代码）；`tts.py` 和 `asr.py` 各自定义了一个小接口，每接一个新服务商实现一次就行。实时流式识别（`WS /asr/stream`）完全不需要适配层——它是纯字节/文本透明中继，能对接**任何**基于 WebSocket 的实时语音服务，改 `REALTIME_ASR_WS_URL` / `REALTIME_ASR_AUTH_HEADER` 指过去即可。

## 能力清单

| 接口 | 说明 |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | 账号体系，JWT 鉴权，`token_version` 机制支持登出即时吊销旧 token |
| `POST /api/v1/chat/completions` | 代理到 `CHAT_PROVIDER` 选定的服务商（OpenAI 兼容格式，支持流式） |
| `POST /api/v1/audio/tts` | 一次性语音合成，返回完整 MP3 |
| `POST /api/v1/audio/tts/stream` | 流式语音合成，边合成边吐裸 PCM（采样率因服务商而异，响应的 `Content-Type` 里带具体值），首字延迟低，适合边收边播 |
| `POST /api/v1/asr` | 整段录音文件识别 |
| `WS /api/v1/asr/stream` | 实时流式语音识别中继，边说边出文字 |

## 快速开始

```bash
cp .env.example .env   # 填 SECRET_KEY、ALIBABA_API_KEY（或 OPENAI_API_KEY）、DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## 硬件接入（比如 ESP32）指南

### 鉴权

先调 `/api/v1/auth/register` 或 `/login` 拿到 JWT，之后所有请求带 `Authorization: Bearer <token>`。

WebSocket 握手如果客户端不方便自定义 header（浏览器原生 WebSocket 就是这样），也可以把 token 放 query string：`wss://.../asr/stream?token=xxx`，服务端两种方式都认。

### 实时语音识别协议

`WS /api/v1/asr/stream` 是对 `REALTIME_ASR_WS_URL` 指向的 WebSocket 端点（默认是阿里云 DashScope 的 paraformer-realtime-v2）的**透明中继**，服务端只做鉴权和转发，不改消息内容。用默认的 DashScope 服务商时，连上后：

1. 发一条 JSON 文本帧，开始一次识别任务：

```json
{
  "header": { "action": "run-task", "task_id": "<32位十六进制随机ID>", "streaming": "duplex" },
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

2. 收到 `{"header":{"event":"task-started"}}` 后，开始持续发送**二进制帧**：16kHz / 16-bit / 单声道 / 小端序的裸 PCM，每帧大小不限（几十毫秒到几百毫秒都行，越小延迟越低）。

3. 服务端会陆续推回文本帧：

```json
{"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"text":"你好","sentence_end":false}}}}
```

`sentence_end: false` 是临时识别结果（还在说），`true` 是一句话说完的最终结果。

4. 说完一句/结束时发 `finish-task`：

```json
{"header":{"action":"finish-task","task_id":"<同上>","streaming":"duplex"},"payload":{"input":{}}}
```

如果把 `REALTIME_ASR_WS_URL` 指向别的服务商（比如 OpenAI 的 Realtime API），协议格式就按对方的来——中继本身不关心协议内容。

**ESP32 上推荐用 [ESP-SR](https://github.com/espressif/esp-sr) 做本地唤醒词检测 + 硬件级回声消除（AEC）**，检测到唤醒词后再开这条 WebSocket 连接、开始推流——这样可以做到"AI 说话时开口就能打断"，AEC 靠 ESP-SR 的音频前端处理，不用在服务端或应用层额外做。

### 语音合成

`POST /api/v1/audio/tts/stream`，body：

```json
{ "input": { "text": "要合成的文字" }, "voice": "longxiaochun" }
```

响应是裸 PCM 流——具体采样率看响应头 `Content-Type`（`audio/L16; rate=<N>; channels=1`），因服务商而异（DashScope 是 22050Hz，OpenAI 是 24000Hz）。边收边喂给音频输出（比如 ESP32 的 I2S 播放）即可，不需要解码。`voice` 参数的取值也是服务商专属的——DashScope 的 CosyVoice 音色 vs OpenAI 的（`alloy`、`echo`、`fable`……）。

### 对话

`POST /api/v1/chat/completions`，OpenAI 兼容格式（`messages` 数组），支持 `"stream": true` 做流式输出，逐 token 拼起来送给 TTS 就是完整的"识别→对话→合成"闭环。

## 多语言支持说明

语言覆盖面完全取决于每项能力选用的服务商——网关本身不做任何限制。

| 能力 | 语言控制方式 | DashScope 覆盖 | OpenAI 覆盖 |
|---|---|---|---|
| `chat/completions` | 两个服务商都不限语言，模型跟着 prompt 用的语言回复 | 中、英、日、韩、法、德、西等主流语言均可 | 同样支持广泛的多语言 |
| `asr`（整段录音识别） | DashScope 用请求体的 `language_hints` 参数；OpenAI Whisper 自动检测或指定单一语言代码 | 中文（含粤语等方言）、英文、日语、韩语，见 [paraformer-v2 文档](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) | Whisper 覆盖 50+ 种语言，包括西班牙语、法语、德语等 DashScope 大多不覆盖的语种——如果需要大范围欧洲语种识别，用 `ASR_PROVIDER=openai` 更省事 |
| `asr/stream`（实时识别） | 取决于 `REALTIME_ASR_WS_URL` 指向的上游服务商支持什么 | paraformer-realtime-v2 的语种范围（同上） | 默认不直接支持，需要自己把 `REALTIME_ASR_WS_URL` 指向兼容 OpenAI 协议的实时端点 |
| `audio/tts` `/tts/stream` 语音合成 | `voice` 参数，取值是服务商专属的音色列表 | 中文（含方言音色）、英文为主，见 [CosyVoice 音色列表](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) | OpenAI 的音色以英文为主，但合成其他语言效果也基本可用 |

**一句话总结**：如果硬件面向欧洲语种用户，`ASR_PROVIDER=openai` + `TTS_PROVIDER=openai` 比硬逼 DashScope 支持要省心得多。对话侧两边都没问题。

服务端所有文案（错误提示、日志等）默认是英文，没有做 i18n 层——网关只把原始文本放在 `detail` 字段里返回。如果你的客户端面向非英语用户，建议在客户端层面自己做文案翻译。欢迎提 PR 补充服务端文案的 i18n。

## 项目结构

```
app/
├── config.py           # 环境变量配置、服务商选择
├── database.py         # User 模型 + 数据库连接
├── security.py         # 密码哈希、JWT
├── ratelimit.py         # 接口限流
├── main.py              # FastAPI 入口
├── providers/
│   ├── chat.py           # 对话接口解析（OpenAI 兼容）
│   ├── tts.py             # 语音合成服务商适配（DashScope、OpenAI）
│   └── asr.py             # 整段录音识别服务商适配（DashScope、OpenAI）
└── routers/
    ├── auth.py            # 注册/登录/账号管理
    ├── proxy.py            # 核心：对话/语音识别/语音合成代理，分发到各服务商
    └── health.py
```

## 许可协议

[PolyForm Noncommercial 1.0.0](LICENSE) —— 开源、可自由使用/修改/分发，但仅限非商业用途（个人项目、研究、业余硬件开发等）。商业使用需要联系版权方另行获取授权。
