# 機能概要

Papyrusで利用できる操作と、メッセージやDiscordイベントを契機に自動で動作する機能を、利用者の視点でまとめます。

## スラッシュコマンド

### Chatbot

- `/chatbot role_show`: 現在のチャンネルのChatbot役割を表示します。
- `/chatbot role_set`: 現在のチャンネルのChatbot役割を変更します。
- `/chatbot role_reset`: 現在のチャンネル固有のChatbot役割を解除します。
- `/chatbot conversation_reset`: 会話をリセットするまでの時間を変更します。
- `/chatbot question_wait`: 宛先のない質問へ応答するまでの待機範囲を変更します。
- `/chatbot profile_save`: カスタムプロファイルを保存します。
- `/chatbot profile_disable`: カスタムプロファイルを無効化します。
- `/chatbot profile_show`: カスタムプロファイルを表示します。
- `/chatbot profile_list`: 有効なカスタムプロファイルを一覧表示します。
- `/chatbot aliases_export`: メンバー別名をExcelで出力します。
- `/chatbot aliases_import`: 編集済みのメンバー別名Excelを取り込みます。
- `/chatbot memories_export`: 長期記憶をExcelで出力します。
- `/chatbot memories_import`: 編集済みの長期記憶Excelを取り込みます。

### API利用レポート

- `/api_usage report`: 指定日のOpenAI API利用レポートを投稿または更新します。
- `/api_usage schedule`: 日次レポートの投稿時刻を変更します。
- `/api_usage status`: 現在の設定と配送状態を表示します。

### モデレーションとリアクション

- `/moderation post_ban`: 指定ユーザーのURL・添付投稿を制限します。
- `/moderation post_unban`: 指定ユーザーのURL・添付投稿の制限を解除します。
- `/moderation expression_config`: 禁止表現を検出した際のリアクション／自動削除を切り替えます。
- `/moderation reaction_ban`: 指定ユーザーのリアクションを制限します。
- `/moderation reaction_unban`: 指定ユーザーのリアクション制限を解除します。
- `/moderation reaction_remove`: 直近の自分の投稿からリアクションを削除します。
- `/moderation reaction_remove_bot`: 直近のBot投稿からリアクションを削除します。

### メッセージのコピー

- `/copy to_new_thread`: 現在の履歴を新しいスレッドへコピーします。
- `/copy all`: 現在の全履歴を既存のチャンネルまたはスレッドへコピーします。
- `/copy range`: メッセージURLで指定した範囲だけをコピーします。

コピーでは、投稿者名、アイコン、添付、Embed、返信関係、編集済み表示を可能な範囲で維持します。

### メッセージと履歴の管理

- `/talkdata member_upsert`: サーバーのメンバー一覧をTalkDataへ登録・更新します。
- `/talkdata channel_upsert`: サーバーのチャンネル一覧をTalkDataへ登録・更新します。
- `/talkdata messages_insert_current`: 現在のチャンネルの既存メッセージをTalkDataへ一括登録します。
- `/talkdata messages_insert_all`: 全テキストチャンネルとスレッドの既存メッセージをTalkDataへ一括登録します。
- `/talkdata message_history`: 指定メンバーの削除・編集済みメッセージを期間指定で表示します。

### リマインダー

- `/remind`: 絶対時刻または相対時間を指定してリマインダーを登録します。
- `/reminder list`: 自分の登録済みリマインダーを表示します。
- `/reminder remove`: 一覧番号またはUUIDでリマインダーを削除します。

リマインダーの時刻には、`MM/DD HH:MM`、`HH:MM`、`3h9m` などを使用できます。

### テキストの台詞

- `/arknights random`: 全体または指定したキャラクターから、ランダムな台詞を返します。
- `/arknights doctor`: 「ドクター」を含む台詞を選び、実行者へのメンションに置き換えます。
- `/arknights doctor_rest`: 指定ユーザーまたは実行者へ定型の台詞を送ります。
- `/arknights search`: 台詞からキャラクターとボイス種別を検索します。

### ボイスチャンネルと音声読み上げ

- `/voice connect`: Botを実行者のボイスチャンネルへ接続します。
- `/voice disconnect`: Botをボイスチャンネルから切断します。
- `/voice pause`: 再生中のVOICEVOX音声を一時停止します。
- `/voice speaker_set`: 利用可能な話者・スタイルから自分の読み上げ音声を選びます。
- `/voice speaker_show`: 現在のVOICEVOX話者設定を表示します。

### その他

- `/hi`: 実行者へランダムな挨拶を返します。
- `/choice`: 空白区切りの選択肢から1つをランダムに選びます。

## メッセージのコンテキストメニュー

メッセージを右クリックまたは長押しして「アプリ」から実行します。

- `agree`: 対象メッセージを復唱し、同意の連鎖を表す連番と元メッセージへのリンクを付けます。
- `disagree`: 対象メッセージを引用し、「そんなことはないですね」を付けて投稿します。
- `get_message_history`: TalkDataに保存された対象メッセージの編集履歴を表示します。

## 自動・バックグラウンド動作

### Chatbot

- **Chatbot**: Discordの投稿、編集、削除、返信、メンション、リアクションを会話文脈へ同期し、
  チャンネルの役割と会話状況に応じて応答します。
- **添付解析**: Chatbotの文脈に含まれる画像やPDFを解析し、応答へ利用します。
- **長期記憶**: 会話から記憶候補とメンバーの別名を抽出し、必要な記憶を応答時に検索します。

### メッセージの記録と監査

- **TalkData**: 投稿、編集、削除をデータベースへ保存し、メッセージの履歴を維持します。
- **監査ログ**: 編集・削除されたメッセージの本文、添付、返信関係などを監査ログへ転送します。

### モデレーション

- **モデレーション**: 表記揺れを正規化して禁止表現を検出し、設定に応じてリアクションまたは自動削除で対処します。
  指定ユーザーのURL・添付投稿やリアクションの制限も適用します。

### 通知と定期処理

- **リマインダー配送**: 登録された時刻を定期的に確認して通知し、遅延していた通知も再送します。
- **API利用レポート**: ChatbotのOpenAI API利用量を記録し、設定時刻に日次レポートを投稿・更新します。
- **予定イベント通知**: Discordの予定イベントについて、作成、更新、開始、終了、中止を通知します。
- **Last.fm更新監視**: 設定されたユーザーのScrobbleが長時間更新されていない場合に警告します。
- **TalkData接続監視**: データベース接続を定期確認し、連続して接続できない場合はエラーを記録します。

### Spotify Embed補完

- **Spotify Embed補完**: DiscordがSpotifyリンクのEmbedを生成しなかった場合に、Spotifyの公開メタデータから
  代替カードを返信します。1投稿につき最大5件を処理します。

### テキスト読み上げ

- **VOICEVOX読み上げ**: Botがボイスチャンネルへ接続している間、指定テキストチャンネルの投稿を合成音声で
  読み上げます。Botの投稿とURLを含む投稿は対象外です。

### 共通処理

- **コマンドエラー処理**: 権限不足や処理済みのエラーを利用者向けの応答へ変換し、予期しない例外をログへ記録します。
