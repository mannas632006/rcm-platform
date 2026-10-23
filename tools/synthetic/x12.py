"""
X12 5010 writers for 837P (professional claim) and 835 (remittance advice).

The point of this module is that the output is real X12 with correct loop
structure, not CSV pretending to be X12. Your parser has to be tested against
the thing it will actually meet in production.

Segment terminator is ~, element separator *, composite separator :.
Line breaks after each segment are optional in X12 and are emitted here to keep
the files readable; a parser that depends on them is broken and should fail
against the no-newline variant (see writer(newlines=False)).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

ELEM = "*"
COMP = ":"
TERM = "~"


def _d8(d: date) -> str:
    return d.strftime("%Y%m%d")


def _d6(d: date) -> str:
    return d.strftime("%y%m%d")


def _hm(d: datetime) -> str:
    return d.strftime("%H%M")


def _amt(x) -> str:
    """X12 money: no thousands separators, no currency symbol, up to 2dp."""
    return f"{Decimal(str(x)).quantize(Decimal('0.01'))}"


class SegmentWriter:
    def __init__(self, newlines: bool = True):
        self.segments: list[str] = []
        self.newlines = newlines

    def add(self, *elements):
        parts = [str(e) if e is not None else "" for e in elements]
        # X12 permits trailing empty elements to be dropped
        while len(parts) > 1 and parts[-1] == "":
            parts.pop()
        self.segments.append(ELEM.join(parts))

    def count(self) -> int:
        return len(self.segments)

    def render(self) -> str:
        sep = "\n" if self.newlines else ""
        return (
            TERM + sep.join([""] + self.segments).lstrip("\n")
            if False
            else sep.join(s + TERM for s in self.segments)
        )


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
def _isa(w: SegmentWriter, sender: str, receiver: str, ctrl: int, now: datetime, usage: str = "T"):
    w.add(
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        sender.ljust(15),
        "ZZ",
        receiver.ljust(15),
        _d6(now.date()),
        _hm(now),
        "^",
        "00501",
        str(ctrl).zfill(9),
        "0",
        usage,
        COMP,
    )


def _gs(
    w: SegmentWriter, code: str, sender: str, receiver: str, ctrl: int, now: datetime, version: str
):
    w.add("GS", code, sender, receiver, _d8(now.date()), _hm(now), str(ctrl), "X", version)


def _close(w: SegmentWriter, st_index: int, ctrl: int, gs_ctrl: int, tx_count: int):
    # SE01 counts every segment from ST through SE inclusive, so the SE
    # segment about to be written counts itself.
    se_count = w.count() - st_index + 2
    w.add("SE", str(se_count), str(ctrl).zfill(4))
    w.add("GE", str(tx_count), str(gs_ctrl))
    w.add("IEA", "1", str(gs_ctrl).zfill(9))


# ---------------------------------------------------------------------------
# 837P — Professional claim
# ---------------------------------------------------------------------------
def write_837p(
    claims: list,
    submitter: dict,
    receiver: dict,
    ctrl: int,
    now: datetime | None = None,
    newlines: bool = True,
) -> str:
    """
    claims: list of claim dicts (see generate.py for the shape).
    All claims in one file must share a billing provider and payer, which is
    how real batching works.
    """
    now = now or datetime.now(UTC)
    w = SegmentWriter(newlines)
    _isa(w, submitter["etin"], receiver["etin"], ctrl, now)
    _gs(w, "HC", submitter["etin"], receiver["etin"], ctrl, now, "005010X222A1")

    st_index = w.count() + 1
    w.add("ST", "837", str(ctrl).zfill(4), "005010X222A1")
    w.add("BHT", "0019", "00", str(ctrl), _d8(now.date()), _hm(now), "CH")

    # Loop 1000A submitter
    w.add("NM1", "41", "2", submitter["name"], "", "", "", "", "46", submitter["etin"])
    w.add("PER", "IC", submitter["contact"], "TE", submitter["phone"])
    # Loop 1000B receiver
    w.add("NM1", "40", "2", receiver["name"], "", "", "", "", "46", receiver["etin"])

    first = claims[0]
    hl = 1
    # Loop 2000A billing provider
    w.add("HL", str(hl), "", "20", "1")
    w.add("PRV", "BI", "PXC", first["taxonomy"])
    w.add("NM1", "85", "2", first["billing_name"], "", "", "", "", "XX", first["billing_npi"])
    w.add("N3", first["billing_addr"])
    w.add("N4", first["billing_city"], first["billing_state"], first["billing_zip"])
    w.add("REF", "EI", first["billing_tin"])
    billing_hl = hl
    hl += 1

    for c in claims:
        # Loop 2000B subscriber
        w.add("HL", str(hl), str(billing_hl), "22", "0")
        w.add(
            "SBR",
            "P" if c["payer_sequence"] == 1 else "S",
            "18",
            "",
            "",
            "",
            "",
            "",
            "",
            c["claim_filing_code"],
        )
        # Loop 2010BA subscriber name
        w.add(
            "NM1",
            "IL",
            "1",
            c["patient_last"],
            c["patient_first"],
            "",
            "",
            "",
            "MI",
            c["member_id"],
        )
        w.add("N3", c["patient_addr"])
        w.add("N4", c["patient_city"], c["patient_state"], c["patient_zip"])
        w.add("DMG", "D8", _d8(c["patient_dob"]), c["patient_sex"])
        # Loop 2010BB payer
        w.add("NM1", "PR", "2", c["payer_name"], "", "", "", "", "PI", c["payer_id"])

        # Loop 2300 claim
        w.add(
            "CLM",
            c["claim_id"],
            _amt(c["total_charge"]),
            "",
            "",
            COMP.join([c["pos"], "B", "1"]),
            "Y",
            "A",
            "Y",
            "Y",
        )
        w.add("DTP", "434", "D8", _d8(c["dos"]))
        if c.get("prior_auth_number"):
            w.add("REF", "G1", c["prior_auth_number"])
        if c.get("referral_number"):
            w.add("REF", "9F", c["referral_number"])
        if c.get("original_claim_id"):
            # corrected claim: frequency 7 would be in CLM05-3; carry the ref
            w.add("REF", "F8", c["original_claim_id"])

        # HI diagnosis codes, ABK = principal, ABF = secondary
        dx = c["diagnoses"]
        hi = ["HI", COMP.join(["ABK", dx[0]])]
        for extra in dx[1:8]:
            hi.append(COMP.join(["ABF", extra]))
        w.add(*hi)

        # Loop 2400 service lines
        for line in c["lines"]:
            w.add("LX", str(line["line_number"]))
            sv1_proc = [("HC"), line["cpt"]]
            for m in line["modifiers"]:
                sv1_proc.append(m)
            w.add(
                "SV1",
                COMP.join(sv1_proc),
                _amt(line["charge"]),
                "UN",
                str(line["units"]),
                "",
                "",
                COMP.join(str(p) for p in line["dx_pointers"]),
            )
            w.add("DTP", "472", "D8", _d8(c["dos"]))
        hl += 1

    _close(w, st_index, ctrl, ctrl, 1)
    return w.render()


# ---------------------------------------------------------------------------
# 835 — Remittance advice
# ---------------------------------------------------------------------------
def write_835(
    payer: dict,
    payee: dict,
    remits: list,
    ctrl: int,
    check_amount,
    check_date: date,
    trace: str,
    now: datetime | None = None,
    newlines: bool = True,
    provider_adjustments: list | None = None,
) -> str:
    """
    remits: list of remittance dicts, one per claim. Each carries service lines,
    and each service line carries a LIST of adjustments (CAS segments). Multiple
    CAS reasons per line is the normal case and is preserved here deliberately,
    because collapsing them is the single most common way this data gets ruined.

    provider_adjustments: PLB entries, used for recoupments that claw back a
    previously paid claim. These are what make a file fail to balance if your
    parser ignores them.
    """
    now = now or datetime.now(UTC)
    w = SegmentWriter(newlines)
    _isa(w, payer["etin"], payee["etin"], ctrl, now)
    _gs(w, "HP", payer["etin"], payee["etin"], ctrl, now, "005010X221A1")

    st_index = w.count() + 1
    w.add("ST", "835", str(ctrl).zfill(4))

    # BPR — financial information
    w.add(
        "BPR",
        "I",
        _amt(check_amount),
        "C",
        "ACH",
        "CCP",
        "01",
        payer["routing"],
        "DA",
        payer["account"],
        "",
        "",
        "",
        "",
        "",
        _d8(check_date),
    )
    w.add("TRN", "1", trace, payer["tin"])
    w.add("REF", "EV", payer["etin"])
    w.add("DTM", "405", _d8(check_date))

    # Loop 1000A payer
    w.add("N1", "PR", payer["name"])
    w.add("N3", payer["addr"])
    w.add("N4", payer["city"], payer["state"], payer["zip"])
    w.add("PER", "CX", payer["contact"], "TE", payer["phone"])
    # Loop 1000B payee
    w.add("N1", "PE", payee["name"], "XX", payee["npi"])
    w.add("N3", payee["addr"])
    w.add("N4", payee["city"], payee["state"], payee["zip"])
    w.add("REF", "TJ", payee["tin"])

    # Loop 2000 header
    w.add("LX", "1")

    for r in remits:
        # Loop 2100 claim payment information
        w.add(
            "CLP",
            r["claim_id"],
            r["claim_status_code"],
            _amt(r["total_charge"]),
            _amt(r["total_paid"]),
            _amt(r["patient_responsibility"]) if r["patient_responsibility"] else "",
            r["claim_filing_code"],
            r["payer_control_number"],
            "11",
        )
        w.add(
            "NM1",
            "QC",
            "1",
            r["patient_last"],
            r["patient_first"],
            "",
            "",
            "",
            "MI",
            r["member_id"],
        )
        if r.get("rendering_npi"):
            w.add(
                "NM1",
                "82",
                "1",
                r["rendering_last"],
                r["rendering_first"],
                "",
                "",
                "",
                "XX",
                r["rendering_npi"],
            )
        w.add("DTM", "232", _d8(r["dos"]))
        w.add("DTM", "233", _d8(r["dos"]))
        # claim-level CAS, if any
        for cas in r.get("claim_adjustments", []):
            w.add(
                "CAS",
                cas["group"],
                cas["carc"],
                _amt(cas["amount"]),
                str(cas.get("quantity", "")) if cas.get("quantity") else "",
            )

        # Loop 2110 service payment information
        for ln in r["lines"]:
            svc_proc = ["HC", ln["cpt"]] + list(ln["modifiers"])
            w.add(
                "SVC",
                COMP.join(svc_proc),
                _amt(ln["charge"]),
                _amt(ln["paid"]),
                "",
                str(ln["units"]),
            )
            w.add("DTM", "472", _d8(r["dos"]))
            # one CAS segment per group code; up to 6 reason/amount pairs each
            by_group: dict[str, list] = {}
            for adj in ln["adjustments"]:
                by_group.setdefault(adj["group"], []).append(adj)
            for group, adjs in by_group.items():
                seg = ["CAS", group]
                for adj in adjs[:6]:
                    seg += [
                        adj["carc"],
                        _amt(adj["amount"]),
                        str(adj["quantity"]) if adj.get("quantity") else "",
                    ]
                w.add(*seg)
            w.add("AMT", "B6", _amt(ln["allowed"]))
            if ln.get("rarcs"):
                for rc in ln["rarcs"]:
                    w.add("LQ", "HE", rc)

    # PLB — provider level adjustments (recoupments, interest, forwarding balance)
    for plb in provider_adjustments or []:
        w.add(
            "PLB",
            payee["npi"],
            _d8(plb["fiscal_year_end"]),
            COMP.join([plb["reason_code"], plb["reference"]]),
            _amt(plb["amount"]),
        )

    _close(w, st_index, ctrl, ctrl, 1)
    return w.render()


# ---------------------------------------------------------------------------
# 999 / 277CA — acknowledgements, minimal but structurally valid
# ---------------------------------------------------------------------------
def write_999(
    sender: str,
    receiver: str,
    ctrl: int,
    accepted: bool,
    ref_group_ctrl: int,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    w = SegmentWriter(True)
    _isa(w, sender, receiver, ctrl, now)
    _gs(w, "FA", sender, receiver, ctrl, now, "005010X231A1")
    st_index = w.count() + 1
    w.add("ST", "999", str(ctrl).zfill(4), "005010X231A1")
    w.add("AK1", "HC", str(ref_group_ctrl), "005010X222A1")
    if not accepted:
        # A 999 saying only "rejected" sends an operator to open the file by
        # hand. AK2/IK3/IK4/IK5 name the transaction, the segment, its
        # position and the element that failed, which is the difference
        # between a queue someone can work and one they cannot.
        w.add("AK2", "837", str(ref_group_ctrl).zfill(4), "005010X222A1")
        w.add("IK3", "CLM", "23", "2300", "8")
        w.add("IK4", "2", "782", "7", "NOTANUMBER")
        w.add("IK5", "R", "5")
    w.add("AK9", "A" if accepted else "R", "1", "1", "1" if accepted else "0")
    _close(w, st_index, ctrl, ctrl, 1)
    return w.render()


# ---------------------------------------------------------------------------
# 277CA - claim acknowledgement
# ---------------------------------------------------------------------------
def write_277ca(
    payer: dict,
    submitter: dict,
    billing: dict,
    claims: list,
    ctrl: int,
    now: datetime | None = None,
    newlines: bool = True,
) -> str:
    """The payer's front-end verdict on a batch of claims.

    `claims` is a list of dicts carrying `claim_id`, `accepted`, `charge`,
    `dos`, `patient_last`, `patient_first` and, for a rejection, a `reason`.

    This is the transaction BR-M8-02 turns on: *"A claim MUST NOT be
    considered submitted until a 277CA acceptance is received. 999 acceptance
    alone is insufficient -- it confirms syntax, not acceptance."* The corpus
    carried 325 syntax acknowledgements and no claim acknowledgements at all,
    so that rule had nothing to check against.

    Accepted claims carry status category A2 (accepted into adjudication);
    rejected ones carry A3 (returned as unprocessable), which is a
    *rejection* in DOC-02 section 2.5's sense and never a denial.
    """
    now = now or datetime.now(UTC)
    w = SegmentWriter(newlines)
    _isa(w, payer["etin"], submitter["etin"], ctrl, now)
    _gs(w, "HN", payer["etin"], submitter["etin"], ctrl, now, "005010X214")

    st_index = w.count() + 1
    w.add("ST", "277", str(ctrl).zfill(4), "005010X214")
    w.add("BHT", "0085", "08", str(ctrl), _d8(now.date()), _hm(now), "TH")

    # 2000A information source: the payer answering.
    w.add("HL", "1", "", "20", "1")
    w.add("NM1", "PR", "2", payer["name"], "", "", "", "", "PI", payer["payer_id"])
    w.add("TRN", "1", f"BATCH{ctrl}")
    w.add("DTP", "050", "D8", _d8(now.date()))

    # 2000B information receiver: who sent the claims.
    w.add("HL", "2", "1", "21", "1")
    w.add("NM1", "41", "2", submitter["name"], "", "", "", "", "46", submitter["etin"])

    # 2000C provider of service.
    w.add("HL", "3", "2", "19", "1")
    w.add("NM1", "85", "2", billing["name"], "", "", "", "", "XX", billing["npi"])

    hl = 4
    for c in claims:
        # 2000D patient, then the claim status below it.
        w.add("HL", str(hl), "3", "22", "0")
        w.add("NM1", "QC", "1", c["patient_last"], c["patient_first"])
        w.add("TRN", "2", c["claim_id"])
        if c["accepted"]:
            w.add("STC", COMP.join(["A2", "20"]), _d8(now.date()), "WQ", _amt(c["charge"]))
            w.add("REF", "1K", "PCN" + c["claim_id"][3:])
        else:
            w.add(
                "STC",
                COMP.join(["A3", c.get("status_code", "21")]),
                _d8(now.date()),
                "U",
                _amt(c["charge"]),
                "",
                "",
                "",
                "",
                "",
                "",
                c.get("reason", "Missing or invalid information"),
            )
        w.add("DTP", "472", "D8", _d8(c["dos"]))
        hl += 1

    _close(w, st_index, ctrl, ctrl, 1)
    return w.render()
