# OpenAI API usageレポートの保守

OpenAI API callを追加・変更する場合は、機能の実装と同じ変更単位でusageレポートも更新する。

## 更新項目

### ローカル計測

- API callを `observe_chatbot_api_call` 経由で実行し、安定した `operation` 名、実際のモデル名、
  必要に応じて `item_count` を渡す。
- 新しいusage値が必要なら、`ApiUsageIncrement`、`ChatbotApiUsageDaily`、Repositoryのupsert、
  `FeatureUsage` を一緒に更新する。
- DB列を追加する場合は、`create_chatbot_tables` に既存DB用の `ADD COLUMN IF NOT EXISTS` を追加する。
- 新しい `operation` は `FEATURE_LABELS` と `ITEM_LABELS`、必要なら `MEMORY_OPERATIONS` に追加する。

### 単価

- `MODEL_PRICES` に利用モデルとaliasを登録する。custom profileの許可モデルを増やす場合も同時に更新する。
- input、cached input、cache write、output、長文コンテキスト、課金ツールの単価を確認する。
- 適用開始日を保持し、OpenAI公式の[料金表](https://developers.openai.com/api/docs/pricing)を確認した日へ
  `PRICING_VERIFIED_ON` を更新する。

### OpenAI側との照合

- 新しいAPI種別を使う場合は、対応するOrganization Usage APIを
  `OpenAIOrganizationUsageClient.fetch_daily_summary` の取得対象へ追加する。
- Usage APIは `OPENAI_USAGE_PROJECT_ID` で絞り込み、ページネーションを最後まで取得する。
- Usage詳細の取得失敗がCosts APIの確定額取得を妨げない構造を維持する。

## report固有の確認

- 完了済みのUTC日を `/api_usage report` で再集計する。
- 新機能のcall数、token数、cache write、推定額が機能別内訳へ反映されることを確認する。
- OpenAI請求内訳、機能別推定、未配賦差額を確認する。
- OpenAI Usageとの差が出た場合は、call数とinput・cached・output tokenのどこに差があるか確認する。
- 同じOpenAI ProjectをPapyrus以外から使用した費用は、ローカル計測との差額になる点に注意する。

通常のテスト、静的検証、Discord手動スモークテストは
[`development.md`](../development.md) の手順に従う。
