# saiyia

**[中文](README.md) | [English](README.en.md) | [Español](README.es.md) | [日本語](README.ja.md)**

オープンソースの音声 AI ゲートウェイサーバーです。

やることは一つだけ：WebSocket や HTTP リクエストを送れるあらゆるクライアント（スマホ、Web、ESP32 のようなハードウェア）に、統一されたアカウントシステムと、AI 対話／音声認識／音声合成のプロキシを提供します。クライアント側がサードパーティ AI サービスの API キーを直接持つ必要はありません。マイクとスピーカーを備えたコンパニオンロボットのようなハードウェアプロジェクトに向いています。

## 設計方針

意図的に最小限に保っています。「アカウント + AI 機能プロキシ」だけを行い、決済・サブスクリプション機能は含みません（`User` モデルにサブスクリプション関連のフィールドは一切なく、すべてのアカウントは対等で、レート制限のみ行います）。マルチデバイスのデータ同期や管理画面もありません。課金するかどうか、どう課金するか、会話履歴を保存するかどうかは、利用者が自分で決めて実装してください。

## API 一覧

| エンドポイント | 説明 |
|---|---|
| `POST /api/v1/auth/register` `/login` `/me` `/logout` `/change-password` `/delete-account` | アカウントシステム、JWT 認証。`token_version` の仕組みでログアウト時に古いトークンを即座に失効させられます |
| `POST /api/v1/chat/completions` | アリババクラウド Model Studio（DashScope）の対話 API へのプロキシ（OpenAI 互換フォーマット、ストリーミング対応） |
| `POST /api/v1/audio/tts` | 一括音声合成、完全な MP3 を返します |
| `POST /api/v1/audio/tts/stream` | ストリーミング音声合成。生成しながら生の PCM（16-bit/mono/22050Hz）を返すので初回応答が速く、受信しながらの再生に向いています |
| `POST /api/v1/asr` | 録音ファイル全体の文字起こし（話者分離をネイティブサポート） |
| `WS /api/v1/asr/stream` | リアルタイムストリーミング音声認識の中継。話しながらテキストが返ってきます |

## クイックスタート

```bash
cp .env.example .env   # SECRET_KEY、ALIBABA_API_KEY、DB_PASSWORD を入力
docker compose up -d --build
curl http://localhost:8000/api/v1/health
```

## ハードウェア連携ガイド（ESP32 など）

### 認証

まず `/api/v1/auth/register` または `/login` を呼んで JWT を取得し、以降すべてのリクエストで `Authorization: Bearer <token>` を付けます。

WebSocket のハンドシェイクでカスタムヘッダーを設定できないクライアント（ブラウザ標準の WebSocket がそうです）の場合は、トークンをクエリ文字列で渡すこともできます：`wss://.../asr/stream?token=xxx`。サーバー側はどちらの方式も受け付けます。

### リアルタイム音声認識プロトコル

`WS /api/v1/asr/stream` はアリババクラウド DashScope のリアルタイム音声認識プロトコル（paraformer-realtime-v2）への**透過的な中継**です。サーバーは認証と転送のみを行い、メッセージの内容には一切手を加えません。接続後の流れ：

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

**ESP32 では、ローカルのウェイクワード検出とハードウェアレベルのエコーキャンセレーション（AEC）に [ESP-SR](https://github.com/espressif/esp-sr) を使うことを推奨します。** ウェイクワードが検出されてからこの WebSocket を開いて送信を始めることで、「AI が話している最中でもユーザーが割り込める」という体験がうまく実現できます。AEC は ESP-SR のオーディオフロントエンドが処理するので、サーバー側やアプリケーション層で追加の作業は不要です。

### 音声合成

`POST /api/v1/audio/tts/stream`、リクエストボディ：

```json
{ "input": { "text": "合成したいテキスト" }, "voice": "longxiaochun" }
```

レスポンスは `Content-Type: audio/L16; rate=22050; channels=1` の生 PCM ストリームです。デコード不要で、受信しながらそのままオーディオ出力（ESP32 の I2S 再生など）に流し込めます。

### 対話

`POST /api/v1/chat/completions`、OpenAI 互換フォーマット（`messages` 配列）、`"stream": true` でトークン単位のストリーミング出力に対応しています。ストリーミングされたトークンをそのまま TTS に流し込めば、「聞く→考える→話す」の一連のループが完成します。

## 多言語サポートについて

このゲートウェイ自体は言語を限定しません。上限はプロキシ先の DashScope モデルの能力によって決まります。**あらかじめ誤解しやすい点を説明しておきます**：DashScope の音声系モデル（認識・合成）は主に中国語とアジア圏の言語向けに作られており、「主要言語なら何でも対応」というわけではありません。スペイン語・フランス語・ドイツ語などのヨーロッパ言語は、現時点では音声（認識・合成）側の paraformer / CosyVoice の主なカバー範囲に入っていません。利用前に公式 Playground で実際に試すことをおすすめします。テキスト対話（chat）はこの制限を受けず、どの言語でも会話できます。

| 機能 | 言語の制御方法 | 対応が確認されている主要言語 |
|---|---|---|
| `chat/completions` 対話 | 言語制限なし。プロンプトで使った言語をモデルが理解し、その言語で返答します。追加設定は不要 | 中国語・英語・日本語・韓国語・フランス語・ドイツ語・スペイン語など主要言語で問題なく対話できます（これは LLM 全般の言語能力であり、下記の音声専用モデルとは別物です） |
| `asr`（録音ファイル全体の認識） | リクエストボディの `language_hints` パラメータで制御、デフォルトは `["zh", "en"]`。他の言語コードを認識のヒントとして渡せます | 中国語（広東語などの方言含む）、英語、日本語、韓国語。最新の対応言語一覧は [paraformer-v2 公式ドキュメント](https://help.aliyun.com/zh/model-studio/paraformer-speech-recognition) を参照してください |
| `asr/stream`（リアルタイム認識） | 透過的な中継。言語・モデルは完全にクライアントが `run-task` メッセージの `parameters` で指定するもので、ゲートウェイ側は一切制限・書き換えを行いません | 上記と同様（paraformer-realtime-v2） |
| `audio/tts` `/tts/stream` 音声合成 | リクエストボディの `voice` パラメータで決まります。音色ごとに対応言語・アクセントが異なります | 中国語（地域アクセントの音色含む）と英語が主なカバー範囲。日本語・韓国語の音色は状況により異なるため、[CosyVoice 音色一覧](https://help.aliyun.com/zh/model-studio/cosyvoice-speech-synthesis) で現在利用可能なものを確認してください |

ヨーロッパ言語のユーザーを対象にしたハードウェアを作る場合は、音声認識・音声合成の部分を別のプロバイダーに差し替えることをおすすめします（プロキシ層は差し替え可能な設計です。`proxy.py` 内の該当関数が呼び出す API を変えるだけで、アカウントシステムや全体のアーキテクチャを変更する必要はありません）。対話機能はこの制限を受けず、そのまま使えます。

画面上の文言（エラーメッセージなど）は現在すべて中国語でハードコードされており、まだ i18n 対応はしていません。非中国語話者向けのクライアントを作る場合は、当面はクライアント側で翻訳することをおすすめします。ゲートウェイは `detail` フィールドに元のテキストをそのまま返すだけなので、機能自体には影響しません。サーバー側エラーメッセージの i18n を追加する PR は歓迎します。

## プロジェクト構成

```
app/
├── config.py       # 環境変数の設定
├── database.py     # User モデル + データベース接続
├── security.py     # パスワードハッシュ、JWT
├── ratelimit.py     # レート制限
├── main.py          # FastAPI エントリーポイント
└── routers/
    ├── auth.py       # 登録／ログイン／アカウント管理
    ├── proxy.py       # コア：対話／ASR／TTS プロキシ
    └── health.py
```

## ライセンス

MIT
