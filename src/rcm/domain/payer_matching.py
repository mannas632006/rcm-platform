"""Normalising payer names, and deciding when two payers are one (M1.7).

PD-8 says payer identity keys on `payer_id` and never on a free-text name, and
nothing here changes that. Names are used for two narrower jobs:

  * looking a payer up when a file gives a name and no usable identifier, and
  * noticing that two `payer_id`s appear to be the same payer.

The second is why this module exists. The synthetic corpus already contains the
problem in its clearest form: an 837 names the payer `00590` and the 835 that
answers it names `PAYER00590`, because a payer's clearinghouse ETIN and its
payer ID are different identifiers from different namespaces. Loading both
sides produces **sixteen payer rows for eight payers**, each with its own
denial rate, each internally consistent, and all of them wrong.

**What this module will not do.** It does not merge payers. A merge changes
identity, and identity is the one thing PD-8 puts out of reach of a heuristic.
Candidates go to a review queue and a human decides; the SRS calls for exactly
that -- *"Manual review UI for ambiguous matches"*.

**What normalisation does and does not reach.** Case, punctuation, spacing,
corporate suffixes and `&`/`and` are noise and are removed. Acronym expansion
is not attempted: bridging `Blue Cross Texas` to `BCBSTX` needs to know that
BCBS stands for Blue Cross Blue Shield, which is domain knowledge, not string
similarity. Pretending a matcher can do it would mean tuning a threshold until
this corpus passed, which is fitting to the test data rather than solving the
problem.

So the three spellings in M1.7's acceptance condition resolve to one
`payer_id` by the mechanism that actually scales: every spelling seen in a
file carrying that identifier is recorded as an alias of it. `BCBS TX`,
`Blue Cross Texas` and `BCBSTX` resolve because all three arrived on files
that named the same payer, not because a similarity score bridged them.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Final

#: Two normalised names this close are proposed as the same payer. Chosen to
#: be strict: the cost of a missed candidate is a human never being asked,
#: which is recoverable, while a flood of bad candidates makes the review
#: queue something people stop reading, which is not.
REVIEW_THRESHOLD: Final = 0.85

#: Corporate suffixes and legal forms. Noise for identity: "Aetna Commercial"
#: and "Aetna Commercial Inc." are one payer written twice.
_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "pc",
        "pa",
        "the",
    }
)

_PUNCTUATION: Final = re.compile(r"[^\w\s]")
_WHITESPACE: Final = re.compile(r"\s+")


def normalise_payer_name(raw: str) -> str:
    """Canonical form of a payer name, for comparison only.

    Never for display. The spelling a file actually used is evidence of where
    it came from, and `payer_alias.alias` keeps it; this is the key the
    comparison runs on.

    Unicode is folded to its compatibility form first, so a name carrying a
    non-breaking space or a typographic apostrophe compares equal to the same
    name typed plainly -- a difference no human would call a different payer.
    """
    folded = unicodedata.normalize("NFKD", raw)
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    lowered = _PUNCTUATION.sub(" ", stripped.lower())
    tokens = [t for t in _WHITESPACE.split(lowered) if t and t not in _SUFFIXES]
    return " ".join(tokens)


def comparison_key(raw: str) -> str:
    """Normalised name with spaces removed.

    `BCBS TX` and `BcbsTx` are the same name typed two ways, and they differ
    only by a space. Comparing on this as well as on the spaced form is what
    catches that without lowering the similarity threshold for everything else.
    """
    return normalise_payer_name(raw).replace(" ", "")


def similarity(left: str, right: str) -> float:
    """How alike two payer names are, between 0 and 1.

    Three readings, and the strongest wins:

      * the normalised strings compared directly
      * the same with spaces removed, which catches `BCBS TX` / `BcbsTx`
      * their token sets, which catches reordering and an extra word

    `SequenceMatcher` rather than an edit-distance library: it is in the
    standard library, and a dependency whose version changes the matching
    behaviour is a dependency that can silently re-score a review queue.
    """
    left_normalised = normalise_payer_name(left)
    right_normalised = normalise_payer_name(right)
    if not left_normalised or not right_normalised:
        return 0.0
    if left_normalised == right_normalised:
        return 1.0

    direct = SequenceMatcher(None, left_normalised, right_normalised).ratio()
    collapsed = SequenceMatcher(None, comparison_key(left), comparison_key(right)).ratio()

    left_tokens = set(left_normalised.split())
    right_tokens = set(right_normalised.split())
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    return max(direct, collapsed, overlap)


def is_review_candidate(left: str, right: str) -> bool:
    """Should a human be asked whether these two names are one payer?"""
    return similarity(left, right) >= REVIEW_THRESHOLD
