"""投票者を、秘密鍵なしでは照合できないキーへ変換する。"""

import hashlib
import hmac


def make_ballot_key(secret: bytes, poll_id: int, user_id: int) -> str:
    """投票ごとに異なる、投票者の秘匿キーを返す。

    DiscordのユーザーIDは公開されており数も少ないため、単純なハッシュでは総当たりで照合できる。
    DBの外で管理する秘密鍵を用いたHMACにすることで、DBだけを閲覧できる者には照合できないようにする。
    """
    return hmac.new(secret, f"{poll_id}:{user_id}".encode(), hashlib.sha256).hexdigest()
