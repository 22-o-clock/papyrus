# OpenAI API usageレポートの保守

OpenAI API callを追加・変更する場合は、機能の実装と同じ変更単位でusageレポートも更新する。

## 更新項目

### ローカル計測

- API callを `observe_chatbot_api_call` 経由で実行し、安定した `operation` 名、実際のモデル名、
  必要に応じて `item_count` を渡す。
- 新しいusage値が必要なら、`ApiUsageIncrement`、`ChatbotApiUsageDaily`、Repositoryのupsert、
  `FeatureUsage` を一緒に更新する。
- DB列を追加する場合は、`create_chatbot_tables` に既存DB用の `ADD COLUMN IF NOT EXISTS` を追加する。
- 日次レポートは費用と全機能の合計利用量を表示する。新しい `operation` も合計へ含める。
- 機能別・モデル別・キャッシュ別の計測は保存し、通常のレポートでは細かな内訳を表示しない。
- 長期記憶文書の通常更新は `memory_document_update`、文字数超過時の再生成は
  `memory_document_shorten` として分けて計測する。
- `memory_document_update` の `item_count` には同じcallで処理したチャンネル数、
  `memory_document_shorten` には短縮した文書数を記録する。
- 未反映の他チャンネル情報をFunction Callingで取得した後の再生成は
  `draft_generation_pending_memory_followup` として通常の `draft_generation` から分けて計測する。
- DEBUGログでは、operation、モデル、input、cached input、cache write、output、reasoning、totalの各token数と、
  Web検索・Code Interpreterの利用回数を確認できる状態を維持する。

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
- 新機能のリクエスト数、入力・出力トークン数、推定額が合計へ反映されることを確認する。
- OpenAI確定額と推定額、進行中・部分集計・取得失敗の表示を確認する。
- 通常は「コスト概要」「利用量」の2項目とし、異常がある場合だけ確認事項を追加する。
- OpenAI Usageとの差が出た場合は、call数とinput・cached・output tokenのどこに差があるか確認する。
- 同じOpenAI ProjectをPapyrus以外から使用した費用は、ローカル計測との差額になる点に注意する。

通常のテスト、静的検証、Discord手動スモークテストは
[`development.md`](../development.md) の手順に従う。

GPT-6 Lunaの通常入力・キャッシュ読み取り・書き込み・出力と、272,000トークンを超える入力の単価を登録する。
既定の応答生成、長期記憶文書の更新、添付解析にはGPT-6 Lunaを使用する。
カスタムプロファイル用のGPT-6.1 Solも、通常入力・キャッシュ・長文入力の単価を登録する。

## 画像生成

- `image_generation` はGPT Image 2.5 FlareのImages APIを使用し、呼び出しごとのusageを保存する。
- 新規生成のテキスト入力は100万トークン当たり$5、画像出力は$30として推定額に含める。
- 画像入力は渡さない。画像編集を追加する場合は、画像入力・テキスト入力を分離して計測する。
- Organization Usageのimagesエンドポイントも取得し、画像生成はリクエスト数で照合する。
  completions・embeddingsのトークン比較から画像生成を除外する。
- 画像のバイナリは生成中だけ保持し、会話スナップショットにはDiscord添付の情報を保存する。
