from typing import cast


def omit_empty_values(value: dict[str, object]) -> dict[str, object]:
    """省略時の意味が明らかな空値とfalseを再帰的に除きます。"""
    compact: dict[str, object] = {}
    for key, item in value.items():
        if item is None or item is False or item == "" or (isinstance(item, list | dict) and not item):
            continue
        if isinstance(item, dict):
            compact[key] = omit_empty_values(cast("dict[str, object]", item))
        elif isinstance(item, list):
            compact[key] = [
                omit_empty_values(cast("dict[str, object]", nested)) if isinstance(nested, dict) else nested for nested in item
            ]
        else:
            compact[key] = item
    return compact
