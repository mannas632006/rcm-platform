"""Measure GATE M4 on the synthetic corpus.

    GATE M4: "Replay the synthetic denial corpus. Show measured first-pass
    clean-claim rate before and after rules are active. Demonstrate the
    feedback loop closing on at least one denial pattern."

Steps, against a loaded corpus (`python -m tools.load_corpus`):

1. Reference data. The NCCI pairs and payer authorisation rules the
   synthetic adjudicator applies are loaded as an NCCI release and as payer
   registry entries -- what a team would maintain by hand.
2. The feedback loop runs over every denial: classify, then answer each one
   (M4.8, M4.9).
3. Review, done by this tool in the rules reviewer's place and recorded as
   `gate:m4-rules-reviewer`. A candidate is promoted to ADVISORY only when,
   on claims submitted **before** the cutoff, it flagged something and its
   false-positive rate is within the FP cap (TBD-45, 10%). Candidates are
   drafted ADVISORY, so none can reach BLOCKING.
4. Replay, without writing anything. Each first submission is evaluated
   against the published ruleset with NCCI and the registry. A denied claim
   counts as caught only when acting findings (ADVISORY or BLOCKING) name
   every cause it was denied for. A paid claim that is flagged is counted
   as review burden.

First-pass clean-claim rate = first submissions paid with no denied line /
first submissions with an outcome (adjudicated or rejected). "After" assumes
a caught claim is corrected before it is sent, which is what the scrubber is
for. Before = the corpus as adjudicated: no rule, NCCI edit or registry
entry ran on any of it.

The out-of-time view is the honest one: rules are judged on claims before
the cutoff and measured on claims after it. The in-sample view, all claims,
is printed beside it and labelled as such.

    python -m tools.m4_gate
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rcm.analytics.denials import denied_submitter_claim_ids
from rcm.decision import ncci_store, registry
from rcm.decision.denial_models import DenialClassification
from rcm.decision.denials import TAXONOMY_VERSION, DenialCategory
from rcm.decision.feedback_models import DenialFeedback
from rcm.decision.rules import lifecycle
from rcm.decision.rules.definition import RuleStatus, parse_rule
from rcm.decision.rules.evaluate import ActiveRule, Evaluation, Mode, Outcome, evaluate
from rcm.decision.rules.models import RuleBacktest, RuleVersion
from rcm.decision.rules.scrub import client_facts
from rcm.decision.rules.store import load_ruleset, publish_ruleset
from rcm.domain.acknowledgement import AcknowledgementOutcome, ClaimAcknowledgement
from rcm.domain.claim import Claim
from rcm.domain.payer import Payer
from rcm.domain.remittance import ClaimPayment
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from rcm.work import denials as work_denials
from tools.load_corpus import DEFAULT_CLIENT

REFERENCE = Path(__file__).resolve().parent / "synthetic" / "reference"
REVIEWER = "gate:m4-rules-reviewer"
REGISTRY_OWNER = "gate:registry-owner"
NCCI_LABEL = "SYNTHETIC"
_ACTING = frozenset({Mode.BLOCKING, Mode.ADVISORY})
_CHECKS = {
    "NCCI_PTP": DenialCategory.CODING,
    "NCCI_MUE": DenialCategory.MEDICAL_NECESSITY,
    registry.PRIOR_AUTH_CHECK: DenialCategory.AUTHORISATION,
}


def _say(text: str = "") -> None:
    """A console report, as tools/advance_claims.py writes one."""
    sys.stdout.write(f"{text}\n")


def seed_reference(session: Session, *, today: date) -> tuple[int, int]:
    """The NCCI pairs and payer authorisation rules the adjudicator applies."""
    with (REFERENCE / "ncci_edits.csv").open(encoding="utf-8") as handle:
        pairs = list(csv.DictReader(handle))
    ptp = io.StringIO()
    writer = csv.writer(ptp)
    writer.writerow(
        ["column_1", "column_2", "effective_date", "deletion_date", "modifier_indicator"]
    )
    for pair in pairs:
        writer.writerow(
            [
                pair["column1_code"],
                pair["column2_code"],
                "20000101",
                "",
                "1" if pair["modifier_bypass_allowed"] == "1" else "0",
            ]
        )
    ncci_store.load_release(
        session,
        kind="PTP",
        label=NCCI_LABEL,
        payload=ptp.getvalue().encode(),
        loaded_by=REGISTRY_OWNER,
    )

    entries = 0
    with (REFERENCE / "payer_rules.csv").open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            payer_id, cpt = row["payer_id"], row["cpt_hcpcs"]
            if session.get(Payer, payer_id) is None:
                continue
            if registry.history(session, payer_id=payer_id, cpt=cpt):
                continue
            registry.record_entry(
                session,
                payer_id=payer_id,
                cpt=cpt,
                prior_auth_required=row["prior_auth_required"] == "Y",
                source_url=None,
                verified_on=today,
                verified_by=REGISTRY_OWNER,
                recorded_by=REGISTRY_OWNER,
                reason="seeded from the synthetic payer rules for GATE M4",
                today=today,
            )
            entries += 1
    return len(pairs), entries


@dataclass(frozen=True, slots=True)
class Submitted:
    submitter_claim_id: str
    submitted_on: date | None
    #: None when no outcome is on file; else REJECTED, CLEAN or DENIED.
    outcome: str | None
    denied_for: frozenset[DenialCategory]


def first_submissions(session: Session, client_id: uuid.UUID) -> dict[str, Submitted]:
    denied = set(session.scalars(denied_submitter_claim_ids(client_id)))
    adjudicated = set(
        session.scalars(
            select(ClaimPayment.claim_id).where(
                ClaimPayment.client_id == client_id, ClaimPayment.is_reversal.is_(False)
            )
        )
    )
    rejected = set(
        session.scalars(
            select(ClaimAcknowledgement.claim_id).where(
                ClaimAcknowledgement.client_id == client_id,
                ClaimAcknowledgement.outcome == str(AcknowledgementOutcome.REJECTED),
            )
        )
    )
    causes: dict[str, set[DenialCategory]] = defaultdict(set)
    for claim_id, category in session.execute(
        select(ClaimPayment.claim_id, DenialClassification.category)
        .join(ClaimPayment, ClaimPayment.claim_payment_id == DenialClassification.claim_payment_id)
        .where(
            DenialClassification.client_id == client_id,
            DenialClassification.taxonomy_version == TAXONOMY_VERSION,
        )
    ):
        causes[claim_id].add(DenialCategory(category))

    out = {}
    for claim in session.scalars(
        select(Claim).where(Claim.client_id == client_id, Claim.original_claim_id.is_(None))
    ):
        key = claim.submitter_claim_id
        outcome: str | None
        if key in adjudicated:
            outcome = "DENIED" if key in denied else "CLEAN"
        elif key in rejected:
            outcome = "REJECTED"
        else:
            outcome = None
        out[key] = Submitted(key, claim.submitted_on, outcome, frozenset(causes.get(key, ())))
    return out


class _Causes:
    """The denial category a finding names: a check's, or a rule's declared one."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._known: dict[tuple[str, int], DenialCategory | None] = {}

    def of(self, rule_id: str, version: int) -> DenialCategory | None:
        if rule_id in _CHECKS:
            return _CHECKS[rule_id]
        if (rule_id, version) not in self._known:
            row = self._session.get(RuleVersion, (rule_id, version))
            declared = (row.document.get("provenance") or {}).get("category") if row else None
            self._known[(rule_id, version)] = DenialCategory(declared) if declared else None
        return self._known[(rule_id, version)]

    def acting(self, evaluation: Evaluation) -> set[DenialCategory]:
        found: set[DenialCategory] = set()
        for finding in evaluation.findings:
            if finding.outcome is Outcome.FLAGGED and finding.mode in _ACTING:
                cause = self.of(finding.rule_id, finding.rule_version)
                if cause is not None:
                    found.add(cause)
        return found


def review_candidates(
    session: Session, client_id: uuid.UUID, claims: dict[str, Submitted], cutoff: date
) -> list[dict[str, object]]:
    """Promote, in the reviewer's place, each candidate whose pre-cutoff FP
    rate is within the cap. Every step goes through M4.5's lifecycle.

    A candidate an earlier run promoted is measured again and reported as
    promoted, and nothing is written for it: run twice, the gate reports the
    same thing and changes nothing."""
    cap = get_settings().blocking_rule_max_false_positive_rate
    facts = {c.submitter_claim_id: f for c, f in client_facts(session, client_id=client_id)}
    before = [
        key
        for key, s in claims.items()
        if s.outcome in ("CLEAN", "DENIED")
        and s.submitted_on is not None
        and s.submitted_on < cutoff
        and key in facts
    ]
    reviewed: list[dict[str, object]] = []
    candidates = session.scalars(
        select(RuleVersion)
        .where(RuleVersion.status.in_([str(RuleStatus.BACKTESTING), str(RuleStatus.ADVISORY)]))
        .order_by(RuleVersion.rule_id)
    ).all()
    for row in candidates:
        provenance = row.document.get("provenance") or {}
        if provenance.get("source") != "denial_feedback":
            continue
        rule = ActiveRule(parse_rule(row.document), row.version, RuleStatus.SHADOW)
        flagged = false_positive = 0
        for key in before:
            if any(f.outcome is Outcome.FLAGGED for f in evaluate([rule], facts[key]).findings):
                flagged += 1
                false_positive += claims[key].outcome == "CLEAN"
        rate = Decimal(false_positive) / Decimal(flagged) if flagged else None
        already = row.status == str(RuleStatus.ADVISORY)
        promoted = already or (rate is not None and rate <= cap)
        if promoted and not already:
            reason = (
                f"GATE M4 review: before {cutoff.isoformat()} it flagged {flagged} claims, "
                f"false-positive rate {rate:.2%}, within the {cap:.0%} cap"
            )
            for step in (RuleStatus.SHADOW, RuleStatus.ADVISORY):
                lifecycle.change_status(
                    session,
                    rule_id=row.rule_id,
                    version=row.version,
                    to=step,
                    changed_by=REVIEWER,
                    reason=reason,
                )
        reviewed.append(
            {
                "rule_id": row.rule_id,
                "category": provenance.get("category"),
                "flagged_before_cutoff": flagged,
                "fp_rate_before_cutoff": rate,
                "promoted": promoted,
                "already": already,
            }
        )
    return reviewed


@dataclass
class Tally:
    claims: int = 0
    clean: int = 0
    denied: int = 0
    rejected: int = 0
    caught: int = 0
    #: Caught by NCCI and the registry alone: reference data, not learned.
    caught_by_reference: int = 0
    flagged_clean: int = 0
    caught_by_cause: Counter[str] = field(default_factory=Counter)
    denied_by_cause: Counter[str] = field(default_factory=Counter)
    #: Per learned rule: denied claims it named a cause of, and paid claims
    #: it flagged.
    rule_hits: Counter[str] = field(default_factory=Counter)
    rule_false: Counter[str] = field(default_factory=Counter)

    def rate(self, clean: int) -> str:
        return f"{Decimal(clean) / Decimal(self.claims):.2%}" if self.claims else "n/a"


def replay(
    session: Session,
    client_id: uuid.UUID,
    claims: dict[str, Submitted],
    *,
    since: date | None,
    today: date,
    active: bool,
) -> Tally:
    """Count each first submission with an outcome, and -- when the rules are
    active -- evaluate it. Writes nothing."""
    tally = Tally()
    ruleset = load_ruleset(session) if active else None
    causes = _Causes(session)
    for claim, facts in client_facts(session, client_id=client_id):
        submitted = claims.get(claim.submitter_claim_id)
        if submitted is None or submitted.outcome is None:
            continue
        if since is not None and (submitted.submitted_on is None or submitted.submitted_on < since):
            continue
        tally.claims += 1
        if submitted.outcome == "REJECTED":
            tally.rejected += 1
            continue
        if submitted.outcome == "DENIED":
            tally.denied += 1
            for cause in submitted.denied_for:
                tally.denied_by_cause[str(cause)] += 1
        else:
            tally.clean += 1
        if ruleset is None:
            continue
        ncci = ncci_store.check_for(
            session,
            ptp_release=ruleset.ncci_ptp_release,
            mue_release=ruleset.ncci_mue_release,
            codes={str(line.values["line.procedure_code"]) for line in facts.lines},
        )
        payer_registry = registry.check_for(session, payer_id=claim.payer_id, today=today)
        evaluation = evaluate(ruleset.rules, facts, checks=[ncci, payer_registry])
        named = causes.acting(evaluation)
        reference = causes.acting(evaluate([], facts, checks=[ncci, payer_registry]))
        for finding in evaluation.findings:
            if (
                finding.rule_id.startswith("FB_")
                and finding.outcome is Outcome.FLAGGED
                and finding.mode in _ACTING
            ):
                rule_cause = causes.of(finding.rule_id, finding.rule_version)
                if submitted.outcome == "CLEAN":
                    tally.rule_false[finding.rule_id] += 1
                elif rule_cause in submitted.denied_for:
                    tally.rule_hits[finding.rule_id] += 1
        if submitted.outcome == "CLEAN":
            tally.flagged_clean += bool(named)
        # Caught only when every cause it was denied for is named: a claim
        # with two refusals is clean only once both are fixed.
        elif submitted.denied_for and submitted.denied_for <= named:
            tally.caught += 1
            tally.caught_by_reference += submitted.denied_for <= reference
            for cause in submitted.denied_for:
                tally.caught_by_cause[str(cause)] += 1
    return tally


def print_tally(title: str, tally: Tally) -> None:
    _say(f"\n{title}")
    _say(f"  first submissions with an outcome   {tally.claims}")
    _say(f"    rejected (front end, PD-4)        {tally.rejected}")
    _say(f"    denied                            {tally.denied}")
    _say(f"    paid clean                        {tally.clean}")
    _say(f"  first-pass clean-claim rate before  {tally.rate(tally.clean)}")
    if tally.caught:
        _say(f"  caught by NCCI + registry alone     {tally.caught_by_reference}")
        _say(
            "  rate after, reference data only     "
            f"{tally.rate(tally.clean + tally.caught_by_reference)}"
        )
        _say(f"  caught only with learned rules too  {tally.caught - tally.caught_by_reference}")
    _say(f"  denied claims caught, all checks    {tally.caught}")
    _say(f"  first-pass clean-claim rate after   {tally.rate(tally.clean + tally.caught)}")
    _say(f"  paid claims also flagged (review)   {tally.flagged_clean}")
    _say("  by cause                    denied  caught")
    for cause, count in tally.denied_by_cause.most_common():
        _say(f"    {cause:<26}{count:>8}  {tally.caught_by_cause[cause]:>6}")


def loop_closing(
    session: Session,
    claims: dict[str, Submitted],
    reviewed: Iterable[dict[str, object]],
    cutoff: date,
) -> list[dict[str, object]]:
    """Each promoted candidate: the denials it answered, and its backtest."""
    out = []
    for review in reviewed:
        if not review["promoted"]:
            continue
        rule_id = str(review["rule_id"])
        answered = session.scalars(
            select(ClaimPayment.claim_id)
            .select_from(DenialFeedback)
            .join(ClaimPayment, ClaimPayment.claim_payment_id == DenialFeedback.claim_payment_id)
            .where(DenialFeedback.rule_id == rule_id)
        ).all()
        before = 0
        for key in answered:
            s = claims.get(key)
            if s is not None and s.submitted_on is not None and s.submitted_on < cutoff:
                before += 1
        backtest = session.scalars(
            select(RuleBacktest)
            .where(RuleBacktest.rule_id == rule_id)
            .order_by(RuleBacktest.ran_at)
        ).first()
        out.append(
            {
                "rule_id": rule_id,
                "category": review["category"],
                "answered": len(answered),
                "before_cutoff": before,
                "backtest_flagged": backtest.flagged if backtest else None,
                "backtest_fp": backtest.fp_rate if backtest else None,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--client", type=uuid.UUID, default=DEFAULT_CLIENT)
    args = parser.parse_args(argv)
    client_id: uuid.UUID = args.client
    now = datetime.now(UTC)
    today = now.date()

    with session_scope() as session:
        pairs, entries = seed_reference(session, today=today)
    _say(f"reference: {pairs} NCCI pairs, {entries} new registry entries")

    with session_scope() as session:
        run = work_denials.classify_and_route(session, client_id=client_id, now=now)
    _say(
        f"classified {run.classification.classified} denied lines "
        f"(unmapped {len(run.classification.unmapped)}); "
        f"feedback {dict(run.feedback.outcomes)}"
    )

    with session_scope() as session:
        claims = first_submissions(session, client_id)
        dated = sorted(s.submitted_on for s in claims.values() if s.submitted_on and s.outcome)
        cutoff = dated[len(dated) // 2]
        reviewed = review_candidates(session, client_id, claims, cutoff)
        promoted = [r for r in reviewed if r["promoted"]]
        earlier = [r for r in promoted if r["already"]]
        # A ruleset is a snapshot of the live rules: publish one only when
        # this run changed them, or when no run has promoted anything yet.
        if len(earlier) < len(promoted) or not earlier:
            publish_ruleset(session, published_by=REVIEWER)
    _say(
        f"\ncutoff {cutoff.isoformat()}: {len(reviewed)} candidates reviewed, "
        f"{len(promoted)} promoted to ADVISORY"
        + (f" ({len(earlier)} by an earlier run; nothing republished)" if earlier else "")
    )
    for review in reviewed:
        rate = review["fp_rate_before_cutoff"]
        shown = f"{rate:.2%}" if isinstance(rate, Decimal) else "n/a"
        _say(
            f"  {review['rule_id']:<48} {review['category']!s:<25} "
            f"flagged {review['flagged_before_cutoff']!s:>4}  fp {shown:>7}  "
            f"{'PROMOTED' if review['promoted'] else 'held'}"
        )

    with session_scope() as session:
        claims = first_submissions(session, client_id)
        baseline = replay(session, client_id, claims, since=None, today=today, active=False)
        print_tally("BEFORE: no rule, NCCI edit or registry entry (all claims)", baseline)
        in_sample = replay(session, client_id, claims, since=None, today=today, active=True)
        print_tally("AFTER, IN-SAMPLE: all claims (the rules saw these denials)", in_sample)
        held_out = replay(session, client_id, claims, since=cutoff, today=today, active=True)
        print_tally(
            f"AFTER, OUT-OF-TIME: rules judged before {cutoff.isoformat()}, "
            "measured on claims from then on",
            held_out,
        )
        closing = loop_closing(session, claims, reviewed, cutoff)

    _say("\nFEEDBACK LOOP CLOSING: promoted candidates, out of time")
    _say("  rule                                          answered  before  later: caught  false")
    closed = 0
    for c in closing:
        rule_id = str(c["rule_id"])
        hits, false = held_out.rule_hits[rule_id], held_out.rule_false[rule_id]
        closed += hits > 0
        _say(f"  {rule_id:<46}{c['answered']!s:>8}{c['before_cutoff']!s:>8}{hits:>16}{false:>7}")
    _say(f"  {closed} learned pattern(s) caught a later recurrence")

    passed = closed > 0
    _say(f"\nGATE M4: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
