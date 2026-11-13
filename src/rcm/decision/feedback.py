"""The denial feedback loop (M4.9, SRS 8.4, ADR-0013).

FR-RULES-07 / BR-M7-01: every preventable denial produces either a candidate
rule or a recorded decision not to create one. SRS 8.4, for each classified
denial not yet answered:

- not preventable: logged as environmental, no rule;
- a live rule flagged the line before submission and the payer denied it
  anyway: a DEFECT -- the rule failed, and a person investigates;
- a live rule flags it now (added since): already covered;
- otherwise: a candidate rule is drafted for the pattern -- one payer, one
  procedure, one category -- and backtested. The loop never promotes it:
  shadow, advisory and blocking are a person's steps (FR-RULES-06).

Categories no claim field can catch (timely filing, duplicates, provider
enrolment) are answered with the reason, recorded. So is an authorisation
denial on a claim that carried an authorisation (EC-60): the payer is the one
to answer for it, on appeal. Every answer is a row in
`denial_feedback` and a `decision_record` (PD-1), so no preventable denial
can recur unexplained -- BR-M7-01's [TBD-44] defect cannot arise.

Candidates are drafted as ADVISORY at most, flag only, never auto-correct,
and never add a modifier (PD-16).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from rcm.decision import ncci_store, registry
from rcm.decision.denial_models import DenialClassification
from rcm.decision.denials import (
    CATEGORY_RULES,
    TAXONOMY_VERSION,
    DenialCategory,
    Preventability,
)
from rcm.decision.feedback_models import DenialFeedback
from rcm.decision.rules import lifecycle
from rcm.decision.rules.backtest import run_backtest
from rcm.decision.rules.definition import Layer, RuleStatus
from rcm.decision.rules.evaluate import ClaimFacts, Mode, Outcome, evaluate
from rcm.decision.rules.models import RuleBacktest, RuleVersion
from rcm.decision.rules.scrub import claim_facts
from rcm.decision.rules.store import LoadedRuleset, create_rule_version, load_ruleset
from rcm.domain.claim import Claim, ClaimLine
from rcm.domain.remittance import ClaimPayment, RemittanceServiceLine
from rcm.platform.audit import DecisionRecord, DecisionType, record_decision
from rcm.platform.errors import ResourceNotFoundError
from rcm.platform.logging import get_logger

__all__ = [
    "DECIDED_BY",
    "LOGIC_VERSION",
    "DenialCategory",
    "Draft",
    "FailedRule",
    "FeedbackOutcome",
    "FeedbackReport",
    "NoRule",
    "Untraced",
    "candidate_for",
    "rule_id_for",
    "run_feedback",
]

log = get_logger(__name__)

#: v2: an authorisation denial on a claim that carried one drafts no rule (EC-60).
LOGIC_VERSION: Final = "feedback-v2"
DECIDED_BY: Final = "rules_engine:denial_feedback"

C = DenialCategory


class FeedbackOutcome(StrEnum):
    CANDIDATE_DRAFTED = "CANDIDATE_DRAFTED"
    CANDIDATE_EXISTS = "CANDIDATE_EXISTS"
    RULE_FAILED = "RULE_FAILED"
    ALREADY_COVERED = "ALREADY_COVERED"
    NO_RULE = "NO_RULE"
    NOT_PREVENTABLE = "NOT_PREVENTABLE"
    NO_CLAIM = "NO_CLAIM"
    LINE_UNMATCHED = "LINE_UNMATCHED"


@dataclass(frozen=True, slots=True)
class _Template:
    code: str
    layer: Layer
    route_to: str
    #: Fixed text: nothing from the claim is spliced in (PD-13).
    message: str
    extra: tuple[dict[str, object], ...] = ()


#: The categories a claim rule can catch, and the rule each drafts. The
#: procedure and payer are the scope; anything sharper is a person's
#: refinement, after the backtest shows what the broad rule costs.
_TEMPLATES: Final[dict[DenialCategory, _Template]] = {
    C.REGISTRATION_ELIGIBILITY: _Template(
        "ELIG",
        Layer.DEMOGRAPHIC,
        "ELIGIBILITY_PROBLEM",
        "This payer has denied this procedure on registration or eligibility. "
        "Verify coverage and the patient's details before submitting.",
    ),
    C.COORDINATION_OF_BENEFITS: _Template(
        "COB",
        Layer.DEMOGRAPHIC,
        "ELIGIBILITY_PROBLEM",
        "This payer has denied this procedure on coordination of benefits. "
        "Confirm which payer is primary before submitting.",
    ),
    C.AUTHORISATION: _Template(
        "AUTH",
        Layer.PAYER_SPECIFIC,
        "AUTH_PROBLEM",
        "This payer has denied this procedure for want of prior authorisation. "
        "Confirm an authorisation is on the claim before submitting.",
        ({"field": "claim.has_prior_auth", "op": "eq", "value": False},),
    ),
    C.CODING: _Template(
        "CODE",
        Layer.CLINICAL,
        "SCRUB_EXCEPTION",
        "This payer has denied this procedure on coding. Review the procedure, "
        "its modifiers and its diagnosis pairing before submitting.",
    ),
    C.MEDICAL_NECESSITY: _Template(
        "MEDNEC",
        Layer.CLINICAL,
        "SCRUB_EXCEPTION",
        "This payer has denied this procedure as not medically necessary. Confirm "
        "the diagnoses support it under the payer's coverage policy.",
    ),
}

#: Preventable, but not by a rule on the claim's contents. FR-RULES-07's
#: "recorded decision not to create one", with the reason.
_NO_RULE: Final[dict[DenialCategory, str]] = {
    C.TIMELY_FILING: (
        "Timely filing turns on when a claim is sent, not what it says; the "
        "filing-deadline priority (PD-15) prevents it, not a claim rule"
    ),
    C.DUPLICATE: (
        "A duplicate is a claim already sent; the scrubber reads one claim and "
        "has no field for its history"
    ),
    C.PROVIDER_ENROLMENT: (
        "Enrolment is a fact about the provider at the payer; the scrubber has "
        "no provider field to test"
    ),
}

#: EC-60: the payer denied for want of an authorisation the claim carried.
#: The candidate for this category asks whether one is on the claim, and one
#: was, so it would not have flagged this claim and cannot stop the next.
_AUTH_ON_CLAIM: Final = (
    "An authorisation was on the claim and the payer denied for want of one; no rule "
    "on the claim's contents catches that. Appeal with the authorisation as evidence"
)


@dataclass(frozen=True, slots=True)
class Draft:
    document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NoRule:
    reason: str


def _token(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", text.upper())


def rule_id_for(category: DenialCategory, *, payer_id: str, cpt: str) -> str:
    """One pattern, one rule id. The digest keeps two patterns apart that
    read alike once cleaned into a rule id."""
    template = _TEMPLATES.get(category)
    if template is None:
        raise ValueError(f"{category} has no candidate rule")
    digest = hashlib.sha256(f"{category}|{payer_id}|{cpt}".encode()).hexdigest()[:8].upper()
    return f"FB_{template.code}_{_token(payer_id)[:40]}_{_token(cpt)[:16]}_{digest}"


def candidate_for(
    category: DenialCategory, *, payer_id: str, cpt: str, carc: str
) -> Draft | NoRule:
    """The candidate rule a denial pattern drafts, or why none can be."""
    if CATEGORY_RULES[category].preventability is Preventability.NOT_PREVENTABLE:
        raise ValueError(f"{category} is not preventable; nothing to draft")
    if category in _NO_RULE:
        return NoRule(_NO_RULE[category])
    template = _TEMPLATES[category]
    return Draft(
        {
            "rule_id": rule_id_for(category, payer_id=payer_id, cpt=cpt),
            "layer": str(template.layer),
            "severity": "ADVISORY",
            "scope": {"payer_id": [payer_id], "cpt": [cpt]},
            "condition": {
                "all": [
                    {"field": "line.procedure_code", "op": "eq", "value": cpt},
                    *template.extra,
                ]
            },
            "action": {"type": "FLAG", "message": template.message, "route_to": template.route_to},
            "provenance": {
                "source": "denial_feedback",
                "category": str(category),
                "carc": carc,
                "taxonomy_version": TAXONOMY_VERSION,
            },
        }
    )


@dataclass(frozen=True, slots=True)
class FailedRule:
    """A live rule flagged the line before submission and the payer denied
    it for the same cause. For the work layer to put before a person."""

    service_line_id: uuid.UUID
    claim_id: uuid.UUID
    rule_id: str
    rule_version: int | None


@dataclass(frozen=True, slots=True)
class Untraced:
    """A preventable denial the loop could not trace to a submitted line.
    For the work layer to put before a person: a data gap, not an answer."""

    service_line_id: uuid.UUID
    claim_payment_id: uuid.UUID
    reason: str


@dataclass(slots=True)
class FeedbackReport:
    outcomes: Counter[str] = field(default_factory=Counter)
    failed: list[FailedRule] = field(default_factory=list)
    untraced: list[Untraced] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Answer:
    outcome: FeedbackOutcome
    reason: str
    rule_id: str | None = None
    rule_version: int | None = None
    backtest_id: uuid.UUID | None = None


#: The cause an NCCI edit catches: PTP is bundling (CARC 97), MUE is units
#: (CARC 151), as the taxonomy files them.
_CHECK_CATEGORY: Final[dict[str, DenialCategory]] = {
    "NCCI_PTP": C.CODING,
    "NCCI_MUE": C.MEDICAL_NECESSITY,
    registry.PRIOR_AUTH_CHECK: C.AUTHORISATION,
}

#: Modes that act on a claim. A shadow rule is recorded, never acted on, so
#: it neither failed nor covers anything (FR-RULES-06).
_ACTING: Final = frozenset({str(Mode.BLOCKING), str(Mode.ADVISORY)})


class _Causes:
    """The denial category each rule says it catches, read once per rule.

    A rule covers a denial only when it declares that category in
    `provenance.category`: every candidate does, and an author may. A rule
    that declares none is not counted -- a coding flag on the line says
    nothing about an authorisation denial (ADR-0013).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._known: dict[tuple[str, int], DenialCategory | None] = {}

    def of(self, rule_id: str, version: int | None) -> DenialCategory | None:
        if rule_id in _CHECK_CATEGORY:
            return _CHECK_CATEGORY[rule_id]
        if version is None:
            return None
        key = (rule_id, version)
        if key not in self._known:
            row = self._session.get(RuleVersion, key)
            declared = (row.document.get("provenance") or {}).get("category") if row else None
            try:
                self._known[key] = DenialCategory(declared) if declared else None
            except ValueError:
                self._known[key] = None
        return self._known[key]


def _flagged_at_scrub(
    session: Session,
    causes: _Causes,
    *,
    claim_id: uuid.UUID,
    line_number: int,
    category: DenialCategory,
) -> tuple[str, int | None] | None:
    """A rule that acted on this line, for this cause, at the claim's last
    scrub -- and did not stop the denial. A claim never scrubbed had no rule
    run on it, so none failed."""
    record = session.scalar(
        select(DecisionRecord)
        .where(
            DecisionRecord.entity_type == "claim",
            DecisionRecord.entity_id == claim_id,
            DecisionRecord.decision_type == str(DecisionType.SCRUB),
        )
        .order_by(DecisionRecord.decided_at.desc(), DecisionRecord.decision_id.desc())
        .limit(1)
    )
    if record is None:
        return None
    # Read strictly: scrub always writes these keys, and a record without
    # them is not evidence that nothing was flagged.
    for finding in record.output["findings"]:
        if (
            finding["outcome"] == str(Outcome.FLAGGED)
            and finding["mode"] in _ACTING
            and finding["line_number"] in (line_number, None)
            and causes.of(finding["rule_id"], finding["rule_version"]) is category
        ):
            return str(finding["rule_id"]), finding["rule_version"]
    return None


def _flagged_now(
    session: Session,
    causes: _Causes,
    *,
    ruleset: LoadedRuleset | None,
    facts: ClaimFacts,
    line_number: int,
    category: DenialCategory,
) -> tuple[str, int] | None:
    """A rule or NCCI edit that would act on this line, for this cause, today.
    Read against the claim as it stands, which a resubmission may have
    changed since the denied one was sent."""
    if ruleset is None:
        return None
    ncci = ncci_store.check_for(
        session,
        ptp_release=ruleset.ncci_ptp_release,
        mue_release=ruleset.ncci_mue_release,
        codes={str(line.values["line.procedure_code"]) for line in facts.lines},
    )
    payer_registry = registry.check_for(
        session, payer_id=str(facts.claim["claim.payer_id"]), today=datetime.now(UTC).date()
    )
    for finding in evaluate(ruleset.rules, facts, checks=[ncci, payer_registry]).findings:
        if (
            finding.outcome is Outcome.FLAGGED
            and str(finding.mode) in _ACTING
            and finding.line_number in (line_number, None)
            and causes.of(finding.rule_id, finding.rule_version) is category
        ):
            return finding.rule_id, finding.rule_version
    return None


#: One loop at a time across clients: candidates are keyed by payer and
#: procedure, not by client, so two clients' runs could draft one pattern.
_LOCK: Final = "denial_feedback"


def run_feedback(session: Session, *, client_id: uuid.UUID) -> FeedbackReport:
    """Answer every classified denial of one client not yet answered.

    Idempotent: an answered denial is not read again. One run at a time, so
    two runs cannot draft the same candidate twice.
    """
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(_LOCK))))
    report = FeedbackReport()
    answered = aliased(DenialFeedback)
    rows = session.execute(
        select(
            DenialClassification,
            RemittanceServiceLine.line_number,
            RemittanceServiceLine.procedure_code,
            Claim.claim_id,
            Claim.payer_id,
        )
        .join(
            RemittanceServiceLine,
            and_(
                RemittanceServiceLine.service_line_id == DenialClassification.service_line_id,
                RemittanceServiceLine.client_id == client_id,
            ),
        )
        .join(
            ClaimPayment,
            and_(
                ClaimPayment.claim_payment_id == DenialClassification.claim_payment_id,
                ClaimPayment.client_id == client_id,
            ),
        )
        # CLM01, the payer's echo of the claim number sent.
        .outerjoin(
            Claim,
            and_(Claim.client_id == client_id, Claim.submitter_claim_id == ClaimPayment.claim_id),
        )
        .where(
            DenialClassification.client_id == client_id,
            DenialClassification.taxonomy_version == TAXONOMY_VERSION,
            ~select(answered.service_line_id)
            .where(
                answered.service_line_id == DenialClassification.service_line_id,
                answered.taxonomy_version == DenialClassification.taxonomy_version,
            )
            .exists(),
        )
        .order_by(DenialClassification.claim_payment_id, RemittanceServiceLine.line_number)
    ).all()
    if not rows:
        return report

    try:
        ruleset: LoadedRuleset | None = load_ruleset(session)
    except ResourceNotFoundError:
        # Nothing published, so nothing covers anything: drafting is the
        # safe direction. Said here and in each decision record's input.
        log.warning("denials.feedback.no_ruleset")
        ruleset = None
    loop = _Loop(session, client_id=client_id, ruleset=ruleset)
    by_payment: dict[uuid.UUID, list[dict[str, object]]] = defaultdict(list)

    for classified, line_number, cpt, claim_id, payer_id in rows:
        category = DenialCategory(classified.category)
        answer = loop.answer(
            category=category,
            carc=classified.carc_code,
            remit_line=line_number,
            cpt=cpt,
            claim_id=claim_id,
            payer_id=payer_id,
        )
        session.add(
            DenialFeedback(
                service_line_id=classified.service_line_id,
                taxonomy_version=classified.taxonomy_version,
                client_id=client_id,
                claim_payment_id=classified.claim_payment_id,
                claim_id=claim_id,
                outcome=str(answer.outcome),
                rule_id=answer.rule_id,
                rule_version=answer.rule_version,
                reason=answer.reason,
            )
        )
        report.outcomes[str(answer.outcome)] += 1
        if answer.outcome is FeedbackOutcome.RULE_FAILED:
            if claim_id is None or answer.rule_id is None:
                raise AssertionError("a failed rule is found only on a traced claim")
            report.failed.append(
                FailedRule(
                    service_line_id=classified.service_line_id,
                    claim_id=claim_id,
                    rule_id=answer.rule_id,
                    rule_version=answer.rule_version,
                )
            )
        elif answer.outcome in (FeedbackOutcome.NO_CLAIM, FeedbackOutcome.LINE_UNMATCHED):
            report.untraced.append(
                Untraced(
                    service_line_id=classified.service_line_id,
                    claim_payment_id=classified.claim_payment_id,
                    reason=answer.reason,
                )
            )
        by_payment[classified.claim_payment_id].append(
            {
                "line_number": line_number,
                "category": str(category),
                "preventability": classified.preventability,
                "outcome": str(answer.outcome),
                "rule_id": answer.rule_id,
                "rule_version": answer.rule_version,
                "backtest_id": str(answer.backtest_id) if answer.backtest_id else None,
                "reason": answer.reason,
            }
        )

    for claim_payment_id, lines in by_payment.items():
        record_decision(
            session,
            entity_type="claim_payment",
            entity_id=claim_payment_id,
            decision_type=DecisionType.DENIAL_FEEDBACK,
            decided_by=DECIDED_BY,
            logic_version=LOGIC_VERSION,
            input_snapshot={
                "taxonomy_version": TAXONOMY_VERSION,
                "ruleset_version": ruleset.version if ruleset else None,
                "lines": [
                    {k: line[k] for k in ("line_number", "category", "preventability")}
                    for line in lines
                ],
            },
            output={"lines": lines},
        )
    session.flush()
    log.info(
        "denials.feedback",
        **dict(report.outcomes),
        failed=len(report.failed),
        untraced=len(report.untraced),
    )
    return report


class _Loop:
    """One run's answers, with what it has already read."""

    def __init__(
        self, session: Session, *, client_id: uuid.UUID, ruleset: LoadedRuleset | None
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._ruleset = ruleset
        self._causes = _Causes(session)
        self._facts: dict[uuid.UUID, ClaimFacts] = {}

    def answer(
        self,
        *,
        category: DenialCategory,
        carc: str,
        remit_line: int,
        cpt: str,
        claim_id: uuid.UUID | None,
        payer_id: str | None,
    ) -> _Answer:
        rule = CATEGORY_RULES[category]
        if rule.preventability is Preventability.NOT_PREVENTABLE:
            return _Answer(
                FeedbackOutcome.NOT_PREVENTABLE,
                f"{category} is not preventable ({rule.preventable_at}); logged, no rule",
            )
        if category not in _TEMPLATES:
            return _Answer(FeedbackOutcome.NO_RULE, _NO_RULE[category])
        if claim_id is None or payer_id is None:
            return _Answer(
                FeedbackOutcome.NO_CLAIM,
                "No submitted claim is on file for this 835 claim; a person traces it",
            )
        line_number = self._line(claim_id=claim_id, remit_line=remit_line, cpt=cpt)
        if isinstance(line_number, _Answer):
            return line_number

        failed = _flagged_at_scrub(
            self._session,
            self._causes,
            claim_id=claim_id,
            line_number=line_number,
            category=category,
        )
        if failed is not None:
            name, version = failed
            return _Answer(
                FeedbackOutcome.RULE_FAILED,
                f"{name} flagged this line before submission and the payer denied it "
                "for the same cause: the rule did not stop it",
                rule_id=name,
                rule_version=version,
            )
        if claim_id not in self._facts:
            self._facts[claim_id] = claim_facts(
                self._session, claim_id=claim_id, client_id=self._client_id
            )
        covered = _flagged_now(
            self._session,
            self._causes,
            ruleset=self._ruleset,
            facts=self._facts[claim_id],
            line_number=line_number,
            category=category,
        )
        if covered is not None:
            name, version = covered
            return _Answer(
                FeedbackOutcome.ALREADY_COVERED,
                f"{name} acts on this line now; the denial predates it",
                rule_id=name,
                rule_version=version,
            )
        if (
            category is C.AUTHORISATION
            and self._facts[claim_id].claim["claim.has_prior_auth"] is True
        ):
            return _Answer(FeedbackOutcome.NO_RULE, _AUTH_ON_CLAIM)
        return self._candidate(category=category, payer_id=payer_id, cpt=cpt, carc=carc)

    def _line(self, *, claim_id: uuid.UUID, remit_line: int, cpt: str) -> int | _Answer:
        """The submitted line the denied one answers: same position and
        procedure, or the only line with that procedure. Never a guess."""
        lines = self._session.execute(
            select(ClaimLine.line_number, ClaimLine.procedure_code).where(
                ClaimLine.client_id == self._client_id, ClaimLine.claim_id == claim_id
            )
        ).all()
        if any(n == remit_line and code == cpt for n, code in lines):
            return remit_line
        same = [n for n, code in lines if code == cpt]
        if len(same) == 1:
            return same[0]
        return _Answer(
            FeedbackOutcome.LINE_UNMATCHED,
            f"The denied procedure {cpt} is not on the submitted claim; a person traces it"
            if not same
            else f"The denied procedure {cpt} is on {len(same)} submitted lines and the 835 "
            "line matches none by position; a person traces it",
        )

    def _candidate(
        self, *, category: DenialCategory, payer_id: str, cpt: str, carc: str
    ) -> _Answer:
        drafted = candidate_for(category, payer_id=payer_id, cpt=cpt, carc=carc)
        if not isinstance(drafted, Draft):
            raise AssertionError("a template category always drafts")
        rule_id = str(drafted.document["rule_id"])
        latest = self._session.scalar(
            select(RuleVersion)
            .where(RuleVersion.rule_id == rule_id)
            .order_by(RuleVersion.version.desc())
            .limit(1)
        )
        if latest is not None and latest.status != str(RuleStatus.DEPRECATED):
            backtest = self._backtest_once(rule_id, latest.version)
            return _Answer(
                FeedbackOutcome.CANDIDATE_EXISTS,
                f"Joins candidate {rule_id}, drafted from an earlier denial of this pattern",
                rule_id=rule_id,
                rule_version=latest.version,
                backtest_id=backtest,
            )
        # New, or the pattern's rule was retired and the denial came back.
        version = create_rule_version(
            self._session, document=drafted.document, created_by=DECIDED_BY
        )
        lifecycle.change_status(
            self._session,
            rule_id=rule_id,
            version=version,
            to=RuleStatus.BACKTESTING,
            changed_by=DECIDED_BY,
            reason="Drafted from a preventable denial (M4.9); backtested before any person sees it",
        )
        backtest = self._backtest_once(rule_id, version)
        retired = "" if latest is None else f" (version {latest.version} was retired)"
        return _Answer(
            FeedbackOutcome.CANDIDATE_DRAFTED,
            f"Drafted {rule_id}{retired} and backtested it; a person decides whether it "
            "goes further",
            rule_id=rule_id,
            rule_version=version,
            backtest_id=backtest,
        )

    def _backtest_once(self, rule_id: str, version: int) -> uuid.UUID | None:
        """Backtest the candidate on this client's history, once. A candidate
        drafted from another client's denial has not seen this one's."""
        seen = self._session.scalar(
            select(RuleBacktest.backtest_id)
            .where(
                RuleBacktest.rule_id == rule_id,
                RuleBacktest.version == version,
                RuleBacktest.client_id == self._client_id,
            )
            .limit(1)
        )
        if seen is not None:
            return None
        return run_backtest(
            self._session,
            rule_id=rule_id,
            version=version,
            client_id=self._client_id,
            ran_by=DECIDED_BY,
        ).backtest_id
