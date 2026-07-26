"""冷笑ポイント集計で共有する定数。"""

import datetime
from decimal import Decimal

JST = datetime.timezone(datetime.timedelta(hours=9))

# 冷笑ポイントの対象とするUnicode絵文字。カスタム絵文字は対象外とする。
CYNICISM_EMOJI = "🥶"
# 一部のクライアントが絵文字へ付与する異体字セレクタ。本文判定の前に除去する。
VARIATION_SELECTOR = "\ufe0f"

# 冷笑ポイントの根拠種別。
REACTION_SOURCE = "reaction"
REPLY_SOURCE = "reply"

# 中立な第三者としての判定を重く見るため、Papyrusの既定値を人間より大きくする。
DEFAULT_PAPYRUS_WEIGHT = Decimal("3.00")
DEFAULT_HUMAN_WEIGHT = Decimal("1.00")
MINIMUM_WEIGHT = Decimal(0)
MAXIMUM_WEIGHT = Decimal(100)

# 設定は単一行で保持する。
CONFIGURATION_ID = 1

# 期間終了の翌日この時刻(JST)に発表する。
PUBLISH_HOUR = 21
REPORT_CHECK_INTERVAL_MINUTES = 1
RANKING_DISPLAY_LIMIT = 5
