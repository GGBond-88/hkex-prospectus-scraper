"""Pure decision functions: is this filing an English final IPO prospectus?"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from hk_ipo.l0.models import Filing


_HAS_LATIN = re.compile(r"[A-Za-z]")
_HAS_CJK = re.compile(r"[一-鿿]")


def has_english_version(filing: Filing) -> bool:
    """Return True if an English-language version of this filing is available.

    Precedence (per spec section 4):
      1. LANGUAGE_CD-derived `filing.language` field ('en' / 'bilingual').
      2. URL suffix: '_e.pdf' implies English, '_c.pdf' implies Chinese only.
      3. Title heuristic: presence of Latin letters and absence of CJK only.
    """
    if filing.language in ("en", "bilingual"):
        # Only trust the language field if it's not contradicted by a clearly Chinese URL.
        if filing.doc_url.lower().endswith("_c.pdf"):
            return False
        return True
    if filing.language == "zh":
        if filing.doc_url.lower().endswith("_e.pdf"):
            return True
        if filing.doc_url.lower().endswith("_c.pdf"):
            return False
        # Fall through to title heuristic.
    if _HAS_LATIN.search(filing.doc_title) and not _HAS_CJK.search(filing.doc_title):
        return True
    return False


_VALID_DOC_TYPES = {"Prospectus", "Listing Document - GEM"}


def is_english_prospectus(filing: Filing) -> bool:
    """Spec section 4: final + accepted doc type + has English version."""
    return (
        filing.doc_type in _VALID_DOC_TYPES
        and filing.is_final
        and has_english_version(filing)
    )


class SkipReason(str, Enum):
    NO_ENGLISH = "no_english_version_published"
    WRONG_DOC_TYPE = "not_final_prospectus"


def should_skip(filing: Filing) -> SkipReason | None:
    """If the filing should be skipped, return why; else None."""
    if filing.doc_type not in _VALID_DOC_TYPES or not filing.is_final:
        return SkipReason.WRONG_DOC_TYPE
    if not has_english_version(filing):
        return SkipReason.NO_ENGLISH
    return None


@dataclass(slots=True, frozen=True)
class FilterDecision:
    keep: bool
    skip_reason: SkipReason | None

    @classmethod
    def from_filing(cls, filing: Filing) -> "FilterDecision":
        reason = should_skip(filing)
        return cls(keep=reason is None, skip_reason=reason)
