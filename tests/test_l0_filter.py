"""Unit tests for hk_ipo.l0.filter (pure functions, no I/O)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hk_ipo.l0.filter import (
    FilterDecision,
    SkipReason,
    has_english_version,
    is_english_prospectus,
    should_skip,
)
from hk_ipo.l0.models import Filing


def _filing(**overrides: object) -> Filing:
    base = {
        "hk_ticker": "09999",
        "doc_id": "2024010100001",
        "doc_title": "Global Offering",
        "doc_url": "https://www1.hkexnews.hk/listedco/listconews/sehk/2024/0115/2024010100001_e.pdf",
        "doc_type": "Prospectus",
        "market": "MB",
        "language": "en",
        "is_final": True,
        "publish_date": datetime(2024, 1, 15, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return Filing(**base)  # type: ignore[arg-type]


# ---------- has_english_version ----------

def test_has_english_version_explicit_language_en() -> None:
    assert has_english_version(_filing(language="en")) is True


def test_has_english_version_explicit_language_bilingual() -> None:
    assert has_english_version(_filing(language="bilingual")) is True


def test_has_english_version_chinese_only_language_field() -> None:
    assert has_english_version(_filing(language="zh", doc_url="https://x/y_c.pdf")) is False


def test_has_english_version_url_suffix_e_pdf() -> None:
    # Even if language metadata missing, the URL suffix _e.pdf is the tiebreaker.
    f = _filing(language="en", doc_url="https://x/y_e.pdf")
    assert has_english_version(f) is True


def test_has_english_version_url_suffix_c_pdf_overrides() -> None:
    # zh language + _c.pdf URL -> definitely no English.
    f = _filing(language="zh", doc_url="https://x/y_c.pdf")
    assert has_english_version(f) is False


def test_has_english_version_title_cjk_only_when_language_zh() -> None:
    f = _filing(language="zh", doc_title="招股章程", doc_url="https://x/y_c.pdf")
    assert has_english_version(f) is False


def test_has_english_version_title_latin_letters_when_language_en() -> None:
    f = _filing(language="en", doc_title="Global Offering", doc_url="https://x/y_e.pdf")
    assert has_english_version(f) is True


# ---------- is_english_prospectus ----------

def test_is_english_prospectus_final_en_mb() -> None:
    assert is_english_prospectus(_filing()) is True


def test_is_english_prospectus_final_en_gem() -> None:
    f = _filing(doc_type="Listing Document - GEM", market="GEM")
    assert is_english_prospectus(f) is True


def test_is_english_prospectus_rejects_non_final() -> None:
    f = _filing(is_final=False)
    assert is_english_prospectus(f) is False


def test_is_english_prospectus_rejects_wrong_doc_type() -> None:
    f = _filing(doc_type="Annual Report")
    assert is_english_prospectus(f) is False


def test_is_english_prospectus_rejects_chinese_only() -> None:
    f = _filing(language="zh", doc_url="https://x/y_c.pdf", doc_title="招股章程")
    assert is_english_prospectus(f) is False


def test_is_english_prospectus_application_proof_excluded_via_is_final() -> None:
    # Application Proofs / PHIPs / supplemental are surfaced upstream with is_final=False.
    for title in ("Application Proof", "PHIP", "Supplemental Prospectus"):
        f = _filing(doc_title=title, is_final=False)
        assert is_english_prospectus(f) is False, title


def test_is_english_prospectus_accepts_listing_document_gem_alias() -> None:
    # GEM doc type may arrive verbatim from HKEX as "Listing Document - GEM".
    f = _filing(doc_type="Listing Document - GEM", market="GEM")
    assert is_english_prospectus(f) is True


# ---------- should_skip / FilterDecision ----------

def test_should_skip_returns_none_for_keep() -> None:
    assert should_skip(_filing()) is None


def test_should_skip_no_english() -> None:
    f = _filing(language="zh", doc_url="https://x/y_c.pdf", doc_title="招股章程")
    assert should_skip(f) == SkipReason.NO_ENGLISH


def test_should_skip_wrong_doc_type() -> None:
    f = _filing(doc_type="Annual Report")
    assert should_skip(f) == SkipReason.WRONG_DOC_TYPE


def test_should_skip_non_final_treated_as_wrong_doc_type() -> None:
    # A1/PHIP/supplemental are not final; they look like prospectuses but aren't the one we want.
    f = _filing(is_final=False)
    assert should_skip(f) == SkipReason.WRONG_DOC_TYPE


def test_filter_decision_keep() -> None:
    d = FilterDecision.from_filing(_filing())
    assert d.keep is True
    assert d.skip_reason is None


def test_filter_decision_skip() -> None:
    f = _filing(doc_type="Annual Report")
    d = FilterDecision.from_filing(f)
    assert d.keep is False
    assert d.skip_reason == SkipReason.WRONG_DOC_TYPE
