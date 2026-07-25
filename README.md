## セットアップ

```sh
uv sync

uv run main.py
```

## 実行環境

本番BotとPC上のデバッグBotは同じDiscordサーバー、通常DB、Chatbot DB、OpenAI Projectを共有します。
共有資源への二重作用を防ぐため、`BOT_ENVIRONMENT` は必須です。

```env
# GCP
BOT_ENVIRONMENT=production

# PC
BOT_ENVIRONMENT=debug
```

未設定、空文字、`production` / `debug` 以外の値ではBotは起動しません。既存の `DEBUG` はCogのロード構成を
選ぶ別機能として残っています。PCで全Cogをロードする場合は `DEBUG=false` と
`BOT_ENVIRONMENT=debug` を組み合わせます。

起動時には各Cogの必須環境変数もまとめて検証します。不足や形式不正があれば、DiscordやDBへ接続する前に
不足項目を列挙して終了します。

### Chatbotテストチャンネル

`CHANNEL_ID_DEBUG_CHATBOT` にテスト用チャンネルのDiscord IDを設定します。同じ値をGCPとPCへ設定します。

```env
CHANNEL_ID_DEBUG_CHATBOT=123456789012345678
```

- `production`: 指定IDではChatbotの保存、解析、判定、応答を行いません。
- `debug`: 指定IDだけでChatbotの保存、解析、判定、応答を行います。空または未設定では起動しません。

デバッグBotではテストチャンネル固有の `assistant` / `chat` とシャドーモードを変更できます。グローバルな
会話設定、カスタムプロファイル、長期記憶、別名情報は本番と共有するため、読み取りだけが可能です。

### API usageレポート

API利用量は同じOpenAI Projectの実費と一致させるため、本番とデバッグを共有DBへ合算します。Discordへの配送は
次のように分離します。

- 本番Botは定期投稿と手動投稿を行います。
- デバッグBotは定期投稿を行わず、`/api_usage report` による手動投稿だけを行います。
- 本番の投稿先は `THREAD_ID_API_USAGE_REPORT`、デバッグBot専用の投稿先は
  `THREAD_ID_DEBUG_API_USAGE_REPORT` へ設定します。
- どちらの設定も、テキストチャンネルまたはスレッドのIDを処理できます。
- デバッグ環境では `/api_usage schedule` を使用できません。`status` は利用できます。
- 保存済みメッセージが別Botの投稿だった場合は、配送記録を上書きせず設定エラーとして停止します。

OpenAI API callを追加・変更するときの計測、単価、Usage API照合、テスト手順は
[`docs/openai-api-usage-maintenance.md`](docs/openai-api-usage-maintenance.md)を参照してください。

### TalkData

TalkDataは本番とデバッグでスキーマを分離します。メッセージの自動保存とTalkDataの操作は、実行環境に
対応するスキーマ内で完結します。

- `production`: `talkdata` スキーマを使用します。
- `debug`: `talkdata_test` スキーマを使用します。

起動時には、選択されたスキーマ内のTalkData用テーブルと初期レコードを準備します。スキーマ自体は作成しないため、
デバッグ環境を初めて起動する前に `talkdata_test` スキーマを用意してください。

### デバッグ環境で自動実行しない機能

全Cogをロードしても、次のバックグラウンド処理や無条件のイベント処理はデバッグ環境では開始しません。

- API usageの定期投稿、再集計、確定額再取得
- リマインダーの定期配送（PCから登録したリマインダーは本番Botが配送）
- 長期記憶キューの復旧、抽出、既存記憶の整合処理
- Last.fm更新停止警告
- Discord予定イベントの作成、更新、終了通知
- 監査ログの削除・編集転送
- Monitorの起動直後から有効な既定ゲートキーパー
- 通常DBおよびChatbot DBのテーブル作成・スキーマ変更

`stay_focused`、明示的に設定したリアクション制限、明示的なVC接続後のVoiceVoxなど、デバッグBot内で
完結する機能は利用できます。共有DBのスキーマ変更を伴う機能は、デバッグBotではなく別DBまたは別スキーマで
検証してください。TalkDataはこの方針に従い、デバッグ環境では専用の `talkdata_test` スキーマを使用します。
