"""Read Seedream output sizes explicitly requested in the current QQ message."""

import re
import unicodedata


SEEDREAM_TIERS = {"1K", "1.5K", "2K"}
PIXEL_SIZE = r"([0-9]{1,5})\s*[x×*]\s*([0-9]{1,5})"
SIZE_TOKEN = re.compile(
    rf"(?<![a-z0-9_.-])(?:{PIXEL_SIZE}|[0-9]+(?:\.[0-9]+)?\s*k)(?![a-z0-9_.-])"
)
NEGATED = re.compile(r"(?:不要|不用|不需要|不必|无需|避免|别)[^，。？！；,?!;\n]{0,12}$")
DISCUSSION = re.compile(r"\s*(?:太贵|算了|不要|不用|是什么|什么意思|显示器|电视|屏幕|对比)")


def seedream_pixels(value):
    if not value or not (match := re.fullmatch(PIXEL_SIZE, value)):
        return None
    width, height = map(int, match.groups())
    if 921600 <= width * height <= 4624220 and max(width, height) <= 16 * min(width, height):
        return f"{width}x{height}"
    return None


def qq_seedream_size(message):
    """Use 1K unless this message contains one affirmative size instruction."""
    text = unicodedata.normalize("NFKC", message or "").lower()
    sizes = list(SIZE_TOKEN.finditer(text))
    if len(sizes) != 1:
        return "1K"
    match = sizes[0]
    if NEGATED.search(text[:match.start()]) or DISCUSSION.match(text[match.end():]):
        return "1K"
    value = match.group()
    if pixels := seedream_pixels(value):
        return pixels
    tier = re.sub(r"\s+", "", value).upper()
    if tier in SEEDREAM_TIERS:
        return tier
    raise ValueError("Seedream 5.5 支持 1K、1.5K、2K，或符合模型像素范围的宽×高。")
