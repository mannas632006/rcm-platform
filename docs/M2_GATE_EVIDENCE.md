# M2 gate evidence — warehouse, marts and the leakage suite

CLAUDE.md section 5 on M2.4: the leakage suite *"must fail on a deliberate
leak"*, and the gate asks to see that failure rather than be told about it.
SRS section 5.6 is blunter: this is *"the requirement most likely to be
violated accidentally"*, and *"it silently destroys model validity"*.

This file covers M2.1 (dbt project), M2.2 (`mart_claim_line_outcome`), M2.3
(point-in-time features), M2.4 (the leakage suite), M2.5 (temporal splits),
M2.6 (the maturity gate) and M2.7 (reconstruction).

---

## The leakage suite, and the failure the gate asks for

### How it decides

The obvious way to police leakage is a list of forbidden column names and a
reviewer. That catches the obvious cases and misses the ones that matter,
because the accident is never "somebody added a column called `line_paid` to
the features". It is that a column was computed from a document that did not
exist when the claim was sent, and nothing about the SQL looks wrong.

So the suite asks DR-07's question empirically instead:

> *"Could this value have been known the second before we pressed SEND?"*

The warehouse is built **twice** — once with everything, once as it looked on
a cutoff date — into two parallel sets of schemas. Every FEATURE column is
then compared for the rows present in both builds. If removing data that
arrived after submission changes a feature's value, that value was built from
information that did not exist yet. No reading of the SQL is required, and it
does not matter how the column was written, what it is called, or how many
models it was laundered through.

Labels are expected to differ — that is what makes them labels — so they are
excluded from the comparison, and a separate test asserts that they **do**
differ. A comparison where nothing moves proves nothing, and that assertion
is what stops the suite passing by comparing an empty set to an empty set.

A second, cheaper layer catches a different mistake: every column in the mart
must appear in a registry that classifies it KEY / FEATURE / LABEL /
PROVENANCE. A column nobody has classified fails the build. You cannot add a
feature without saying it is one, and saying so is the moment the question
gets asked.

### The deliberate leak, and the output

The leak introduced is the exact mistake DR-07 describes, and it is the one
that would survive review. A denial rate "over the last 90 days" is naturally
written as a group-by over claims **submitted** in that window. One word
changes:

```diff
-        la.payment_date as known_on,
+        sub.submission_date as known_on,
```

It reads correctly. It is about the right claims, over the right window, in
the right direction. And it is leakage: a claim submitted ten days before
this one may not have been adjudicated yet, so its outcome was not knowable
when this claim went out.

```
E   AssertionError: these FEATURE columns changed when data from after
    submission was removed, so they could not have been known at submission:
    {'cpt_modifier_denial_rate_90d': 4, 'cpt_modifier_window_size': 7,
     'payer_provider_denial_rate_90d': 8, 'payer_provider_window_size': 8,
     'payer_cpt_window_size': 6, 'payer_cpt_denial_rate_90d': 2}

FAILED tests/leakage/test_feature_leakage.py::test_features_knowable_at_submission_time
FAILED tests/leakage/test_feature_leakage.py::test_the_trailing_rate_matches_an_independent_recomputation
2 failed, 6 passed
```

Both layers fire, and the suite names all six affected columns rather than
reporting that something, somewhere, moved.

The second leak demonstrates the registry layer. A column derived from the
label, added to the mart and classified nowhere:

```diff
+    j.line_paid / nullif(j.line_charge, 0) as payment_ratio,
```

```
E   AssertionError: unclassified columns: ['payment_ratio']
```

Both mutations were restored and the files verified byte-identical by hash
afterwards; the suite returns to 8 passed.

### Why the window excludes the current day

The frame is `[as_of - 90 days, as_of - 1 day]`. A remittance that landed
earlier the same morning *was* knowable, so including the current day would
be defensible — but at date granularity "same day" cannot be distinguished
from "a few hours later", and PD-18 says to fail closed. The cost is a
slightly staler feature. The alternative is a leak that nothing detects.

---

## M2.3 — the submission timestamp was the wrong one

The first build of the trailing features returned null for every row, and
every claim in the corpus sat in a single month.

The cause is worth recording because it is a failure of DR-07 that satisfies
DR-07's wording. Until this milestone the only submission-shaped timestamp in
CORE was `raw_file.received_at` — when **this platform archived the file**,
not when the claim was sent. The two agree in steady-state operation and
diverge exactly where it is most expensive: replaying a year of history in an
afternoon gives every claim the same `received_at`. Every trailing window
then looks back at a period containing nothing.

The features really were "computed with an as-of date equal to the claim's
submission timestamp". The timestamp was just the wrong one, and nothing in
the output said so.

BHT04 — the date the submitter stated — is the business submission date, and
the 837 parser had been reading it since M1.5. It had nowhere to live.
Migration 0008 adds `claim.submitted_on`, the loader fills it, and the
corpus now spans thirteen months:

```
                 month   lines   with history   avg window
            2024-01-01     104              2          0.1
            2024-02-01     179             65          0.8
            2024-03-01     152            118          1.9
            2024-04-01     186            174          3.1
            2024-05-01     130            121          3.1
            2024-06-01     170            159          3.3
```

That shape is the point. A correct point-in-time feature is **empty at the
start and fills in**. If every row had a value, the feature would be reading
history that did not exist yet.

The column is nullable and the mart records which date it used
(`submission_date_is_archival`), so a corpus loaded by replay can be told
from one loaded as it arrived. If that flag is ever true in a training set,
DR-07 is only approximately satisfied and the row says so.

---

## M2.2 — the mart, and the columns with no source yet

Grain: **one row per submitted service line per adjustment reason** (PD-6).

The direction of the joins is the load-bearing decision. The model starts
from what was **submitted** and joins outcomes on, every join LEFT. Starting
from the remittance would have been more natural and would silently drop
every claim the payer rejected — building a training set from a population
that excludes the claims most worth predicting, while every number in it
remained a real count of real rows.

```
mart rows                        1279
distinct submitted lines         1018
rows carrying an adjustment      1218   (1218 distinct — no fan-out)
rows with no adjustment            61   (all never adjudicated)
```

The sixteen line-level adjustments that do not reach the mart are all on
reversal postings, which is intended: a reversal restates a claim to take the
payment back, and admitting it would double the grain. `was_reversed` and
`net_collected` carry the fact without duplicating the row.

**Sixteen of the 73 columns are present, typed, and null**, each naming the
milestone that fills it — patient demographics and eligibility (M3), scrub
flags and the denial taxonomy (M4), and three that are M1 parser gaps
(`rendering_npi`, `taxonomy`, `ndc_code` need 837 loops 2310B, 2000A PRV and
LIN). They are in the mart rather than waiting outside it because SRS 5.5 is
a contract something downstream is written against, and a column that appears
later is a schema change nobody planned for. A test asserts they stay null,
so a placeholder that quietly acquires data is a placeholder somebody filled
without review.

`rendering_npi` is the interesting one. The NPI *was* a fact about the claim
before it was sent, and `claim_payment.rendering_provider_npi` is sitting
right there — but that row did not exist at submission. Reading it would
restrict the training set to adjudicated claims and launder a
payer-transformed value into a feature. It stays null until the 837 carries
it.

---

## Bugs this milestone found

**`sum()` over an empty window returns NULL, not 0.** Caught by checking the
windowed implementation against a slow, obvious, strictly-backwards
recomputation that shares no code with it. Zero says "we looked back ninety
days and there was nothing" — a real observation, and the state every
payer/code pair starts in. NULL says "not known". A model handed NULL for
both cannot tell a brand-new code from one it failed to measure. 203 lines
were affected.

**Every dbt data test passes on an empty table.** This is how dbt tests work
— they select violating rows and fail if any come back — so a build against
an empty warehouse reports a clean sweep. The M2 mutation pass found it the
hard way: four controls came back "not proven" because the harness had
rebuilt against a database another suite had truncated, and every assertion
went green over nothing. `assert_the_warehouse_is_not_empty` is now the floor
the others stand on. Same lesson as M1.6's corpus containing no 277CAs: a
suite that cannot tell "nothing is wrong" from "nothing is there" is not a
suite.

**dbt staging views blocked a CORE migration from rolling back.** Found by
checking that 0008 downgrades, which it did not:

```
cannot drop column submitted_on of table claim because other objects depend on it
DETAIL:  view analytics_staging.stg_claims depends on column submitted_on
```

OLTP and analytics share one PostgreSQL instance by design (CLAUDE.md section
3: *"no separate warehouse yet"*), and a staging **view** makes the warehouse
a dependent object of the CORE tables. Postgres then refuses any migration
touching a column that a view selects. That turns "can this migration be
rolled back" into "has anyone run dbt in this database lately" — and a
rollback path is something the Definition of Done requires to exist, not
something that depends on unrelated state.

Staging is now materialised as tables rather than views. It is against the
usual dbt convention and it costs a copy of the staging layer; it buys
migrations that do not care whether the warehouse has been built. The
downgrade and re-apply now both run clean.

**The truncated build kept lines whose claims it had dropped.** The as-of
filter was applied to claims but not to their service lines, so the two
builds described slightly different worlds and 97 lines failed to reach the
mart. A line belongs to a claim; it is now truncated with it, reading the
cutoff from `stg_claims` rather than restating the date arithmetic, because
two copies of that expression is how the harness ends up reporting a leak
that is really a bug in the harness.

---

## Prove the negative — 11 controls, 11 proven

Each control removed once, the test that guards it run and confirmed to fail,
the source restored and verified byte-identical by hash.

```
PROVEN  DR-07     trailing window keyed on adjudication, not submission
PROVEN  DR-07     the window looks backwards, not forwards
PROVEN  PD-11     every mart column is classified
PROVEN  DR-09     the maturity gate filters
PROVEN  PD-3      a CARC never reaches the mart without its group
PROVEN  PD-4      a rejection is not a denial in the mart
PROVEN  PD-6      every submitted line reaches the mart
PROVEN  BR-M9-04  reversals do not double the grain (both filters together)
PROVEN  DR-08     a random split is refused
PROVEN  PD-18     a row with no date of service is never mature
PROVEN  PD-10     the mart is a pure function of CORE
```

Two findings from the pass itself, both worth more than the verdicts:

**BR-M9-04 is only proven as a pair.** `core_claim_adjudication` filters to
non-reversal postings *and* to the first posting per claim. Removing either
alone changes nothing on this corpus — reversals always arrive after the
original, so the rank filter excludes them anyway, and no claim has two
non-reversal postings, so the rank filter is a no-op. Removing both fails the
test with 13 violations. They are genuine belt-and-braces rather than one
control and one decoration, and they would diverge on data this corpus does
not contain: a payer dating a reversal before the original, or a claim with
two non-reversal postings.

**The maturity gate could not be proven against the corpus at all.** Every
row in the synthetic corpus is dated 2024 and long since mature, so removing
the filter from `mart_final_outcome` changed nothing. The same gap as M1.5's
missing multi-modifier lines and M1.10's missing reversals: the corpus could
not express the shape the control guards. `tests/integration/test_maturity_gate.py`
now builds two claims identical but for their date of service, one on each
side of the boundary, and the control is proven against that.

---

## M2.5 — a random split is refused, not unsupported

DR-08 could have been satisfied by simply not writing a random split. That
leaves a gap somebody fills under deadline, so `split()` refuses the
strategies a reasonable person would try next — `random`, `shuffle`,
`stratified`, `kfold`, `group` — each with its own reason:

```
random      draws test rows from the same time period as training rows, so the
            model is scored on payer behaviour it has already seen
kfold       puts future rows in the training set for every fold but the last
stratified  balances classes across the boundary: a random split with extra steps
```

A developer told "random is not supported" goes looking for the supported
one. A developer told why it leaks stops. `SplitStrategy` has one member, and
a test asserts it stays that way — if a `RANDOM` member ever appears, it can
be selected without going through `split()` and every refusal becomes
decorative.

---

## M2.7 — what "byte-identical" can mean

PD-10 asks that every layer rebuild from RAW and code. Surrogate primary keys
are `uuid4`, so a reload mints new ones and a literal byte comparison fails
for a reason that has nothing to do with correctness.

UUIDs are arbitrary labels, not facts about the claim. The test compares the
business keys and the values — every column except the surrogates, every row,
in business-key order, hashed. Deriving ids deterministically from content
would make the literal comparison possible and is recorded below as an open
design question rather than quietly assumed away.

Two reconstructions, because they fail for different reasons:

```
marts from CORE    catches a model that is not a pure function of its inputs
CORE from RAW      catches a loader that depends on what was already loaded
```

The digest carries its own row count and a third test asserts it exceeds 100,
because an empty mart hashes to a stable value and both comparisons would
otherwise pass by comparing nothing to nothing.

---

## Full gate at this commit

```
lint                 OK
format               107 files already formatted
mypy strict          no issues found in 67 source files
module boundaries    3 contracts kept, 0 broken
unit                 266 passed
property              25 passed
integration          111 passed   (against a real Postgres 16)
security             102 passed   (against a real Postgres 16, as rcm_app)
leakage                8 passed   (builds the warehouse twice)
                     ---------
                     512 passed
dbt build             48 nodes: 18 models, 30 tests, 0 errors
coverage floor       rcm/ingestion/x12  98.2%  (1276/1300)   rcm/domain  99.7% (348/349)
migrations           alembic check clean; 0008 downgrades and re-applies cleanly
later-stage guard    dbt build and the leakage suite are now wired; only the
                     frontend, tfsec and Playwright stages remain unbuilt
prime directives     PD-16, PD-17, S-11, T20 all OK
bandit               no medium or high findings
```

Invariant tests from CLAUDE.md section 7.2 added by this milestone:

```
test_features_knowable_at_submission_time   tests/leakage/test_feature_leakage.py
test_random_split_is_rejected               tests/unit/test_training_rules.py
```

Both were listed in section 7.2 from M0 and neither could be written until
now: the first needs a mart to compare, the second needs a split API to
refuse.

### Still open

- Surrogate keys are `uuid4`, so a reconstruction reproduces the same facts
  under different ids. Deriving them from content (uuid5 over the business
  key) would make PD-10's comparison literal and let downstream references
  survive a rebuild. It touches every M1 loader, so it is a change to make
  deliberately rather than inside this milestone.
- The mart is a table, not time-partitioned. SRS 5.5 says "time-partitioned";
  at corpus volume it does not matter, and dbt's Postgres adapter has no
  first-class partitioning, so this needs a custom materialisation and a
  partition key decision (submission date, most likely).
- `payer_provider_denial_rate_90d` keys on the **billing** NPI, because the
  rendering NPI is not parsed. They differ for group practices, which is
  exactly where the feature would be most informative.
- The maturity window is `[TBD-50]`, defaulted to DR-09's recommended 180
  days. That is a number from a specification, not a measurement of this book
  of business, and it is wrong in both directions for some payers.
- `days_to_filing_deadline` reads `payer.timely_filing_days`, which has no
  staleness check until M4.10. A stale term produces a confident wrong
  deadline.
- dbt runs as the migration role because it issues DDL. That is correct but
  it means the warehouse build has more privilege than the application; a
  dedicated `rcm_analytics` role owning only the `analytics_*` schemas would
  be tighter.
- The two builds the leakage suite compares share the same `core_*` model
  code by construction. A leak introduced identically into both would be
  invisible to the comparison — which is why the independent recomputation
  of the trailing window exists alongside it.
