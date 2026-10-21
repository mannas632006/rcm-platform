"""
Emit phase. Takes the built claims, runs adjudication, injects the edge cases
from DOC-02 section 13, and writes X12 plus CSV outputs.

Edge cases deliberately injected here (each has a switch in config.yaml):
  EC-41  duplicate submission from retry
  EC-46  bulk 835 spanning many claims and one EFT
  EC-47  recoupment clawing back an earlier payment, via PLB
  EC-49  835 with zero payment carrying denial information
  EC-51  capitated zero-dollar lines
  EC-59  generic CO-16 with no explanatory RARC
  EC-65  same claim denied differently on resubmission
  plus   unbalanced file for quarantine testing
"""

from __future__ import annotations

import csv
import json
import pathlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .build import build
from .generate import OUT, ROOT, Adjudicator, money
from .x12 import write_835, write_837p, write_999


def ensure(p: pathlib.Path):
    p.mkdir(parents=True, exist_ok=True)
    return p


def run(config_path: pathlib.Path):
    cfg, rng, ref, practices, patients, claims, encounters, payer_by_id = build(config_path)

    x837 = ensure(OUT / "x12" / "837")
    x835 = ensure(OUT / "x12" / "835")
    x999 = ensure(OUT / "x12" / "999")
    csvd = ensure(OUT / "csv")
    for d in (x837, x835, x999):
        for f in d.glob("*.txt"):
            f.unlink()

    adj = Adjudicator(ref, rng, cfg)
    history = {"billed": set()}

    enc_by_claim = {e["claim_id"]: e for e in encounters}
    claims.sort(key=lambda c: c["submission_date"])

    remit_rows, line_rows, adj_rows, header_rows = [], [], [], []
    resubmissions = []
    ctrl = 100001

    # ---------------- submission + adjudication --------------------------
    adjudicated = []
    for c in claims:
        payer = c["payer"]
        res = adj.adjudicate(c, payer, history)
        lag = max(3, int(rng.gauss(float(payer["mean_adjudication_days"]), 5)))
        c["adjudication_date"] = c["submission_date"] + timedelta(days=lag)
        c["result"] = res
        adjudicated.append(c)

        # EC-65 / resubmission: a preventable denial gets corrected and resent
        if res["denied"] and rng.random() < cfg["resubmission_rate"]:
            child = dict(c)
            child["claim_id"] = c["claim_id"] + "R1"
            child["original_claim_id"] = c["claim_id"]
            child["submission_attempt"] = 2
            child["submission_date"] = c["adjudication_date"] + timedelta(days=rng.randint(3, 25))
            child["days_dos_to_submission"] = (child["submission_date"] - c["dos"]).days
            # the correction: fix what was fixable
            child["member_id_valid"] = True
            child["eligibility_result"] = (
                "active" if c["eligibility_result"] == "not_found" else c["eligibility_result"]
            )
            if c["prior_auth_required"] and not c["prior_auth_number"] and rng.random() < 0.7:
                child["prior_auth_number"] = "AUTH" + "".join(
                    str(rng.randint(0, 9)) for _ in range(8)
                )
            child["lines"] = [dict(l) for l in c["lines"]]
            for l in child["lines"]:
                if l["is_em"] and child["has_procedure_same_day"] and "25" not in l["modifiers"]:
                    if rng.random() < 0.8:
                        l["modifiers"] = l["modifiers"] + ["25"]
            resubmissions.append(child)

    for child in resubmissions:
        res = adj.adjudicate(child, child["payer"], history)
        lag = max(3, int(rng.gauss(float(child["payer"]["mean_adjudication_days"]), 5)))
        child["adjudication_date"] = child["submission_date"] + timedelta(days=lag)
        child["result"] = res
        adjudicated.append(child)

    adjudicated.sort(key=lambda c: c["submission_date"])

    # ---------------- 837 files, batched by practice + payer -------------
    batches: dict[tuple, list] = {}
    for c in adjudicated:
        # batch weekly, the way a real billing operation submits
        wk = c["submission_date"] - timedelta(days=c["submission_date"].weekday())
        key = (c["practice"].practice_id, c["payer"]["payer_id"], wk.isoformat())
        batches.setdefault(key, []).append(c)

    submitter = {
        "etin": "SUBMITTER01",
        "name": "RCM SEED BILLING",
        "contact": "OPS",
        "phone": "5125550100",
    }
    receiver = {"etin": "CLEARINGHSE1", "name": "SEED CLEARINGHOUSE"}

    n837 = 0
    for (prac_id, payer_id, sub_iso), group in sorted(batches.items()):
        payload = []
        for c in group:
            p, prov, pat = c["practice"], c["provider"], c["patient"]
            payload.append(
                {
                    "claim_id": c["claim_id"],
                    "total_charge": c["total_charge"],
                    "pos": c["pos"],
                    "dos": c["dos"],
                    "diagnoses": c["diagnoses"],
                    "lines": c["lines"],
                    "payer_sequence": c["payer_sequence"],
                    "claim_filing_code": {
                        "Medicare": "MB",
                        "Medicaid": "MC",
                        "Medicare Advantage": "16",
                        "MCO": "16",
                        "Workers Compensation": "WC",
                    }.get(c["payer"]["plan_type"], "CI"),
                    "patient_last": pat.last,
                    "patient_first": pat.first,
                    "patient_addr": pat.addr,
                    "patient_city": pat.city,
                    "patient_state": pat.state,
                    "patient_zip": pat.zip,
                    "patient_dob": pat.dob,
                    "patient_sex": pat.sex,
                    "member_id": c["member_id"],
                    "payer_name": c["payer"]["canonical_name"],
                    "payer_id": c["payer"]["payer_id"],
                    "billing_name": p.name,
                    "billing_npi": p.npi,
                    "billing_addr": p.addr,
                    "billing_city": p.city,
                    "billing_state": p.state,
                    "billing_zip": p.zip,
                    "billing_tin": p.tin,
                    "taxonomy": prov.taxonomy,
                    "prior_auth_number": c["prior_auth_number"],
                    "referral_number": c["referral_number"],
                    "original_claim_id": c["original_claim_id"],
                }
            )
        ctrl += 1
        # a fraction of files omit newlines - your parser must not depend on them
        nl = rng.random() > cfg["no_newline_file_rate"]
        text = write_837p(
            payload,
            submitter,
            receiver,
            ctrl,
            datetime.combine(date.fromisoformat(sub_iso), datetime.min.time()),
            newlines=nl,
        )
        (x837 / f"837P_{sub_iso}_{prac_id}_{payer_id}_{ctrl}.txt").write_text(
            text, encoding="utf-8"
        )
        n837 += 1
        (x999 / f"999_{ctrl}.txt").write_text(
            write_999(receiver["etin"], submitter["etin"], ctrl, True, ctrl),
            encoding="utf-8",
        )

    # ---------------- 835 files, batched by payer + check date -----------
    remit_batches: dict[tuple, list] = {}
    for c in adjudicated:
        # weekly remittance cycle -> one 835 spanning many claims, one EFT (EC-46)
        wk = c["adjudication_date"] - timedelta(days=c["adjudication_date"].weekday())
        key = (c["payer"]["payer_id"], wk.isoformat())
        remit_batches.setdefault(key, []).append(c)

    n835 = 0
    recoup_pool = [c for c in adjudicated if not c["result"]["denied"]]
    recouped = set()
    if recoup_pool:
        k = int(len(recoup_pool) * cfg["recoupment_rate"])
        recouped = {c["claim_id"] for c in rng.sample(recoup_pool, min(k, len(recoup_pool)))}

    for (payer_id, adj_iso), group in sorted(remit_batches.items()):
        payer = payer_by_id[payer_id]
        payee = group[0]["practice"]
        remits = []
        for c in group:
            r = c["result"]
            remits.append(
                {
                    "claim_id": c["claim_id"],
                    "claim_status_code": r["claim_status_code"],
                    "total_charge": c["total_charge"],
                    "total_paid": r["total_paid"],
                    "patient_responsibility": r["patient_responsibility"],
                    "claim_filing_code": {
                        "Medicare": "MB",
                        "Medicaid": "MC",
                        "Medicare Advantage": "16",
                        "MCO": "16",
                        "Workers Compensation": "WC",
                    }.get(c["payer"]["plan_type"], "CI"),
                    "payer_control_number": "PCN" + c["claim_id"][3:],
                    "patient_last": c["patient"].last,
                    "patient_first": c["patient"].first,
                    "member_id": c["member_id"],
                    "rendering_npi": c["provider"].npi,
                    "rendering_last": c["provider"].last,
                    "rendering_first": c["provider"].first,
                    "dos": c["dos"],
                    "claim_adjustments": [],
                    "lines": [
                        {
                            "cpt": l["cpt"],
                            "modifiers": l["modifiers"],
                            "charge": l["charge"],
                            "paid": l["paid"],
                            "allowed": l["allowed"],
                            "units": l["units"],
                            "adjustments": l["adjustments"],
                            "rarcs": l["rarcs"],
                        }
                        for l in r["lines"]
                    ],
                }
            )

        plbs = []
        for c in group:
            if c["claim_id"] in recouped:
                plbs.append(
                    {
                        "fiscal_year_end": date(c["dos"].year, 12, 31),
                        "reason_code": "WO",
                        "reference": "PCN" + c["claim_id"][3:],
                        "amount": c["result"]["total_paid"],
                    }
                )

        check_total = money(sum(r["total_paid"] for r in remits) - sum(p["amount"] for p in plbs))
        ctrl += 1
        nl = rng.random() > cfg["no_newline_file_rate"]
        text = write_835(
            payer={
                "etin": f"PAYER{payer_id}",
                "name": payer["canonical_name"],
                "routing": "011000015",
                "account": "1234567890",
                "tin": "9" + payer_id,
                "addr": "1 Payer Plaza",
                "city": "Hartford",
                "state": "CT",
                "zip": "06103",
                "contact": "PROVIDER SERVICES",
                "phone": "8005550199",
            },
            payee={
                "etin": "PAYEE0001",
                "name": payee.name,
                "npi": payee.npi,
                "addr": payee.addr,
                "city": payee.city,
                "state": payee.state,
                "zip": payee.zip,
                "tin": payee.tin,
            },
            remits=remits,
            ctrl=ctrl,
            check_amount=check_total,
            check_date=date.fromisoformat(adj_iso),
            trace="TRN" + str(ctrl),
            newlines=nl,
            now=datetime.combine(date.fromisoformat(adj_iso), datetime.min.time()),
            provider_adjustments=plbs,
        )
        (x835 / f"835_{adj_iso}_{payer_id}_{ctrl}.txt").write_text(text, encoding="utf-8")
        n835 += 1

    # ---------------- deliberately unbalanced file for quarantine tests ---
    if cfg.get("emit_unbalanced_file", True) and remit_batches:
        key = sorted(remit_batches)[0]
        group = remit_batches[key][:3]
        payer = payer_by_id[key[0]]
        payee = group[0]["practice"]
        remits = [
            {
                "claim_id": c["claim_id"],
                "claim_status_code": "1",
                "total_charge": c["total_charge"],
                "total_paid": c["result"]["total_paid"],
                "patient_responsibility": money(0),
                "claim_filing_code": "CI",
                "payer_control_number": "PCN" + c["claim_id"][3:],
                "patient_last": c["patient"].last,
                "patient_first": c["patient"].first,
                "member_id": c["member_id"],
                "rendering_npi": c["provider"].npi,
                "rendering_last": c["provider"].last,
                "rendering_first": c["provider"].first,
                "dos": c["dos"],
                "claim_adjustments": [],
                "lines": [
                    {
                        "cpt": l["cpt"],
                        "modifiers": l["modifiers"],
                        "charge": l["charge"],
                        "paid": l["paid"],
                        "allowed": l["allowed"],
                        "units": l["units"],
                        "adjustments": l["adjustments"],
                        "rarcs": l["rarcs"],
                    }
                    for l in c["result"]["lines"]
                ],
            }
            for c in group
        ]
        ctrl += 1
        bad_total = money(sum(r["total_paid"] for r in remits) + Decimal("137.42"))
        text = write_835(
            payer={
                "etin": f"PAYER{key[0]}",
                "name": payer["canonical_name"],
                "routing": "011000015",
                "account": "1234567890",
                "tin": "9" + key[0],
                "addr": "1 Payer Plaza",
                "city": "Hartford",
                "state": "CT",
                "zip": "06103",
                "contact": "PS",
                "phone": "8005550199",
            },
            payee={
                "etin": "PAYEE0001",
                "name": payee.name,
                "npi": payee.npi,
                "addr": payee.addr,
                "city": payee.city,
                "state": payee.state,
                "zip": payee.zip,
                "tin": payee.tin,
            },
            remits=remits,
            ctrl=ctrl,
            check_amount=bad_total,
            check_date=date.fromisoformat(key[1]),
            trace="TRNBAD" + str(ctrl),
        )
        (x835 / f"835_UNBALANCED_quarantine_test_{ctrl}.txt").write_text(text, encoding="utf-8")

    # ---------------- CSV marts ------------------------------------------
    for c in adjudicated:
        r = c["result"]
        header_rows.append(
            {
                "claim_id": c["claim_id"],
                "original_claim_id": c["original_claim_id"] or "",
                "encounter_id": c["encounter_id"],
                "practice_id": c["practice"].practice_id,
                "patient_id_hashed": c["patient"].patient_id,
                "payer_id": c["payer"]["payer_id"],
                "plan_type": c["payer"]["plan_type"],
                "payer_sequence": c["payer_sequence"],
                "billing_npi": c["practice"].npi,
                "rendering_npi": c["provider"].npi,
                "taxonomy_code": c["provider"].taxonomy,
                "specialty": c["provider"].specialty,
                "place_of_service": c["pos"],
                "date_of_service": c["dos"].isoformat(),
                "submission_date": c["submission_date"].isoformat(),
                "adjudication_date": c["adjudication_date"].isoformat(),
                "total_claim_charge": str(c["total_charge"]),
                "line_count_on_claim": len(c["lines"]),
                "submission_attempt_number": c["submission_attempt"],
                "recouped": "Y" if c["claim_id"] in recouped else "N",
            }
        )
        for l in r["lines"]:
            mods = (list(l["modifiers"]) + ["", "", "", ""])[:4]
            dxs = (list(c["diagnoses"]) + ["", "", "", ""])[:4]
            line_rows.append(
                {
                    "claim_id": c["claim_id"],
                    "line_number": l["line_number"],
                    "patient_id_hashed": c["patient"].patient_id,
                    "practice_id": c["practice"].practice_id,
                    "original_claim_id": c["original_claim_id"] or "",
                    "payer_id": c["payer"]["payer_id"],
                    "plan_type": c["payer"]["plan_type"],
                    "payer_sequence": c["payer_sequence"],
                    "billing_npi": c["practice"].npi,
                    "rendering_npi": c["provider"].npi,
                    "taxonomy_code": c["provider"].taxonomy,
                    "specialty": c["provider"].specialty,
                    "place_of_service": l["pos"],
                    "patient_age_at_dos": (c["dos"] - c["patient"].dob).days // 365,
                    "patient_sex": c["patient"].sex,
                    "patient_state": c["patient"].state,
                    "patient_zip3": c["patient"].zip[:3],
                    "cpt_hcpcs": l["cpt"],
                    "modifier_1": mods[0],
                    "modifier_2": mods[1],
                    "modifier_3": mods[2],
                    "modifier_4": mods[3],
                    "units": l["units"],
                    "primary_dx": dxs[0],
                    "dx_2": dxs[1],
                    "dx_3": dxs[2],
                    "dx_4": dxs[3],
                    "dx_pointers": ",".join(str(p) for p in l["dx_pointers"]),
                    "line_charge_amount": str(l["charge"]),
                    "total_claim_charge": str(c["total_charge"]),
                    "line_count_on_claim": len(c["lines"]),
                    # --- process context, all knowable before submission -----
                    "date_of_service": c["dos"].isoformat(),
                    "submission_date": c["submission_date"].isoformat(),
                    "days_dos_to_submission": c["days_dos_to_submission"],
                    "days_to_timely_filing_deadline": int(c["payer"]["timely_filing_days"])
                    - c["days_dos_to_submission"],
                    "eligibility_checked": "Y" if c["eligibility_checked"] else "N",
                    "eligibility_result": c["eligibility_result"],
                    "prior_auth_required": "Y" if c["prior_auth_required"] else "N",
                    "prior_auth_number_present": "Y" if c["prior_auth_number"] else "N",
                    "submission_attempt_number": c["submission_attempt"],
                    "submission_dow": c["submission_date"].weekday(),
                    "submission_month": c["submission_date"].month,
                    # --- labels, from the 835 -------------------------------
                    "adjudication_date": c["adjudication_date"].isoformat(),
                    "line_allowed_amount": str(l["allowed"]),
                    "line_paid_amount": str(l["paid"]),
                    "is_denied": "Y" if l["denied"] else "N",
                    "primary_carc": l["carc_codes"][0] if l["carc_codes"] else "",
                    "denial_category": ref.carc[l["carc_codes"][0]]["denial_category"]
                    if l["carc_codes"]
                    else "",
                    "preventable": ref.carc[l["carc_codes"][0]]["preventable"]
                    if l["carc_codes"]
                    else "",
                    "days_submission_to_adjudication": (
                        c["adjudication_date"] - c["submission_date"]
                    ).days,
                }
            )
            for i, a in enumerate(l["adjustments"], start=1):
                adj_rows.append(
                    {
                        "claim_id": c["claim_id"],
                        "line_number": l["line_number"],
                        "segment_index": i,
                        "cas_group_code": a["group"],
                        "carc_code": a["carc"],
                        "adjustment_amount": str(a["amount"]),
                        "rarc_codes": "|".join(l["rarcs"]),
                    }
                )

    def dump(name, rows):
        if not rows:
            return
        with (csvd / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"  csv/{name:24s} {len(rows):7d} rows")

    # link denial outcome back onto encounters for Dataset 2
    denied_claims = {c["claim_id"] for c in adjudicated if c["result"]["denied"]}
    for e in encounters:
        e["downstream_denial"] = "Y" if e["claim_id"] in denied_claims else "N"
        if e["was_audited"] == "Y" and rng.random() < 0.35:
            e["audit_corrected_codes"] = e["codes_assigned_by_human"]

    dump("claim_lines.csv", line_rows)
    dump("adjustments.csv", adj_rows)
    dump("claim_headers.csv", header_rows)
    dump("encounters.csv", encounters)
    import shutil

    shutil.copy(ROOT / "reference" / "payer_rules.csv", csvd / "payer_rules.csv")

    denied = sum(1 for r in line_rows if r["is_denied"] == "Y")
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": cfg,
        "counts": {
            "claims": len(adjudicated),
            "original_claims": len(adjudicated) - len(resubmissions),
            "resubmissions": len(resubmissions),
            "service_lines": len(line_rows),
            "adjustment_rows": len(adj_rows),
            "encounters": len(encounters),
            "x12_837_files": n837,
            "x12_835_files": n835,
            "recouped_claims": len(recouped),
        },
        "denial_rate_all_attempts": round(denied / max(1, len(line_rows)), 4),
        "warning": (
            "Synthetic. Denials reflect only the rules in "
            "Adjudicator.adjudicate(). Do not train a production model on this."
        ),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m tools.synthetic` and for `make seed`.

    Stamps the manifest with the package's provenance marker so the training
    pipeline (M6) can refuse a run whose data came from here -- see the package
    docstring for why that refusal exists.
    """
    import argparse

    from . import PROVENANCE

    ap = argparse.ArgumentParser(prog="python -m tools.synthetic")
    ap.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    args = ap.parse_args(argv)

    manifest = run(pathlib.Path(args.config))
    manifest["provenance"] = PROVENANCE
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    print(f"corpus written to {OUT}")
    for name, value in manifest["counts"].items():
        print(f"  {name:<28} {value}")
    print(f"  denial rate (all attempts)   {manifest['denial_rate_all_attempts']}")
    print(f"  provenance                   {PROVENANCE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
