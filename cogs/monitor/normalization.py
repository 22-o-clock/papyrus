import json
import unicodedata
from pathlib import Path

import jaconv
import regex
from pykakasi import kakasi
from unidecode import unidecode

DATA_DIRECTORY = Path(__file__).parent / "data"
MANUAL_MAP_PATH = DATA_DIRECTORY / "map.json"
CONFUSABLE_MAP_PATH = DATA_DIRECTORY / "confusable.json"

JAPANESE_PATTERN = regex.compile(r"[\p{Hiragana}\p{Katakana}\p{Han}]+")
PRESENTATION_RANGES = (
    "\u1d00-\u1d7f",
    "\u1d80-\u1dbf",
    "\u2070-\u209f",
    "\u20a0-\u20cf",
    "\u2400-\u243f",
    "\u2460-\u24ff",
    "\u2700-\u27bf",
    "\u2c60-\u2c7f",
    "\u2c80-\u2cff",
    "\u2d00-\u2d2f",
    "\u3190-\u319f",
    "\u31f0-\u31ff",
    "\u3200-\u32ff",
    "\u3300-\u33ff",
    "\ua640-\ua69f",
    "\ua720-\ua7ff",
    "\uff00-\uffef",
    "\U0001f100-\U0001f1ff",
    "\U0001f200-\U0001f2ff",
    "\U0001f5da-\U0001f5db",
)
PRESENTATION_PATTERN = regex.compile(f"[{''.join(PRESENTATION_RANGES)}]")
INVISIBLE_PATTERN = regex.compile(
    r"[\s\u0009\u0020\u00a0\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e"
    r"\u2000-\u200f\u202f\u205f\u2060-\u2064\u206a-\u206f\u3000\u2800\u3164"
    r"\ufeff\uffa0\U0001d159\U0001d173-\U0001d17a]"
)
NON_LETTER_PATTERN = regex.compile(r"[^\p{Letter}]")
KAKASI = kakasi()


def _load_mapping(path: Path) -> dict[str, str]:
    """正規化用JSONを文字列同士の辞書として読み込む。"""
    raw_mapping: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_mapping, dict):
        error_message = f"mapping data is not an object: {path}"
        raise TypeError(error_message)
    return {str(key): str(value) for key, value in raw_mapping.items()}


def convert_kanji_and_katakana(raw_text: str) -> str:
    """漢字とカタカナをひらがなへ変換する。"""
    for piece in JAPANESE_PATTERN.findall(raw_text):
        converted = "".join(item["hira"] or item["orig"] for item in KAKASI.convert(piece))
        raw_text = raw_text.replace(piece, converted, 1)
    return raw_text


def romanize(raw_text: str) -> str:
    """文字列中の日本語をローマ字へ変換する。"""
    return str(jaconv.kana2alphabet(convert_kanji_and_katakana(raw_text)))


class FuzzyMatch:
    """表記揺れや紛らわしいUnicode文字を正規化してパターン照合する。"""

    def __init__(self, pattern: str = r"$^") -> None:
        self.manual_mapping = _load_mapping(MANUAL_MAP_PATH)
        self.confusable_mapping = _load_mapping(CONFUSABLE_MAP_PATH)
        self.pattern = regex.compile(pattern)

    def preprocess(self, raw_text: str) -> str:
        """互換文字と不可視文字を正規化する。"""
        raw_text = unicodedata.normalize("NFKC", raw_text)
        for character in PRESENTATION_PATTERN.findall(raw_text):
            replacement = self.manual_mapping.get(character, unidecode(character))
            raw_text = raw_text.replace(character, replacement, 1)
        return unicodedata.normalize("NFKC", INVISIBLE_PATTERN.sub("", raw_text))

    def prototype(self, raw_text: str) -> str:
        """Unicode confusableを代表文字列へ置換する。"""
        normalized = unicodedata.normalize("NFD", raw_text)
        normalized = "".join(self.confusable_mapping.get(character, character) for character in normalized)
        normalized = unicodedata.normalize("NFD", normalized)
        return NON_LETTER_PATTERN.sub("", normalized).casefold()

    def is_match(self, raw_text: str) -> bool:
        """正規化後の文字列が設定されたパターンに一致するか返す。"""
        normalized = self.prototype(romanize(self.preprocess(raw_text)))
        return self.pattern.search(normalized) is not None
