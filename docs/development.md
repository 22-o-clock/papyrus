# 開発・保守ガイドライン

## 実行環境

Papyrusには、通常運用向けの`production`と、テスト環境として使用する`debug`がある。

### debug環境の方針

debug環境は、通常運用と処理対象や副作用が競合しないようにする。必要に応じてチャンネル、通知先、保存先を
分離し、分離できない処理は制限または停止する。競合しない処理は、定期処理やイベント処理を含めて実行できる。

新しい機能にも同じ方針を適用する。

### 環境の選択

実行環境は`BOT_ENVIRONMENT`で選択する。この設定は必須で、未設定、空文字、`production` / `debug` 以外の
値ではBotを起動しない。

`DEBUG`は実行環境とは別に、ロードするCogを選択する設定として扱う。全Cogをロードしてデバッグする場合は、
`BOT_ENVIRONMENT=debug`と`DEBUG=false`を組み合わせる。`DEBUG=true`では`core/debug_cogs.py`に
明示したCogだけをロードし、対象がなければCogをロードしない。

起動時には各Cogの必須環境変数をまとめて検証し、不足や形式不正がある場合はDiscordやDBへ接続する前に終了する。

### 現在の具体的な挙動

#### Chatbotのテストチャンネル

`CHANNEL_ID_DEBUG_CHATBOT` にChatbot用テストチャンネルのDiscord IDを設定する。

- `production`: 指定チャンネルではChatbotの保存、解析、判定、応答を行わない。
- `debug`: 指定チャンネルだけでChatbotの保存、解析、判定、応答を行う。

デバッグ環境では、テストチャンネル固有の役割とシャドーモードを変更できる。グローバルな会話設定、
カスタムプロファイル、長期記憶、別名情報は読み取り専用とする。

#### API usageレポート

- production環境では定期投稿と手動投稿を行う。
- debug環境では定期投稿を行わず、`/api_usage report` による手動投稿だけを行う。
- production環境の投稿先は `THREAD_ID_API_USAGE_REPORT`、debug環境の投稿先は
  `THREAD_ID_DEBUG_API_USAGE_REPORT` で指定する。
- debug環境では `/api_usage schedule` を使用できない。`/api_usage status` は利用できる。
- 投稿先にはテキストチャンネルまたはスレッドを指定できる。
- 保存済みメッセージが別のBotによる投稿だった場合は、配送記録を上書きせず設定エラーとして停止する。

#### TalkData

TalkDataは実行環境ごとにスキーマを分離する。

- `production`: `talkdata` スキーマを使用する。
- `debug`: `talkdata_test` スキーマを使用する。

起動時には選択したスキーマ内のテーブルと初期レコードを準備するが、スキーマ自体は作成しない。

#### debug環境で自動実行しない機能

次の機能は、現在の実装では通常運用との二重処理や共有データへの副作用を避けるため、debug環境で停止する。

- API usageの定期投稿、再集計、確定額再取得
- リマインダーの定期配送
- 長期記憶キューの復旧、抽出、既存記憶の整合処理
- Last.fm更新停止警告
- Discord予定イベントの作成、更新、開始、終了、中止通知
- 監査ログの削除・編集転送
- Monitorの起動直後から有効な既定ゲートキーパー
- 通常DBおよびChatbot DBのテーブル作成・スキーマ変更

`stay_focused`、明示的に設定したリアクション制限、明示的なVC接続後のVOICEVOXなど、debug環境内で
完結する機能は利用できる。

## テスト

- 挙動を変更する場合は、可能な限り利用者から見える挙動を保証する回帰テストを追加する。
- Discord上の挙動に影響する変更では、Botを起動してユーザー操作とログを確認する。
- VoiceVox機能を確認する場合は、起動前にVOICEVOX Engineが `VOICEVOX_URL` で応答することを確認する。
- 文書やDiscord上の挙動に影響しない変更では、Discordの手動確認は不要とする。

### まとまったテスト終了時のコマンド整理

個別のテストごとに行う必要はないが、まとまったタスクのテストが終了した段階で、テスト用Botに登録された
スラッシュコマンドをサーバーから削除することを推奨する。

`BOT_ENVIRONMENT=debug`、`DEBUG=true`でテスト用Botを起動する。現在の`core/debug_cogs.py`はCogを
ロードしないため、空のコマンドツリーがサーバーへ同期され、テスト用Botに紐づくスラッシュコマンドが削除される。
コマンド同期の完了をログで確認したら、Botを停止する。

この操作では、`DISCORD_BOT_TOKEN`が必ずテスト用Botを参照していることを確認する。本番Botのトークンでは
実行しない。

## 完了時の確認

変更した範囲に対して、次の確認を実行する。

```powershell
uv run python -m unittest discover tests
uv run ruff check <変更対象>
uv run ruff format --check <変更対象>
uv run ty check <変更対象>
```

- 対象を限定できない変更では、`<変更対象>` を `.` に置き換えて全体を確認する。
- PRではGitHub Actionsのテストとlintの結果を確認する。ローカルでCI環境全体を再現する必要はない。
- pre-commitは現時点では必須としない。

## ドキュメントの更新

- 機能や利用方法を変更した場合は、実装と同じ変更単位で関連するドキュメントも更新する。
- スラッシュコマンド、コンテキストメニュー、自動・バックグラウンド処理などを変更した場合は
  [`features.md`](features.md) を更新する。
- OpenAI API callの計測、単価、Usage APIとの照合、レポート表示を変更した場合は
  [`maintenance/openai_api_usage.md`](maintenance/openai_api_usage.md) を更新する。
- ドキュメントを追加、削除、移動した場合は [`index.md`](index.md) の分類とリンクを更新する。
