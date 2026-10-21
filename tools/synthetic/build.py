"""
Pipeline driver. Run:  python3 src/build.py --config config/config.yaml
"""

from __future__ import annotations

import argparse
import pathlib
import random
from datetime import date, timedelta
from decimal import Decimal

import yaml

from .generate import (
    CITIES,
    FIRST,
    LAST,
    ROOT,
    SPECIALTIES,
    STREETS,
    Patient,
    Practice,
    Provider,
    Reference,
    build_note,
    luhn_npi,
    member_id_for,
    money,
)


def weighted(rng, mapping):
    ks = list(mapping.keys())
    ws = [mapping[k] if not isinstance(mapping[k], tuple) else mapping[k][1] for k in ks]
    return rng.choices(ks, weights=ws, k=1)[0]


def build(cfg_path: pathlib.Path):
    cfg = yaml.safe_load(cfg_path.read_text())
    rng = random.Random(cfg["seed"])
    ref = Reference()

    start = date.fromisoformat(cfg["start_date"])
    months = cfg["months"]
    end = start + timedelta(days=int(months * 30.44))

    # ---- practices and providers ----------------------------------------
    practices = []
    for i in range(cfg["practices"]):
        city, st, zp = rng.choice(CITIES)
        p = Practice(
            practice_id=f"PRAC{i + 1:03d}",
            name=f"{rng.choice(['Lakeview', 'Riverbend', 'Northgate', 'Summit', 'Copperfield'])} "
            f"{rng.choice(['Medical Group', 'Health Partners', 'Clinic', 'Associates'])}",
            npi=luhn_npi(rng),
            tin="".join(str(rng.randint(0, 9)) for _ in range(9)),
            addr=f"{rng.randint(100, 9999)} {rng.choice(STREETS)}",
            city=city,
            state=st,
            zip=zp,
        )
        for _ in range(cfg["providers_per_practice"]):
            spec = weighted(rng, {k: v[1] for k, v in SPECIALTIES.items()})
            p.providers.append(
                Provider(
                    npi=luhn_npi(rng),
                    last=rng.choice(LAST),
                    first=rng.choice(FIRST),
                    specialty=spec,
                    taxonomy=SPECIALTIES[spec][0],
                )
            )
        practices.append(p)

    # ---- patients --------------------------------------------------------
    payer_share = {p["payer_id"]: float(p["share"]) for p in ref.payers}
    payer_by_id = {p["payer_id"]: p for p in ref.payers}
    patients = []
    for i in range(cfg["patients"]):
        payer = payer_by_id[weighted(rng, payer_share)]
        city, st, zp = rng.choice(CITIES)
        age = rng.choices(
            [rng.randint(1, 17), rng.randint(18, 44), rng.randint(45, 64), rng.randint(65, 92)],
            weights=[0.12, 0.30, 0.32, 0.26],
            k=1,
        )[0]
        dob = start - timedelta(days=age * 365 + rng.randint(0, 364))
        # some coverage terminates mid-window -> drives CARC 27
        cov_end = None
        if rng.random() < cfg["coverage_termination_rate"]:
            cov_end = start + timedelta(days=rng.randint(30, int(months * 30.44)))
        secondary = None
        if rng.random() < cfg["secondary_coverage_rate"]:
            secondary = weighted(rng, payer_share)
            if secondary == payer["payer_id"]:
                secondary = None
        patients.append(
            Patient(
                patient_id=f"PAT{i + 1:06d}",
                last=rng.choice(LAST),
                first=rng.choice(FIRST),
                dob=dob,
                sex=rng.choice("MF"),
                addr=f"{rng.randint(100, 9999)} {rng.choice(STREETS)}",
                city=city,
                state=st,
                zip=zp,
                payer_id=payer["payer_id"],
                member_id=member_id_for(payer, rng),
                coverage_start=start - timedelta(days=rng.randint(200, 1500)),
                coverage_end=cov_end,
                secondary_payer_id=secondary,
            )
        )

    # ---- encounters and claims -------------------------------------------
    encounters, claims = [], []
    claim_seq = 0
    enc_seq = 0
    total_days = (end - start).days

    for _ in range(cfg["encounters"]):
        enc_seq += 1
        pat = rng.choice(patients)
        prac = rng.choice(practices)
        prov = rng.choice(prac.providers)
        dos = start + timedelta(days=rng.randint(0, total_days - 1))
        payer = payer_by_id[pat.payer_id]

        # pick procedures for this specialty
        pool = ref.proc_by_specialty.get(prov.specialty) or ref.procedures
        dx_pool = ref.dx_by_specialty.get(prov.specialty) or ref.diagnoses
        age = (dos - pat.dob).days // 365
        dx_pool = [
            d
            for d in dx_pool
            if int(d["min_age"]) <= age <= int(d["max_age"])
            and (not d["sex_restriction"] or d["sex_restriction"] == pat.sex)
        ]
        if not dx_pool:
            dx_pool = [d for d in ref.diagnoses if not d["sex_restriction"]]

        n_dx = rng.choices([1, 2, 3], weights=[0.5, 0.35, 0.15], k=1)[0]
        dx_rows = rng.sample(dx_pool, min(n_dx, len(dx_pool)))

        n_lines = rng.choices([1, 2, 3, 4], weights=[0.46, 0.31, 0.16, 0.07], k=1)[0]
        proc_rows = rng.sample(pool, min(n_lines, len(pool)))
        # a claim carries at most one E/M line; two E/M codes on one date of
        # service is a coding error, not a realistic pattern to generate
        ems = [p for p in proc_rows if p["is_em"] == "Y"]
        if len(ems) > 1:
            keep = rng.choice(ems)
            proc_rows = [p for p in proc_rows if p["is_em"] == "N" or p is keep]

        copy_forward = rng.random() < cfg["copy_forward_rate"]
        note = build_note(rng, pat, age, dx_rows, proc_rows, copy_forward)

        # ---- build the claim -------------------------------------------
        claim_seq += 1
        claim_id = f"CLM{claim_seq:08d}"
        has_proc = any(p["is_em"] == "N" for p in proc_rows)
        has_em = any(p["is_em"] == "Y" for p in proc_rows)

        lines = []
        for n, pr in enumerate(proc_rows, start=1):
            mods = []
            if pr["is_em"] == "Y" and has_proc:
                # coder correctly applies modifier 25 most, but not all, of the time
                if rng.random() < cfg["modifier25_correct_rate"]:
                    mods.append("25")
            elif pr["is_em"] == "N":
                collides = any(
                    (o["code"], pr["code"]) in ref.ncci_pairs
                    or (pr["code"], o["code"]) in ref.ncci_pairs
                    for o in proc_rows
                    if o is not pr
                )
                if collides and rng.random() < cfg["ncci_bypass_correct_rate"]:
                    mods.append(rng.choice(["59", "XS", "XU"]))
                elif rng.random() < 0.08:
                    mods.append(rng.choice(["RT", "LT", "50"]))
            charge = money(Decimal(pr["typical_charge"]) * Decimal(str(rng.uniform(0.92, 1.12))))
            lines.append(
                {
                    "line_number": n,
                    "cpt": pr["code"],
                    "modifiers": mods,
                    "units": 1,
                    "charge": charge,
                    "dx_pointers": [1]
                    if len(dx_rows) == 1
                    else sorted(rng.sample(range(1, len(dx_rows) + 1), k=min(2, len(dx_rows)))),
                    "is_em": pr["is_em"] == "Y",
                    "specialty": prov.specialty,
                    "pos": pr["typical_pos"],
                }
            )

        # process context
        days_to_sub = int(rng.gammavariate(2.0, 6.0)) + 1
        if rng.random() < cfg["late_submission_rate"]:
            days_to_sub += rng.randint(60, 400)
        submission_date = dos + timedelta(days=days_to_sub)

        elig_checked = rng.random() < cfg["eligibility_check_rate"]
        if pat.coverage_end and dos > pat.coverage_end:
            elig_result = "inactive"
        elif rng.random() < cfg["member_id_error_rate"]:
            elig_result = "not_found"
        else:
            elig_result = "active" if elig_checked else "no_response"

        # A checked eligibility failure normally gets resolved before billing:
        # the front desk asks for the current card. Only the uncaught fraction
        # reaches the payer as a denial. Claims where eligibility was never
        # checked carry the error through, which is the point of the feature.
        if elig_checked and elig_result in ("inactive", "not_found"):
            if rng.random() < cfg["front_desk_correction_rate"]:
                elig_result = "active"

        member_ok = elig_result != "not_found"
        member_id = pat.member_id
        if not member_ok:
            member_id = member_id[:-2] + "X9"  # corrupted, drives CARC 140

        needs_auth = any(
            (ref.rule_idx.get((payer["payer_id"], l["cpt"])) or {}).get("prior_auth_required")
            == "Y"
            for l in lines
        )
        auth_number = None
        if needs_auth and rng.random() < cfg["auth_obtained_rate"]:
            auth_number = "AUTH" + "".join(str(rng.randint(0, 9)) for _ in range(8))

        claims.append(
            {
                "claim_id": claim_id,
                "encounter_id": f"ENC{enc_seq:08d}",
                "practice": prac,
                "provider": prov,
                "patient": pat,
                "payer": payer,
                "dos": dos,
                "submission_date": submission_date,
                "days_dos_to_submission": days_to_sub,
                "pos": lines[0]["pos"],
                "diagnoses": [d["icd10_code"] for d in dx_rows],
                "lines": lines,
                "total_charge": money(sum(l["charge"] for l in lines)),
                "eligibility_checked": elig_checked,
                "eligibility_result": elig_result,
                "member_id": member_id,
                "member_id_valid": member_ok,
                "prior_auth_required": needs_auth,
                "prior_auth_number": auth_number,
                "referral_number": None,
                "has_procedure_same_day": has_proc and has_em,
                "payer_sequence": 1,
                "original_claim_id": None,
                "submission_attempt": 1,
                "patient_id": pat.patient_id,
                "copy_forward": copy_forward,
            }
        )

        encounters.append(
            {
                "encounter_id": f"ENC{enc_seq:08d}",
                "claim_id": claim_id,
                "note_text": note,
                "note_type": "progress note",
                "specialty": prov.specialty,
                "place_of_service": lines[0]["pos"],
                "visit_type": "established" if rng.random() < 0.7 else "new",
                "codes_assigned_by_human": "|".join(l["cpt"] for l in lines),
                "modifiers_assigned": "|".join(",".join(l["modifiers"]) for l in lines),
                "icd10_assigned": "|".join(d["icd10_code"] for d in dx_rows),
                "coder_id_anonymized": f"CODER{rng.randint(1, cfg['coders']):02d}",
                "coding_time_minutes": max(2, int(rng.gammavariate(3, 2.4))),
                "was_audited": "Y" if rng.random() < cfg["audit_rate"] else "N",
                "audit_corrected_codes": "",
                "copy_forward_suspected": "Y" if copy_forward else "N",
                "downstream_denial": "",
            }
        )

    return cfg, rng, ref, practices, patients, claims, encounters, payer_by_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    args = ap.parse_args()
    out = build(pathlib.Path(args.config))
    print("built:", len(out[5]), "claims,", len(out[6]), "encounters")
