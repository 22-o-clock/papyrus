import asyncio
import datetime
from dataclasses import dataclass, field


@dataclass
class ChannelProcessingState:
    """チャンネル履歴の更新ロックと最後の人間投稿時刻を保持します。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_human_message_timestamp: datetime.datetime | None = None
