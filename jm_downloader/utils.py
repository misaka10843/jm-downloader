import logging
import re
from pathlib import Path
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

# Kana ranges: Hiragana (3040-309F) + Katakana (30A0-30FF)
# We might also want to catch some common Japanese punctuation if needed, but Kana is the strongest signal.
_JAPANESE_KANA = re.compile(r'[\u3040-\u309F\u30A0-\u30FF]')

# bracket pairs (halfwidth + fullwidth)
_BRACKET_PAIRS = [
    (r'\\(', r'\\)'),  # ()
    (r'\\[', r'\\]'),  # []
    (r'【', r'】'),
    (r'（', r'）'),
    (r'〈', r'〉'),
    (r'《', r'》'),
    (r'\\{', r'\\}')
]
_BRACKET_REGEX_PARTS = []
for a, b in _BRACKET_PAIRS:
    _BRACKET_REGEX_PARTS.append(f"{a}.*?{b}")
_BRACKET_RE = re.compile("|".join(_BRACKET_REGEX_PARTS), flags=re.S)
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
    # remove any leftover single bracket characters
    cur = cur.translate({ord(c): None for c in _LEFTOVER_BRACKETS})
    return cur


def truncate_by_bytes(s: str, max_bytes: int) -> str:
    """Truncate string to max_bytes in UTF-8."""
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode('utf-8', 'ignore').strip()

def sanitize_filename(name: str, max_len: int = 180) -> str:
    if not name:
        return "untitled"
    s = name.strip()
    s = _WHITESPACE_RE.sub(" ", s)
    s = _INVALID_FILENAME_CHARS.sub("", s)
    s = s.rstrip(". ")
    if not s:
        return "untitled"
    
    # 1. Truncate by characters first (fast loose check)
    if len(s) > max_len:
        s = s[:max_len].rstrip()
        
    # 2. Truncate by bytes (strict filesystem check)
    # 255 bytes is standard limit. We use 230 to allow for suffix/extension/path overhead.
    s = truncate_by_bytes(s, 230)
    
    up = s.upper()
    if up in _WINDOWS_RESERVED:
        s = "_" + s
    return s


def clean_title_for_filename(title: str, extract_brackets: bool = True, max_len: int = 180) -> str:
    if not title:
        return "untitled"
    t = title
    if extract_brackets:
        t = remove_all_bracketed(t)
    t = t.strip()
    t = _WHITESPACE_RE.sub(" ", t)
    t = sanitize_filename(t, max_len=max_len)
    return t or "untitled"


def setup_logging():
    # Root Logger Config
    # We capture everything at least INFO but handle output differently
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            # Console Handler: Rich
            # We filter unwanted libraries here or set specific loggers
            RichHandler(rich_tracebacks=True, markup=True)
        ]
    )
    
    # Suppress jmcomic INFO
    # jmcomic uses valid logger names usually? 
    # If not sure, we can suppress common ones or check usage.
    # Assuming 'jmcomic' is the logger name.
    logging.getLogger('jmcomic').setLevel(logging.WARNING)
    
    # Silence requests/urllib3 info too commonly
    logging.getLogger('urllib3').setLevel(logging.WARNING)
