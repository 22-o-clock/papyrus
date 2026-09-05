"""冷笑ポイント集計で共有する定数。"""

import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))

# 冷笑ポイントの対象とするUnicode絵文字。
CYNICISM_EMOJI = "🥶"
# 冷笑ポイントの対象とするサーバー固有のカスタム絵文字の名前。
CUSTOM_CYNICISM_EMOJI_NAME = "MEIKO_uh"
# 冷笑ポイントの根拠種別。
REACTION_SOURCE = "reaction"

# ランキング発表の処理結果。
POSTED_STATUS = "posted"
EMPTY_STATUS = "empty"


# 設定は単一行で保持する。
CONFIGURATION_ID = 1

# 期間終了の翌日この時刻(JST)に発表する。
PUBLISH_HOUR = 21
REPORT_CHECK_INTERVAL_MINUTES = 1
RANKING_DISPLAY_LIMIT = 5
