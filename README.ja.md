# saiyia

**[English](README.md) | [Español](README.es.md) | [中文](README.zh.md) | [日本語](README.ja.md)**

オープンソースの音声 AI ゲートウェイサーバーです。

やることは一つだけ：WebSocket や HTTP リクエストを送れるあらゆるクライアント（スマホ、Web、ESP32 のようなハードウェア）に、統一されたアカウントシステムと、AI 対話／音声認識／音声合成のプロキシを提供します。クライアント側がサードパーティ AI サービスの API キーを直接持つ必要はありません。マイクとスピーカーを備えたコンパニオンロボットのようなハードウェアプロジェクトに向いています。

## 設計方針

意図的に最小限に保っています。「アカウント + AI 機能プロキシ」だけを行い、決済・サブスクリプション機能は含みません（`User` モデルにサブスクリプション関連のフィールドは一切なく、すべてのアカウントは対等で、レート制限のみ行います）。マルチデバイスのデータ同期や管理画面もありません。課金するかどうか、どう課金するか、会話履歴を保存するかどうかは、利用者が自分で決めて実装してください。

## プロバイダー対応

対話・音声認識・音声合成はそれぞれ**独立して**プロバイダーを選べます。特定のベンダーに縛られません。`.env` で `CHAT_PROVIDER` / `ASR_PROVIDER` / `TTS_PROVIDER` を設定します：

| プロバイダー | 対話 | 録音ファイル認識 | ストリーミング音声合成 | 備考 |
|---|---|---|---|---|
| `dashscope`（デフォルト） | ✅ | ✅（話者分離をネイティブサポート） | ✅ | アリババクラウド Model Studio |
| `openai` | ✅ | ✅（Whisper、話者分離なし） | ✅ | 対話については OpenAI 互換の任意のエンドポイント（Groq、Together、DeepSeek、自前の vLLM サーバーなど）でも動作。`OPENAI_BASE_URL` を変更するだけ |

**プロバイダーの追加はプロジェクト全体を書き換える作業ではありません**——`app/providers/` を見てください。`chat.py` は `(base_url, api_key)` の組を解決するだけです（ほとんどの LLM プロバイダーは OpenAI 互換なので、通常は新しいコードすら不要です）。`tts.py` と `asr.py` はそれぞれ小さな `Protocol` インターフェースを定義しており、プロバイダーごとに一度実装すれば済みます。リアルタイムストリーミング認識（`WS /asr/stream`）はアダプター不要です——純粋なバイト／テキストの透過的な中継なので、WebSocket ベースの**あらゆる**リアルタイム音声 API に対応できます。`REALTIME_ASR_WS_URL` / `REALTIME_ASR_AUTH_HEADER` をそこに向けるだけです。

## API 一覧

| エンドポイント | 説明 |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | アカウントシステム、JWT 認証。`token_version` の仕組みでログアウト時に古いトークンを即座に失効させられます |
| `POST /api/v1/chat/completions` | `CHAT_PROVIDER` で選択したプロバイダーへのプロキシ（OpenAI 互換フォーマット、ストリーミング対応） |
| `POST /api/v1/audio/tts` | 一括音声合成、完全な MP3 を返します |
| `POST /api/v1/audio/tts/stream` | ストリーミング音声合成。生成しながら生の PCM を返します（サンプルレートはプロバイダーによって異なり、レスポンスの `Content-Type` に記載）。初回応答が速く、受信しながらの再生に向いています |
| `POST /api/v1/asr` | 録音ファイル全体の文字起こし |
| `WS /api/v1/asr/stream` | リアルタイムストリーミング音声認識の中継。話しながらテキストが返ってきます |

## クイックスタート

```bash
cp .env.example .env   # SECRET_KEY、ALIBABA_API_KEY（または OPENAI_API_KEY）、DB_PASSWORD を入力
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## ハードウェア連携ガイド（ESP32 など）

### 認証

まず `/api/v1/auth/register` または `/login` を呼んで JWT を取得し、以降すべてのリクエストで `Authorization: Bearer <token>` を付けます。

WebSocket のハンドシェイクでカスタムヘッダーを設定できないクライアント（ブラウザ標準の WebSocket がそうです）の場合は、トークンをクエリ文字列で渡すこともできます：`wss://.../asr/stream?token=xxx`。サーバー側はどちらの方式も受け付けます。

### リアルタイム音声認識プロトコル

`WS /api/v1/asr/stream` は `REALTIME_ASR_WS_URL` が指す WebSocket エンドポイント（デフォルトはアリババクラウド DashScope の paraformer-realtime-v2）への**透過的な中継**です。サーバーは認証と転送のみを行い、メッセージの内容には一切手を加えません。デフォルトの DashScope プロバイダーの場合、接続後の流れ：

1. JSON テキストフレームを送信して認識タスクを開始します：

```json
{
  "header": { "action": "run-task", "task_id": "<32文字のランダムな16進数ID>", "streaming": "duplex" },
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

2. `{"header":{"event":"task-started"}}` を受信したら、**バイナリフレーム**の送信を開始します：16kHz／16-bit／モノラル／リトルエンディアンの生 PCM。フレームサイズは自由（数十ミリ秒〜数百ミリ秒）で、小さいほど遅延が小さくなります。

3. サーバーは認識が進むにつれてテキストフレームを返します：

```json
{"header":{"event":"result-generated"},"payload":{"output":{"sentence":{"text":"こんにちは","sentence_end":false}}}}
```

`sentence_end: false` は暫定結果（まだ話している最中）、`true` はその文が確定したことを意味します。

4. 話し終えたら `finish-task` を送信します：

```json
{"header":{"action":"finish-task","task_id":"<上と同じID>","streaming":"duplex"},"payload":{"input":{}}}
```

`REALTIME_ASR_WS_URL` を別のプロバイダー（例：OpenAI の Realtime API）に向けた場合は、そのプロバイダー独自のメッセージ形式に従ってください——中継自体はプロトコルに関知しません。

**ESP32 では、ローカルのウェイクワード検出とハードウェアレベルのエコーキャンセレーション（AEC）に [ESP-SR](https://github.com/espressif/esp-sr) を使うことを推奨します。** ウェイクワードが検出されてからこの WebSocket を開いて送信を始めることで、「AI が話している最中でもユーザーが割り込める」という体験がうまく実現できます。AEC は ESP-SR のオーディオフロントエンドが処理するので、サーバー側やアプリケーション層で追加の作業は不要です。

### 音声合成

`POST /api/v1/audio/tts/stream`、リクエストボディ：

```json
{ "input": { "text": "合成したいテキスト" }, "voice": "longxiaochun" }
```

レスポンスは生 PCM ストリームです——実際のサンプルレートはプロバイダーによって異なるため、`Content-Type` ヘッダー（`audio/L16; rate=<N>; channels=1`）を確認してください（DashScope は 22050Hz、OpenAI は 24000Hz）。デコード不要で、受信しながらそのままオーディオ出力（ESP32 の I2S 再生など）に流し込めます。`voice` の値もプロバイダー固有です——DashScope の CosyVoice の音色 vs OpenAI の音色（`alloy`、`echo`、`fable` など）。

### 対話

`POST /api/v1/chat/completions`、OpenAI 互換フォーマット（`messages` 配列）、`"stream": true` でトークン単位のストリーミング出力に対応しています。ストリーミングされたトークンをそのまま TTS に流し込めば、「聞く→考える→話す」の一連のループが完成します。

## 多言語サポートについて

言語のカバー範囲は各機能が使うプロバイダーに完全に依存します——ゲートウェイ自体は何も制限しません。

| 機能 | 言語の制御方法 | DashScope のカバー範囲 | OpenAI のカバー範囲 |
|---|---|---|---|
| `chat/completions` | どちらのプロバイダーも制限なし。プロンプトで使った言語でモデルが返答します | 中国語・英語・日本語・韓国語・フランス語・ドイツ語・スペイン語など主要言語で問題なく対話できます | 同様に幅広い多言語対応 |
| `asr`（録音ファイルの認識） | DashScope はリクエストボディの `language_hints` パラメータ、OpenAI Whisper は自動検出または単一の言語コード指定 | 中国語（広東語などの方言含む）、英語、日本語、韓国語 —— [paraformer-v2 公式ドキュメント](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) 参照 | Whisper は 50 以上の言語をカバーし、スペイン語・フランス語・ドイツ語など DashScope がほぼカバーしていない言語も含みます。ヨーロッパ言語の音声認識を幅広くカバーしたい場合は `ASR_PROVIDER=openai` の方が簡単です |
| `asr/stream`（リアルタイム認識） | `REALTIME_ASR_WS_URL` が指す上流プロバイダーの対応状況次第 | paraformer-realtime-v2 の言語範囲（上記と同様） | デフォルトでは直接対応していません。必要な場合は `REALTIME_ASR_WS_URL` を OpenAI 互換のリアルタイムエンドポイントに向けてください |
| `audio/tts` `/tts/stream` 音声合成 | `voice` パラメータ、プロバイダー固有の音色一覧 | 中国語（地域アクセントの音色含む）と英語が主なカバー範囲 —— [CosyVoice 音色一覧](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) 参照 | OpenAI の音色は英語中心ですが、他の多くの言語でもそれなりに使える出力が得られます |

**要点**：ヨーロッパ言語のユーザーを対象にしたハードウェアを作るなら、DashScope に無理に対応させるより `ASR_PROVIDER=openai` と `TTS_PROVIDER=openai` の方が早く実現できます。対話機能はどちらのプロバイダーでも問題なく動きます。

サーバー側の文言（エラーメッセージ、ログなど）はデフォルトですべて英語で、i18n レイヤーはありません——ゲートウェイは `detail` フィールドに元のテキストをそのまま返すだけです。非英語話者向けのクライアントを作る場合は、クライアント側で翻訳することをおすすめします。サーバー側文言の i18n を追加する PR は歓迎します。

## プロジェクト構成

```
app/
├── config.py           # 環境変数の設定、プロバイダー選択
├── database.py         # User モデル + データベース接続
├── security.py         # パスワードハッシュ、JWT
├── ratelimit.py         # レート制限
├── main.py              # FastAPI エントリーポイント
├── providers/
│   ├── chat.py           # 対話エンドポイントの解決（OpenAI 互換）
│   ├── tts.py             # TTS プロバイダーアダプター（DashScope、OpenAI）
│   └── asr.py             # 録音ファイル認識プロバイダーアダプター（DashScope、OpenAI）
└── routers/
    ├── auth.py            # 登録／ログイン／アカウント管理
    ├── proxy.py            # コア：対話／ASR／TTS プロキシ、各プロバイダーへの振り分け
    └── health.py
```

## ライセンス

[PolyForm Noncommercial 1.0.0](LICENSE) —— オープンソースで自由に利用・改変・配布できますが、非営利目的（個人プロジェクト、研究、趣味のハードウェア開発など）に限ります。商用利用には著作権者からの別途ライセンスが必要です。
