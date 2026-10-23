#!/usr/bin/env python3
"""
Independent validating parser for the generated X12.

This is deliberately written WITHOUT importing the writer, so it is a real
check rather than a round-trip of the same assumptions. It is also a useful
starting reference for your own 835 parser (SRS M1.2), particularly the CAS
unrolling in parse_835.

Run:  python3 tests/validate.py
Exit code 0 = all checks pass.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from decimal import Decimal

# Read the corpus from wherever the generator writes it, rather than keeping a
# second copy of the path that can drift out of step with it.
from .generate import OUT

FAIL: list[str] = []
WARN: list[str] = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
    return cond


def segments(text: str):
    """Split X12 into segments. Must work with or without line breaks."""
    for raw in text.replace("\r", "").split("~"):
        s = raw.strip("\n").strip()
        if s:
            yield s.split("*")


# ---------------------------------------------------------------------------
def parse_envelope(segs):
    ids = [s[0] for s in segs]
    check(ids[0] == "ISA", "file must start with ISA")
    check(ids[-1] == "IEA", "file must end with IEA")
    check("GS" in ids and "GE" in ids, "missing GS/GE")
    check("ST" in ids and "SE" in ids, "missing ST/SE")
    st_i = ids.index("ST")
    se_i = ids.index("SE")
    declared = int(segs[se_i][1])
    actual = se_i - st_i + 1
    check(declared == actual, f"SE01 segment count {declared} != actual {actual}")
    return segs[st_i][1]


def parse_837(text: str) -> dict:
    segs = list(segments(text))
    txn = parse_envelope(segs)
    check(txn == "837", f"expected 837, got {txn}")
    claims, cur = [], None
    for s in segs:
        tag = s[0]
        if tag == "CLM":
            cur = {"claim_id": s[1], "total_charge": Decimal(s[2]), "lines": []}
            claims.append(cur)
        elif tag == "HI" and cur is not None:
            cur["dx"] = [e.split(":")[1] for e in s[1:] if ":" in e]
        elif tag == "SV1" and cur is not None:
            comp = s[1].split(":")
            cur["lines"].append(
                {
                    "qualifier": comp[0],
                    "cpt": comp[1],
                    "modifiers": comp[2:],
                    "charge": Decimal(s[2]),
                    "units": s[4],
                    "dx_pointers": s[7].split(":") if len(s) > 7 and s[7] else [],
                }
            )
    for c in claims:
        check(len(c["lines"]) > 0, f"{c['claim_id']} has no service lines")
        check(c.get("dx"), f"{c['claim_id']} has no diagnosis codes")
        line_sum = sum(l["charge"] for l in c["lines"])
        check(
            abs(line_sum - c["total_charge"]) <= Decimal("0.02"),
            f"{c['claim_id']} CLM02 {c['total_charge']} != line sum {line_sum}",
        )
        for l in c["lines"]:
            check(l["qualifier"] == "HC", f"{c['claim_id']} bad SV1 qualifier")
            check(l["dx_pointers"], f"{c['claim_id']} line has no dx pointer")
    return {"claims": claims}


def parse_835(text: str) -> dict:
    """
    Note the grain: this returns one row per claim per line per CAS reason.
    Collapsing multiple CAS reasons into one row is the most common way this
    data gets destroyed (DOC-02 BR-DATA-02).
    """
    segs = list(segments(text))
    txn = parse_envelope(segs)
    check(txn == "835", f"expected 835, got {txn}")

    payment_total = None
    claims, cur, curline = [], None, None
    plb_total = Decimal("0")
    adjustments = []

    for s in segs:
        tag = s[0]
        if tag == "BPR":
            payment_total = Decimal(s[2])
        elif tag == "CLP":
            cur = {
                "claim_id": s[1],
                "status": s[2],
                "charge": Decimal(s[3]),
                "paid": Decimal(s[4]),
                "patient_resp": Decimal(s[5]) if len(s) > 5 and s[5] else Decimal("0"),
                "filing_code": s[6] if len(s) > 6 else "",
                "payer_control_number": s[7] if len(s) > 7 else "",
                "lines": [],
            }
            claims.append(cur)
            curline = None
        elif tag == "SVC" and cur is not None:
            comp = s[1].split(":")
            curline = {
                "cpt": comp[1],
                "modifiers": comp[2:],
                "charge": Decimal(s[2]),
                "paid": Decimal(s[3]),
                "units": s[5] if len(s) > 5 else "1",
                "allowed": None,
                "adjustments": [],
                "rarcs": [],
            }
            cur["lines"].append(curline)
        elif tag == "CAS":
            group = s[1]
            # elements come in triples: reason, amount, quantity
            body = s[2:]
            for i in range(0, len(body), 3):
                if i >= len(body) or not body[i]:
                    break
                amt = Decimal(body[i + 1]) if i + 1 < len(body) and body[i + 1] else Decimal("0")
                rec = {"group": group, "carc": body[i], "amount": amt}
                if curline is not None:
                    curline["adjustments"].append(rec)
                elif cur is not None:
                    cur.setdefault("claim_adjustments", []).append(rec)
                adjustments.append(rec)
        elif tag == "AMT" and curline is not None and s[1] == "B6":
            curline["allowed"] = Decimal(s[2])
        elif tag == "LQ" and curline is not None and s[1] == "HE":
            curline["rarcs"].append(s[2])
        elif tag == "PLB":
            # PLB amounts alternate: composite reason, then amount
            for i in range(3, len(s), 2):
                if i + 1 < len(s) and s[i + 1]:
                    plb_total += Decimal(s[i + 1])

    # ---- balance assertion (DOC-02 BR-M9-01) --------------------------
    claim_paid = sum(c["paid"] for c in claims)
    expected = claim_paid - plb_total
    balances = payment_total is not None and abs(expected - payment_total) <= Decimal("0.02")

    for c in claims:
        for l in c["lines"]:
            check(
                l["allowed"] is not None, f"{c['claim_id']} line {l['cpt']} missing AMT*B6 allowed"
            )
            for a in l["adjustments"]:
                check(
                    a["group"] in ("CO", "PR", "OA", "PI", "CR"),
                    f"{c['claim_id']} bad CAS group {a['group']}",
                )
            # charge = paid + all adjustments, per line
            adj_sum = sum(a["amount"] for a in l["adjustments"])
            check(
                abs(l["charge"] - (l["paid"] + adj_sum)) <= Decimal("0.02"),
                f"{c['claim_id']} line {l['cpt']} does not balance: "
                f"charge {l['charge']} vs paid {l['paid']} + adj {adj_sum}",
            )

    return {
        "claims": claims,
        "payment_total": payment_total,
        "plb_total": plb_total,
        "balances": balances,
        "adjustments": adjustments,
    }


# ---------------------------------------------------------------------------
def main():
    d837 = sorted((OUT / "x12" / "837").glob("*.txt"))
    d835 = sorted((OUT / "x12" / "835").glob("*.txt"))
    check(d837, "no 837 files found - run src/emit.py first")
    check(d835, "no 835 files found")
    if FAIL:
        print("\n".join(FAIL))
        return 1

    print(f"validating {len(d837)} 837 files and {len(d835)} 835 files\n")

    tot_claims = tot_lines = 0
    for p in d837:
        r = parse_837(p.read_text(encoding="utf-8"))
        tot_claims += len(r["claims"])
        tot_lines += sum(len(c["lines"]) for c in r["claims"])
    print(f"  837: {tot_claims} claims, {tot_lines} service lines parsed")

    groups = Counter()
    carcs = Counter()
    balanced = unbalanced = 0
    multi_cas_lines = 0
    r835_claims = r835_lines = r835_adj = 0
    reversal_files = reversal_claims = 0
    for p in d835:
        r = parse_835(p.read_text(encoding="utf-8"))
        is_test = "UNBALANCED" in p.name
        # A reversal re-states a claim in order to take its payment back, so
        # its rows describe a claim the CSVs already counted once. Folding
        # them into the tally below double-counts every reversed claim.
        is_reversal = "REVERSAL" in p.name
        if r["balances"]:
            balanced += 1
            if is_test:
                FAIL.append(f"{p.name} was supposed to be unbalanced but balances")
        else:
            unbalanced += 1
            if not is_test:
                FAIL.append(
                    f"{p.name} does not balance: "
                    f"claims {sum(c['paid'] for c in r['claims'])} "
                    f"- PLB {r['plb_total']} != BPR {r['payment_total']}"
                )
        if is_reversal:
            reversal_files += 1
            reversal_claims += len(r["claims"])
            for c in r["claims"]:
                if c["status"] != "22":
                    FAIL.append(
                        f"{p.name} is a reversal file but CLP02 is {c['status']!r}, not '22'"
                    )
                if c["paid"] >= 0:
                    FAIL.append(f"{p.name} reverses nothing: CLP04 is {c['paid']}")

        if not (is_test or is_reversal):  # these re-emit claims; exclude from tallies
            r835_claims += len(r["claims"])
            for c in r["claims"]:
                r835_lines += len(c["lines"])
                for l in c["lines"]:
                    r835_adj += len(l["adjustments"])
                    if len(l["adjustments"]) > 1:
                        multi_cas_lines += 1
        for a in r["adjustments"]:
            groups[a["group"]] += 1
            carcs[a["carc"]] += 1

    print(f"  835: {r835_claims} claims, {r835_lines} lines, {r835_adj} CAS reasons")
    print(
        f"       balanced {balanced}, unbalanced {unbalanced} "
        f"(1 unbalanced is the intentional quarantine fixture)"
    )
    print(f"       lines carrying >1 CAS reason: {multi_cas_lines}")
    print(f"       reversals: {reversal_files} files, {reversal_claims} claims (excluded above)")
    print(f"       CAS groups: {dict(groups)}")

    # ---- structural expectations -------------------------------------
    check(
        multi_cas_lines > 0, "no line has multiple CAS reasons - parser grain test would be vacuous"
    )
    check(
        "CO" in groups and "PR" in groups,
        "need both CO and PR adjustments to test group-code handling",
    )
    check(unbalanced == 1, f"expected exactly 1 unbalanced file, got {unbalanced}")
    # BR-M9-04 needs something to test against. A corpus with no reversals
    # makes every reversal test a fixture test, which is how the gap this
    # check now closes went unnoticed: the generator's own docstring claimed
    # it produced reversals and it never had.
    check(reversal_files > 0, "no reversal files - BR-M9-04 would be untested at volume")
    check(
        reversal_files == reversal_claims,
        f"expected one claim per reversal file, got {reversal_claims} in {reversal_files}",
    )

    # ---- CSV mart cross-checks ---------------------------------------
    lines = list(csv.DictReader((OUT / "csv" / "claim_lines.csv").open(encoding="utf-8")))
    adjr = list(csv.DictReader((OUT / "csv" / "adjustments.csv").open(encoding="utf-8")))
    check(len(lines) == r835_lines, f"CSV lines {len(lines)} != 835 lines {r835_lines}")
    check(len(adjr) == r835_adj, f"CSV adjustment rows {len(adjr)} != 835 CAS reasons {r835_adj}")

    keys = Counter((r["claim_id"], r["line_number"]) for r in lines)
    check(max(keys.values()) == 1, "claim_id+line_number is not unique - grain is broken")

    chains = [r for r in lines if r["original_claim_id"]]
    check(chains, "no resubmission chains present")

    # leakage guard: label columns must not be silently usable as features
    label_cols = {
        "line_allowed_amount",
        "line_paid_amount",
        "is_denied",
        "primary_carc",
        "denial_category",
        "adjudication_date",
        "days_submission_to_adjudication",
        "preventable",
    }
    present = label_cols & set(lines[0].keys())
    check(
        present == label_cols,
        f"expected all label columns present and clearly named: missing {label_cols - present}",
    )

    print(
        f"\n  CSV: {len(lines)} lines, {len(adjr)} adjustment rows, {len(chains)} resubmitted lines"
    )

    if WARN:
        print("\nwarnings:")
        for w in WARN:
            print("  !", w)
    if FAIL:
        print(f"\nFAILED ({len(FAIL)}):")
        for f in FAIL[:25]:
            print("  x", f)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
