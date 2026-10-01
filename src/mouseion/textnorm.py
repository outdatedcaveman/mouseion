"""One text normaliser for every title/name comparison in Mouseion.

Six copies used to keep only [a-z0-9] after stripping accents, so every Cyrillic, Greek,
Hebrew, Arabic, CJK ... title normalised to the EMPTY string (2026-10-01: 59 real Russian and
Ukrainian titles were listed as "empty title" junk, and no non-Latin title could match a
search result). Letters of every script are kept now; accents are still folded (é -> e,
й -> и), case is folded, everything that is not a letter or digit becomes a single space.
"""
from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r"[\W_]+")          # str patterns are Unicode-aware: \w covers every script


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return " ".join(_NON_WORD.sub(" ", s).split())
