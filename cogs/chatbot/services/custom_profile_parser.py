import re
from dataclasses import dataclass
from enum import StrEnum

PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class CustomProfileDirectiveError(StrEnum):
    """プロファイル指定を回答生成前に拒否する理由。"""

    MISSING_NAME = "missing_name"
    INVALID_NAME = "invalid_name"
    MISSING_CONTENT = "missing_content"


@dataclass(frozen=True)
class ParsedCustomProfileDirective:
    """投稿から分離したプロファイル名と回答対象本文。"""

    name: str
    content: str


class InvalidCustomProfileDirectiveError(ValueError):
    """明示されたoption構文が利用できない場合の例外。"""

    def __init__(self, reason: CustomProfileDirectiveError) -> None:
        self.reason = reason
        super().__init__(reason.value)


def parse_custom_profile_directive(
    content: str,
    *,
    bot_user_id: int,
    directly_mentioned: bool,
) -> ParsedCustomProfileDirective | None:
    """Botへの直接メンションに続く先頭のoption指定だけを抽出します。"""
    if not directly_mentioned:
        return None

    without_bot_mention = re.sub(rf"<@!?{bot_user_id}>", "", content)
    lines = without_bot_mention.splitlines()
    directive_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if directive_index is None:
        return None

    directive_line = lines[directive_index].strip()
    if re.fullmatch(r"option", directive_line, flags=re.IGNORECASE):
        raise InvalidCustomProfileDirectiveError(CustomProfileDirectiveError.MISSING_NAME)
    if re.match(r"^option(?:\s|$)", directive_line, flags=re.IGNORECASE) is None:
        return None

    match = re.fullmatch(r"option\s+(\S+)(?:\s+(.*))?", directive_line, flags=re.IGNORECASE)
    if match is None or PROFILE_NAME_PATTERN.fullmatch(match.group(1)) is None:
        raise InvalidCustomProfileDirectiveError(CustomProfileDirectiveError.INVALID_NAME)

    inline_content = match.group(2)
    content_lines = ([inline_content] if inline_content else []) + lines[directive_index + 1 :]
    request_content = "\n".join(content_lines).strip()
    if not request_content:
        raise InvalidCustomProfileDirectiveError(CustomProfileDirectiveError.MISSING_CONTENT)

    return ParsedCustomProfileDirective(name=match.group(1).lower(), content=request_content)
