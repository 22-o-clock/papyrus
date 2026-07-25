# OpenAI API usageレポートの保守

## 適用範囲

OpenAI API callを追加・変更する場合は、API機能の実装と同じ変更単位でusageレポートも更新する。
Responses APIやEmbeddings APIに限らず、画像、音声、Moderation、Vector Storeなどを追加する場合も対象とする。

OpenAI Projectの確定コストには、そのProjectを使用するすべてのクライアントの費用が含まれる。Papyrus以外から
同じProjectを使用する場合は、ローカル計測との差額として現れることを前提にする。

## 実装チェックリスト

### API callの計測

- すべてのOpenAI API callを `cogs.chatbot.observability.observe_chatbot_api_call` 経由で実行する。
- `operation` には、レポート上で継続して識別できる安定した英語名を付ける。
- 実際にAPIへ渡すモデル名を計測にも渡す。aliasやcustom profileによるモデル切り替えも対象とする。
- 1 callで複数件を処理する場合は `item_count` を設定する。
- 新しいusageフィールドや課金対象を使用する場合は、レスポンスから値を取得して `ApiUsageIncrement` へ追加する。
- API失敗時にも失敗call数を保存し、計測保存の失敗はChatbot本体へ伝播させない。

### モデル・ツール単価

- `cogs.api_usage.pricing.MODEL_PRICES` に、利用可能なすべてのモデルとaliasを登録する。
- custom profileで選択可能なモデルを増やす場合は、`CUSTOM_PROFILE_MODELS` と `MODEL_PRICES` を同時に更新する。
- input、cached input、cache write、output、長文コンテキストの単価を個別に確認する。
- Web SearchやCode Interpreter以外の課金ツールを追加する場合は、呼出量の計測と単価計算を追加する。
- 単価には適用開始日を設定し、過去日のレポートを当時の単価で再計算できるようにする。
- OpenAI公式の[料金表](https://developers.openai.com/api/docs/pricing)とモデルページで確認し、
  `PRICING_VERIFIED_ON` を確認日に更新する。

### Organization Usage APIとの照合

- Papyrusが使用するAPI種別を `OpenAIOrganizationUsageClient.fetch_daily_summary` で取得する。
- 現在はCompletionsとEmbeddingsを対象としている。画像、音声、Moderation、Vector Store、
  Code Interpreterセッションなどを利用し始めた場合は、対応するOrganization Usage APIを追加する。
- Usage APIのページネーションを最後まで取得し、対象の `OPENAI_USAGE_PROJECT_ID` で絞り込む。
- OpenAI側とローカル側のcall数・token数を比較できるよう、取得結果を `OpenAIUsageSummary` と
  レポートの確認事項へ反映する。
- Usage APIの一時障害でCosts APIの確定額まで失わないよう、詳細照合の失敗は確定コスト取得から分離する。
- Usage APIとCosts APIには反映タイミングの差があるため、進行中のUTC日では不一致を警告しない。

### DBとレポート表示

- 新しい集計値が必要な場合は、`ChatbotApiUsageDaily`、`ApiUsageIncrement`、Repositoryのupsert、
  `FeatureUsage` を一緒に更新する。
- 既存DBへ列を追加する場合は、`create_chatbot_tables` に `ADD COLUMN IF NOT EXISTS` を追加する。
- デバッグBotは共有DBのスキーマを変更しない。検証前に本番起動または承認済みの明示的な移行で
  必要なスキーマを適用する。
- 新しい `operation` は `FEATURE_LABELS` と `ITEM_LABELS` に表示名を追加する。
- 長期記憶関連としてまとめる場合は `MEMORY_OPERATIONS` にも追加する。
- 確定コスト、機能別推定、未配賦差額、OpenAI請求内訳が矛盾しないことを確認する。

## テスト

少なくとも次の回帰テストを追加・更新する。

- APIレスポンスからtoken数と課金ツール回数を正しく取得できる。
- cached inputとcache writeをinput tokenから重複課金しない。
- 通常・長文コンテキストのモデル単価を正しく計算できる。
- custom profileで許可したモデルの単価が登録されている。
- Organization Usage APIのモデル別結果とページを集約できる。
- OpenAI側とローカル側が不一致の場合だけ確認事項を表示する。
- 新しい機能名、件数、token数、費用をDiscord Embedへ表示できる。

変更後は、変更範囲に対して次を実行する。

```powershell
uv run ruff check <変更対象>
uv run ruff format --check <変更対象>
uv run ty check <変更対象>
uv run python -m unittest discover
```

## Discord手動スモークテスト

API usageレポートまたはDiscord上のChatbot挙動に影響する変更では、次を確認する。

1. `.env` にOpenAI、Chatbot DB、Discord、レポート投稿先の設定があることを確認する。
2. DB列を追加した場合は、Bot起動前に対象DBへマイグレーションが適用済みであることを確認する。
3. デバッグBotを起動し、Discord Gatewayへの接続とAPI usage Cogの読み込みをログで確認する。
4. 対象のOpenAI API機能をDiscord上で実行する。
5. `/api_usage report` を実行し、機能別件数、token、cache write、推定額、確定額、請求内訳を確認する。
6. 対象操作に伴う未処理例外や予期しない警告がログにないことを確認する。
7. テスト用Botを終了し、実行内容と結果を作業報告へ記載する。

実費との一致を検証する場合は、Costs APIの反映遅延を避けるため完了済みのUTC日を対象にする。
