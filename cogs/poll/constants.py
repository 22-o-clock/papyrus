"""秘密投票で共有する定数。"""

import datetime

JST = datetime.timezone(datetime.timedelta(hours=9))

# 投票期間を省略した場合の指定。
DEFAULT_DURATION = "24h"
# 投票期間として受け付ける最短の長さ。
MIN_DURATION = datetime.timedelta(minutes=1)
# 締め切りから結果の確定までの、作成者が投票を再開できる期間。
REOPEN_WINDOW = datetime.timedelta(hours=24)

MIN_OPTIONS = 2
# セレクトメニューに並べられる選択肢の上限。
MAX_OPTIONS = 25
# セレクトメニューのラベルの文字数上限。
MAX_OPTION_LENGTH = 100
# Embedタイトルの文字数上限。
MAX_QUESTION_LENGTH = 256

# 締め切りと確定の時刻を確認する間隔。
CHECK_INTERVAL_MINUTES = 1
# 投票画面を操作できる時間。
VOTE_PANEL_TIMEOUT_SECONDS = 600
