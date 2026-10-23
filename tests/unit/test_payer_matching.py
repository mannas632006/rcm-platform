"""Normalising payer names, and scoring how alike two of them are (M1.7).

Pure functions, so these are the cheap half of the evidence. The expensive
half is `tests/integration/test_payer_registry.py`, where the scores decide
what reaches a review queue.

The tests that matter most here are the ones asserting a *low* score. A
matcher that scores everything highly resolves every payer to the first one it
saw, and does it silently.
"""

from __future__ import annotations

import pytest

from rcm.domain.payer_matching import (
    REVIEW_THRESHOLD,
    comparison_key,
    is_review_candidate,
    normalise_payer_name,
    similarity,
)

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BCBS Texas", "bcbs texas"),
        ("BCBS TX", "bcbs tx"),
        ("  BCBS   Texas  ", "bcbs texas"),
        ("Cigna  P.P.O.", "cigna p p o"),
        ("Workers' Comp Carrier", "workers comp carrier"),
        ("Aetna Commercial Inc.", "aetna commercial"),
        ("The Aetna Company", "aetna"),
        ("UHC Commercial, LLC", "uhc commercial"),
    ],
)
def test_noise_is_removed(raw: str, expected: str) -> None:
    """Case, spacing, punctuation and legal form are not identity."""
    assert normalise_payer_name(raw) == expected


def test_accents_and_typographic_characters_fold() -> None:
    """A non-breaking space or a curly apostrophe is not a different payer.

    Built with `chr()` rather than written literally: the characters are
    invisibly different from their plain equivalents, which is the whole point
    of the test and also the reason a linter objects to seeing them in source.
    """
    curly_apostrophe = chr(0x2019)
    non_breaking_space = chr(0xA0)

    assert normalise_payer_name(f"Workers{curly_apostrophe} Comp") == normalise_payer_name(
        "Workers' Comp"
    )
    assert normalise_payer_name(f"Aetna{non_breaking_space}Commercial") == normalise_payer_name(
        "Aetna Commercial"
    )


def test_an_empty_name_normalises_to_nothing() -> None:
    """An empty `N1*PR` is common and is not evidence of anything."""
    assert normalise_payer_name("") == ""
    assert normalise_payer_name("   ") == ""
    assert normalise_payer_name("...") == ""


def test_a_name_that_is_only_a_legal_form_normalises_to_nothing() -> None:
    """Otherwise every "Inc." in the corpus resolves to one payer."""
    assert normalise_payer_name("Inc.") == ""
    assert normalise_payer_name("The Company") == ""


def test_the_comparison_key_drops_spaces() -> None:
    assert comparison_key("BCBS TX") == comparison_key("BcbsTx") == "bcbstx"


# ---------------------------------------------------------------------------
# Similarity: what should match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Aetna Commercial", "AETNA COMMERCIAL"),
        ("Aetna Commercial", "Aetna Commercial Inc."),
        ("Cigna PPO", "Cigna  P.P.O."),
        ("BCBS TX", "BcbsTx"),
        ("State Medicaid MCO", "State Medicaid M.C.O."),
        ("Workers Comp Carrier", "Workers' Comp Carrier"),
        # Letter-spaced names, which some senders produce. These are the pairs
        # that need the space-collapsed reading: compared as written they
        # score 0.78-0.80 and fall below the threshold, so without that
        # reading they would never reach a reviewer. The first version of this
        # list contained no such pair, and dropping the reading entirely broke
        # nothing.
        ("UHC MA", "U H C M A"),
        ("BCBS TX", "B C B S T X"),
    ],
)
def test_the_same_name_typed_differently_is_a_candidate(left: str, right: str) -> None:
    assert is_review_candidate(left, right)


def test_an_identical_name_scores_one() -> None:
    assert similarity("BCBS Texas", "BCBS Texas") == 1.0


def test_token_order_does_not_matter() -> None:
    """A payer written "Texas BCBS" is the same payer."""
    assert is_review_candidate("BCBS Texas", "Texas BCBS")


# ---------------------------------------------------------------------------
# Similarity: what should NOT match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Aetna Commercial", "Cigna PPO"),
        ("UHC Commercial", "UHC Medicare Advantage"),
        ("Medicare Part B", "State Medicaid MCO"),
        ("BCBS Texas", "BCBS Illinois"),
        ("Aetna Commercial", "AETNA"),
    ],
)
def test_different_payers_are_not_candidates(left: str, right: str) -> None:
    """The important half.

    `UHC Commercial` and `UHC Medicare Advantage` share a parent and most of
    their text, and are genuinely different payers with different rules and
    different denial behaviour. A matcher that merges them is worse than one
    that matches nothing, because it is wrong in a way that looks tidy.

    `Aetna Commercial` against `AETNA` is the same problem in miniature: a
    truncation is not a synonym, and the platform cannot tell which Aetna
    product a bare "AETNA" meant.
    """
    assert not is_review_candidate(left, right)


def test_an_empty_name_matches_nothing() -> None:
    assert similarity("", "BCBS Texas") == 0.0
    assert similarity("BCBS Texas", "") == 0.0
    assert not is_review_candidate("", "")


def test_an_acronym_is_not_bridged_to_its_expansion() -> None:
    """Documented behaviour, asserted so it cannot drift silently.

    Reaching `Blue Cross Texas` from `BCBS Texas` means knowing that BCBS
    stands for Blue Cross Blue Shield. That is domain knowledge, not string
    similarity, and the only way a matcher gets there is by lowering the
    threshold until this one case passes -- which is fitting to the test data
    rather than solving the problem.

    The three spellings in M1.7's acceptance condition resolve through the
    alias table instead: each was seen on a file naming the same payer.
    """
    assert similarity("BCBS Texas", "BLUE CROSS TEXAS") < REVIEW_THRESHOLD
    assert similarity("BCBS Texas", "Blue Cross Blue Shield of Texas") < REVIEW_THRESHOLD


def test_the_threshold_is_strict_enough_to_be_worth_having() -> None:
    """A threshold low enough to match anything is not a control.

    Pinned because the cost of lowering it is paid silently: candidates stop
    being reviewed carefully once the queue fills with pairs that are
    obviously not the same payer.
    """
    assert REVIEW_THRESHOLD >= 0.8
    assert similarity("Aetna Commercial", "Cigna PPO") < 0.5
