"""
RCM seed data generator.

Produces, from one run:
  out/x12/837/*.txt          real 837P claim files
  out/x12/835/*.txt          real 835 remittance files
  out/x12/999/*.txt          acknowledgements
  out/csv/claim_lines.csv    Dataset 1 - one row per service line
  out/csv/adjustments.csv    one row per CAS reason, never collapsed
  out/csv/claim_headers.csv  claim-level facts
  out/csv/encounters.csv     Dataset 2 - one row per encounter, with note text
  out/csv/payer_rules.csv    Dataset 3 - copied from reference/
  out/manifest.json          what was generated, with counts

WHAT THIS IS FOR
  Testing a pipeline end to end. Parser development. Demos. Load testing.

WHAT THIS IS NOT FOR
  Training a production denial model. The denials here contain only the
  patterns coded into adjudicate() below. A model trained on this learns those
  patterns, which is circular. Real payer behaviour comes from real 835s.
"""

from __future__ import annotations

import csv
import pathlib
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# The package now lives inside the repository, so these resolve relative to
# this file rather than to a standalone checkout's root.
ROOT = pathlib.Path(__file__).resolve().parent
REF = ROOT / "reference"
#: Written under data/, which .gitignore excludes -- a generated corpus is
#: rebuildable from config and seed, and committing it would put a hundred
#: megabytes of fixtures into every clone.
OUT = ROOT.parent.parent / "data" / "synthetic"


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def load(name):
    with (REF / name).open() as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------
class Reference:
    def __init__(self):
        self.payers = load("payers.csv")
        self.procedures = load("procedures.csv")
        self.diagnoses = load("diagnoses.csv")
        self.modifiers = load("modifiers.csv")
        self.pos = load("pos.csv")
        self.carc = {r["carc_code"]: r for r in load("carc.csv")}
        self.rarc = load("rarc.csv")
        self.ncci = load("ncci_edits.csv")
        self.rules = load("payer_rules.csv")

        self.rule_idx = {(r["payer_id"], r["cpt_hcpcs"]): r for r in self.rules}
        self.filing = {p["payer_id"]: int(p["timely_filing_days"]) for p in self.payers}
        self.ncci_pairs = {(e["column1_code"], e["column2_code"]): e for e in self.ncci}
        self.proc_by_specialty: dict[str, list] = {}
        for p in self.procedures:
            self.proc_by_specialty.setdefault(p["specialty"], []).append(p)
        self.dx_by_specialty: dict[str, list] = {}
        for d in self.diagnoses:
            self.dx_by_specialty.setdefault(d["specialty"], []).append(d)

    def carc_for_category(self, category):
        return [c for c in self.carc.values() if c["denial_category"] == category]

    def rarc_for(self, carc_code):
        return [r["rarc_code"] for r in self.rarc if r["pairs_with_carc"] == carc_code]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
FIRST = [
    "Rosa",
    "Marcus",
    "Priya",
    "James",
    "Aisha",
    "Daniel",
    "Lucia",
    "Kenji",
    "Fatima",
    "Thomas",
    "Nadia",
    "Peter",
    "Grace",
    "Omar",
    "Elena",
    "Samuel",
    "Ines",
    "Victor",
    "Mei",
    "Hassan",
    "Clara",
    "Andre",
    "Yusuf",
    "Beatriz",
]
LAST = [
    "Delgado",
    "Whitfield",
    "Nakamura",
    "Osei",
    "Ferreira",
    "Lindqvist",
    "Okafor",
    "Варга",
    "Halvorsen",
    "Mbeki",
    "Castellanos",
    "Petrov",
    "Sundaram",
    "Bianchi",
    "Novak",
    "Aliyev",
    "Haddad",
    "Kowalski",
]
STREETS = ["Maple Ave", "Cedar St", "Birch Ln", "Elm Rd", "Sycamore Dr", "Pine Way"]
CITIES = [
    ("Austin", "TX", "78701"),
    ("Dallas", "TX", "75201"),
    ("Houston", "TX", "77002"),
    ("San Antonio", "TX", "78205"),
]

SPECIALTIES = {
    "Family Medicine": ("207Q00000X", 0.30),
    "Gastroenterology": ("207RG0100X", 0.14),
    "Cardiology": ("207RC0000X", 0.13),
    "Orthopedics": ("207X00000X", 0.13),
    "Behavioral Health": ("101YM0800X", 0.10),
    "Radiology": ("2085R0202X", 0.08),
    "Laboratory": ("291U00000X", 0.07),
    "Physical Therapy": ("225100000X", 0.05),
}


@dataclass
class Provider:
    npi: str
    last: str
    first: str
    specialty: str
    taxonomy: str


@dataclass
class Practice:
    practice_id: str
    name: str
    npi: str
    tin: str
    addr: str
    city: str
    state: str
    zip: str
    providers: list = field(default_factory=list)


@dataclass
class Patient:
    patient_id: str
    last: str
    first: str
    dob: date
    sex: str
    addr: str
    city: str
    state: str
    zip: str
    payer_id: str
    member_id: str
    coverage_start: date
    coverage_end: date | None
    secondary_payer_id: str | None


def luhn_npi(rng) -> str:
    """NPIs are 10 digits with a Luhn check digit prefixed by 80840."""
    base = "".join(str(rng.randint(0, 9)) for _ in range(9))
    payload = "80840" + base
    total, dbl = 0, True
    for ch in reversed(payload):
        d = int(ch)
        if dbl:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        dbl = not dbl
    return base + str((10 - total % 10) % 10)


def member_id_for(payer, rng) -> str:
    pid = payer["payer_id"]
    D = lambda n: "".join(str(rng.randint(0, 9)) for _ in range(n))
    A = lambda n: "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(n))
    return {
        "60054": "W" + D(9),
        "87726": D(9),
        "84980": D(9),
        "00590": A(3) + D(9),
        "62308": "U" + D(8),
        "00431": f"{D(1)}{A(1)}{D(2)}{A(1)}{D(2)}{A(2)}{D(2)}",
        "77032": "M" + D(10),
        "11315": "WC" + D(8),
    }.get(pid, D(9))


# ---------------------------------------------------------------------------
# Clinical note templates - Dataset 2
# ---------------------------------------------------------------------------
NOTE_TEMPLATE = """CHIEF COMPLAINT
{cc}

HISTORY OF PRESENT ILLNESS
{age} year old {sex_word} presenting with {cc_lower}. Symptoms reported for
approximately {dur} prior to presentation. {hpi_extra}

REVIEW OF SYSTEMS
{ros}

PHYSICAL EXAMINATION
{exam}

ASSESSMENT
{assessment}

PLAN
{plan}
"""

EXAM_DETAILED = (
    "Detailed history obtained. Comprehensive examination performed. "
    "Moderate complexity medical decision making documented."
)
EXAM_BRIEF = "Focused history and examination. Straightforward medical decision making."


def build_note(rng, patient, age, dx_rows, proc_rows, copy_forward: bool):
    cc = dx_rows[0]["short_label"]
    exam = EXAM_DETAILED if copy_forward or rng.random() < 0.4 else EXAM_BRIEF
    plan_lines = []
    for p in proc_rows:
        plan_lines.append(f"- {p['internal_label'].replace('_', ' ')} performed/ordered.")
    for d in dx_rows:
        plan_lines.append(f"- {d['short_label']}: continue management, reassess.")
    return NOTE_TEMPLATE.format(
        cc=cc.capitalize(),
        age=age,
        sex_word="female" if patient.sex == "F" else "male",
        cc_lower=cc,
        dur=rng.choice(["two days", "one week", "two weeks", "one month", "three months"]),
        hpi_extra=rng.choice(
            [
                "No fever, chills or acute distress reported.",
                "Patient reports the symptoms are unchanged since last visit.",
                "Symptoms have progressively worsened despite conservative measures.",
            ]
        ),
        ros=rng.choice(
            [
                "Constitutional, cardiovascular and respiratory systems reviewed "
                "and negative except as noted.",
                "Ten point review of systems performed, negative except as documented above.",
            ]
        ),
        exam=exam,
        assessment="\n".join(
            f"{i + 1}. {d['short_label']} ({d['icd10_code']})" for i, d in enumerate(dx_rows)
        ),
        plan="\n".join(plan_lines),
    )


# ---------------------------------------------------------------------------
# The fake payer adjudicator
# ---------------------------------------------------------------------------
class Adjudicator:
    """
    Applies deterministic rules first, then a calibrated random denial layer.

    Deterministic causes produce the denial patterns your scrubbing rules should
    be able to learn and prevent. The random layer exists so the data is not
    perfectly separable - a model that scores 1.00 on this is telling you the
    generator is too clean, not that the model is good.
    """

    def __init__(self, ref: Reference, rng: random.Random, cfg: dict):
        self.ref = ref
        self.rng = rng
        self.cfg = cfg

    def adjudicate(self, claim: dict, payer: dict, history: dict) -> dict:
        allowed_pct = float(payer["allowed_pct_of_charge"])
        propensity = float(payer["denial_propensity"])
        base_rate = self.cfg["base_denial_rate"]

        result_lines = []
        for line in claim["lines"]:
            denials = self._deterministic(claim, line, payer, history)

            if not denials and self.rng.random() < base_rate * propensity:
                denials = [self._random_denial()]

            charge = money(line["charge"])
            if denials:
                allowed = money(0)
                paid = money(0)
                adjustments = []
                for carc_code in denials:
                    row = self.ref.carc[carc_code]
                    adjustments.append(
                        {
                            "group": row["usual_group"],
                            "carc": carc_code,
                            "amount": charge if len(denials) == 1 else money(charge / len(denials)),
                        }
                    )
                rarcs = self.ref.rarc_for(denials[0])
                status = "4"  # denied
            else:
                allowed = money(charge * Decimal(str(allowed_pct)))
                contractual = money(charge - allowed)
                # patient responsibility: deductible/coinsurance on commercial
                if (
                    payer["plan_type"] in ("Commercial", "Medicare Advantage")
                    and self.rng.random() < 0.45
                ):
                    pr = money(allowed * Decimal(str(self.rng.choice([0.10, 0.20, 0.20, 0.30]))))
                else:
                    pr = money(0)
                paid = money(allowed - pr)
                adjustments = [{"group": "CO", "carc": "45", "amount": contractual}]
                if pr > 0:
                    adjustments.append(
                        {"group": "PR", "carc": self.rng.choice(["1", "2", "3"]), "amount": pr}
                    )
                rarcs = []
                status = "1" if pr == 0 else "2"

            result_lines.append(
                {
                    **line,
                    "allowed": allowed,
                    "paid": paid,
                    "adjustments": adjustments,
                    "rarcs": rarcs,
                    "denied": bool(denials),
                    "carc_codes": denials,
                }
            )

        total_paid = money(sum(l["paid"] for l in result_lines))
        total_pr = money(
            sum(a["amount"] for l in result_lines for a in l["adjustments"] if a["group"] == "PR")
        )
        any_paid = any(l["paid"] > 0 for l in result_lines)
        all_denied = all(l["denied"] for l in result_lines)
        claim_status = "4" if all_denied else ("1" if not total_pr else "2")

        return {
            "lines": result_lines,
            "total_paid": total_paid,
            "patient_responsibility": total_pr,
            "claim_status_code": claim_status,
            "denied": all_denied,
        }

    # -- deterministic causes -------------------------------------------------
    def _deterministic(self, claim, line, payer, history) -> list[str]:
        out = []

        # timely filing
        if claim["days_dos_to_submission"] > self.ref.filing[payer["payer_id"]]:
            out.append("29")

        # eligibility: coverage terminated before DOS
        if claim["eligibility_result"] == "inactive":
            out.append("27")
        elif claim["eligibility_result"] == "not_found":
            out.append("31")

        # prior authorisation
        rule = self.ref.rule_idx.get((payer["payer_id"], line["cpt"]))
        if rule and rule["prior_auth_required"] == "Y" and not claim.get("prior_auth_number"):
            out.append("197")

        # NCCI bundling: is this line a column-2 code against another line?
        others = {l["cpt"] for l in claim["lines"] if l is not line}
        for other in others:
            edit = self.ref.ncci_pairs.get((other, line["cpt"]))
            if edit:
                bypass_ok = edit["modifier_bypass_allowed"] == "1"
                has_bypass = any(
                    m in ("59", "XE", "XS", "XP", "XU", "25") for m in line["modifiers"]
                )
                if not (bypass_ok and has_bypass):
                    out.append("97")
                    break

        # E/M billed with same-day procedure and no modifier 25
        if line["is_em"] and claim["has_procedure_same_day"] and "25" not in line["modifiers"]:
            out.append("4")

        # duplicate of a line already billed for this patient/cpt/dos
        key = (claim["patient_id"], line["cpt"], claim["dos"])
        if key in history["billed"]:
            out.append("18")
        history["billed"].add(key)

        # invalid member id format for this payer
        if not claim["member_id_valid"]:
            out.append("140")

        return out[:2]  # a real payer rarely stacks more than two reasons

    def _random_denial(self) -> str:
        pool = self.cfg["random_denial_carc_weights"]
        codes = list(pool.keys())
        weights = list(pool.values())
        return self.rng.choices(codes, weights=weights, k=1)[0]
