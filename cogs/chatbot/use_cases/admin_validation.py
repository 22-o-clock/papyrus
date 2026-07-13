import datetime
import uuid


def parse_memory_admin_target(
    target_type: str,
    target_value: str,
    member_values: dict[str, int],
) -> tuple[int | None, str | None, str]:
    """管理Excelの対象種別と入力値を検証します。"""
    if target_type == "メンバー":
        if target_value not in member_values:
            error_message = "変更後の対象メンバーが選択されていません"
            raise ValueError(error_message)
        return member_values[target_value], None, "member"
    if target_type == "外部対象":
        if not target_value.strip():
            error_message = "外部対象名が空です"
            raise ValueError(error_message)
        return None, target_value.strip(), "external"
    if target_type == "共有情報" and not target_value.strip():
        return None, None, "unresolved"
    error_message = "対象種別と変更後の対象が矛盾しています"
    raise ValueError(error_message)


def parse_memory_admin_expiration(value: object) -> datetime.datetime | None:
    """管理Excelの有効期限をUTC日時へ変換します。"""
    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime.datetime) else datetime.datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    return parsed.astimezone(datetime.UTC)


def validate_exported_memory_ids(seen_ids: set[uuid.UUID], exported_ids: set[uuid.UUID]) -> None:
    """Excel出力時に含まれた行が削除・追加されていないことを保証します。"""
    if seen_ids != exported_ids:
        error_message = "出力された記憶の行が削除または追加されています"
        raise ValueError(error_message)
