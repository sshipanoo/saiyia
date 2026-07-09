# Voice Server

一个开源的语音 AI 网关服务端，从 [赛伊](https://saiyia.com)（一款语音优先的 AI 助理 App）里拆分出来。

它只做一件事：给任何能开 WebSocket / 发 HTTP 请求的客户端（手机、网页、ESP32 等硬件）提供统一的账号体系 + AI 对话/语音识别/语音合成代理，不用自己直接持有第三方 AI 服务的密钥。适合拿来接硬件项目——比如一个带麦克风和喇叭的语音陪伴机器人。

## 这个仓库不包含什么

原项目里跟 iOS App / 网页版 / App Store 订阅强绑定的部分**没有**放进来：Apple 内购凭证校验、App Store Server Notifications、Stripe 订阅、多端数据云同步（对话记录/备忘录/生词本）、后台管理接口。这些跟"给硬件提供语音网关"这个目标无关，硬拆进来只会让项目显得臃肿。

要不要收费、怎么收费，由你自己决定——`User` 模型里没有任何订阅字段，所有账号默认平等，只做限流。

## 能力清单

| 接口 | 说明 |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | 账号体系，JWT 鉴权，`token_version` 机制支持登出即时吊销旧 token |
| `POST /api/v1/chat/completions` | 代理到阿里云百炼大模型对话（OpenAI 兼容格式，支持流式） |
| `POST /api/v1/audio/tts` | 一次性语音合成，返回完整 MP3 |
| `POST /api/v1/audio/tts/stream` | 流式语音合成，边合成边吐裸 PCM（16-bit/mono/22050Hz），首字延迟低，适合边收边播 |
| `POST /api/v1/asr` | 整段录音文件识别（原生支持多说话人分离） |
| `WS /api/v1/asr/stream` | 实时流式语音识别中继，边说边出文字 |

## 快速开始

```bash
cp .env.example .env   # 填 SECRET_KEY、ALIBABA_API_KEY、DB_PASSWORD
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## 硬件接入（比如 ESP32）指南

### 鉴权

先调 `/api/v1/auth/register` 或 `/login` 拿到 JWT，之后所有请求带 `Authorization: Bearer <token>`。

WebSocket 握手如果客户端不方便自定义 header（浏览器原生 WebSocket 就是这样），也可以把 token 放 query string：`wss://.../asr/stream?token=xxx`，服务端两种方式都认。

### 实时语音识别协议

`WS /api/v1/asr/stream` 是对阿里云 DashScope 实时语音识别（paraformer-realtime-v2）协议的**透明中继**，服务端只做鉴权和转发，不改消息内容。连上后：

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

**ESP32 上推荐用 [ESP-SR](https://github.com/espressif/esp-sr) 做本地唤醒词检测 + 硬件级回声消除（AEC）**，检测到唤醒词后再开这条 WebSocket 连接、开始推流——这样可以做到"AI 说话时开口就能打断"，AEC 靠 ESP-SR 的音频前端处理，不用在服务端或应用层额外做。

### 语音合成

`POST /api/v1/audio/tts/stream`，body：

```json
{ "input": { "text": "要合成的文字" }, "voice": "longxiaochun" }
```

响应是 `Content-Type: audio/L16; rate=22050; channels=1` 的裸 PCM 流，边收边喂给音频输出（比如 ESP32 的 I2S 播放）即可，不需要解码。

### 对话

`POST /api/v1/chat/completions`，OpenAI 兼容格式（`messages` 数组），支持 `"stream": true` 做流式输出，逐 token 拼起来送给 TTS 就是完整的"识别→对话→合成"闭环。

## 项目结构

```
app/
├── config.py       # 环境变量配置
├── database.py     # User 模型 + 数据库连接
├── security.py     # 密码哈希、JWT
├── ratelimit.py     # 接口限流
├── main.py          # FastAPI 入口
└── routers/
    ├── auth.py       # 注册/登录/账号管理
    ├── proxy.py       # 核心：对话/语音识别/语音合成代理
    └── health.py
```

## License

MIT
