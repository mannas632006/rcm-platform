"""The corpus loader's contract, without touching a database (M2.1).

`tools/load_corpus.py` is a CI-only path: nothing in the ordinary test run
calls it, because every suite that needs a loaded database loads one itself.
That is precisely the shape of thing that has broken twice in this
milestone, so the parts of it that carry meaning are pinned here.

The ordering is the part that carries meaning. Loading is not a bag of files
to be processed in any convenient order:

  * a 277CA rejection must be recorded before the 835s, or the only account
    of what happened to a rejected claim is the remittance that never
    arrived (PD-4);
  * a resubmission must find its original already present, or the chain
    breaks and the denial rate inflates (BR-DATA-03, PD-7).

A dict preserves insertion order, which makes the ordering easy to change by
accident while everything still runs.
"""

from __future__ import annotations

from pathlib import Path

from tools.load_corpus import STAGES, main


def test_claims_load_before_remittances() -> None:
    """PD-7/BR-DATA-03: a resubmission must find its original present."""
    order = list(STAGES)
    assert order.index("837") < order.index("835")


def test_acknowledgements_load_before_remittances() -> None:
    """PD-4: a rejection recorded after the 835s would be a rejection nothing
    consulted when deciding what happened to the claim."""
    order = list(STAGES)
    assert order.index("999") < order.index("835")
    assert order.index("277CA") < order.index("835")


def test_every_stage_has_a_loader() -> None:
    assert all(callable(loader) for loader in STAGES.values())
    assert set(STAGES) == {"837", "999", "277CA", "835"}


def test_a_missing_corpus_is_reported_rather_than_crashing(tmp_path: Path) -> None:
    """The common operator mistake is running this before the generator.

    It exits 2 naming the command to run first, rather than raising from
    somewhere inside a glob -- and before opening a database connection, so
    the message is about the corpus and not about Postgres.
    """
    assert main(["--corpus", str(tmp_path / "nothing")]) == 2


def test_an_empty_corpus_is_refused(tmp_path: Path) -> None:
    """A corpus directory that exists but holds no files would otherwise
    load nothing, report success, and leave the next step building over an
    empty warehouse -- the exact CI failure this tool was written to fix."""
    (tmp_path / "x12").mkdir(parents=True)
    for folder in STAGES:
        (tmp_path / "x12" / folder).mkdir()

    assert main(["--corpus", str(tmp_path), "--archive", str(tmp_path / "raw")]) == 1
