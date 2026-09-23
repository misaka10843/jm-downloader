import logging
import re

from rich.logging import RichHandler

_JAPANESE_KANA = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')

_BRACKET_PAIRS = [
    ('(', ')'),
    ('[', ']'),
    ('【', '】'),
    ('（', '）'),
    ('〈', '〉'),
    ('《', '》'),
    ('{', '}'),
]
_BRACKET_RE = re.compile(
    "|".join(f"{re.escape(a)}.*?{re.escape(b)}" for a, b in _BRACKET_PAIRS),
    flags=re.S,
)
_LEFTOVER_BRACKETS = "[](){}<>【】（）〈〉《》"
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:\\"/\\|?*\u2000-\u206F\u3000]')
_WHITESPACE_RE = re.compile(r'\s+')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10))
}


def remove_bracketed_segments_once(s: str) -> str:
    return _BRACKET_RE.sub("", s)


def remove_all_bracketed(s: str) -> str:
    prev = None
    cur = s
    while cur != prev:
        prev = cur
        cur = remove_bracketed_segments_once(cur)
    cur = cur.translate({ord(c): None for c in _LEFTOVER_BRACKETS})
    return cur


def truncate_by_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode('utf-8', 'ignore').strip()


def is_windows_reserved(name: str) -> bool:
    """是否是 Windows 保留设备名（CON.txt / nul.log 也算，所以看第一个点之前）。"""
    stem = name.split('.', 1)[0].strip()
    return stem.upper() in _WINDOWS_RESERVED


def sanitize_filename(name: str, max_len: int = 180) -> str:
    if not name:
        return "untitled"
    s = name.strip()
    s = _WHITESPACE_RE.sub(" ", s)
    s = _INVALID_FILENAME_CHARS.sub("", s)
    s = s.rstrip(". ")
    if not s:
        return "untitled"

    if len(s) > max_len:
        s = s[:max_len].rstrip()

    s = truncate_by_bytes(s, 230)

    if is_windows_reserved(s):
        s = "_" + s
    return s


def clean_title_for_filename(title: str, extract_brackets: bool = True, max_len: int = 180) -> str:
    if not title:
        return "untitled"
    t = title
    if extract_brackets:
        stripped = remove_all_bracketed(t)
        # 整段都在括号里时（"[社团]"）剥离结果为空；保留原文，否则多个这种本子
        # 会共用 "untitled" 目录互相覆盖。
        if stripped.strip():
            t = stripped
    t = t.strip()
    t = _WHITESPACE_RE.sub(" ", t)
    t = sanitize_filename(t, max_len=max_len)
    return t or "untitled"


# 兼容层：旧版 _BRACKET_RE 对 ASCII 括号转义有误（"Title (Author)" 会被洗成
# "Title Author"）。仅供 repacker 匹配历史目录名，不要在新逻辑里用。
_LEGACY_BRACKET_RE = re.compile(
    r'\\(.*?\\)|\\[.*?\\]|【.*?】|（.*?）|〈.*?〉|《.*?》|\\{.*?\\}',
    flags=re.S,
)


def legacy_remove_all_bracketed(s: str) -> str:
    prev = None
    cur = s
    while cur != prev:
        prev = cur
        cur = _LEGACY_BRACKET_RE.sub("", cur)
    return cur.translate({ord(c): None for c in _LEFTOVER_BRACKETS})


def legacy_clean_title_for_filename(title: str, max_len: int = 180) -> str:
    """等价于旧版 clean_title_for_filename(title, extract_brackets=True)"""
    if not title:
        return "untitled"
    t = legacy_remove_all_bracketed(title).strip()
    t = _WHITESPACE_RE.sub(" ", t)
    return sanitize_filename(t, max_len=max_len) or "untitled"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(rich_tracebacks=True, markup=True)
        ]
    )

    logging.getLogger('jmcomic').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
