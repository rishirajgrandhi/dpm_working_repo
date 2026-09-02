# The Product — What We're Building and Why

*Start here. This is the whole product in one read, for someone who has to build it, sell it
internally, or decide whether it's worth doing. Everything else in this directory is detail
underneath what's on this page.*

---

## 1. The problem, in one story

A team migrates a customer database from AWS RDS into Snowflake. Along the way, `DIM_CUSTOMER`
becomes a slowly-changing dimension: when a customer's segment changes from `SMB` to
`ENTERPRISE`, the old row is closed off and a new version is opened. The history is the point —
it's what lets you ask "what was this customer's segment when they placed that order?"

One Tuesday, someone simplifies the merge statement. A `WHEN MATCHED THEN UPDATE` branch replaces
the type-2 insert. It ships. It looks fine.

From that day on, the dimension **overwrites** instead of versioning. History stops accumulating
and silently starts disappearing.

Here's the part that matters: **every existing check stays green.**

- Row counts match — one row per customer, same as always.
- The target matches the source — the current values are perfectly correct.
- Freshness is fine. The load runs on time. Nothing errors.
- The pipeline's own tests pass.

Six months later an analyst notices that a cohort report gives a different answer than it did in
March. The history needed to explain the discrepancy is the history that got destroyed. Nobody
can reconstruct it. The bug is six months old and there is no data to recover.

**That is the failure this product exists to catch**, and it is representative rather than
exotic. The general shape:

> Pipeline checks are written by hand, after something has already gone wrong. And we can't say
> what *isn't* being checked — so "no alerts" doesn't mean "everything is healthy."

## 2. What we're building

A monitoring product that answers two questions on a schedule, and says honestly which parts of
the answer it can't vouch for.

**Question 1 — Is the pipeline's logic doing what it's supposed to?**
Answered inside Snowflake alone, with plain SQL. Cheap, so it runs on every load.

**Question 2 — Do the source and the target actually agree?**
Answered by comparing RDS to Snowflake. Expensive, so it runs when someone asks.

Around those two questions sits the thing that makes it a product rather than a script:
checks that are **generated** rather than hand-written, **proven able to fail** before they're
trusted, deduplicated into incidents rather than alert spam, routed to the right team, and
reported through a dashboard that always shows what it *didn't* check next to what it did.

## 3. The three inputs

The whole product is a function of three things. Nothing else needs to exist — no dbt, no
orchestrator, no data catalog.

| Input | What we take | What we do with it |
|---|---|---|
| **The git repo** | Clone URL + read-only token | Read the transform SQL to learn what the pipeline *claims* to do. Read `git log`/`blame`/`CODEOWNERS` to know who to tell when it doesn't |
| **AWS RDS** | Read-only connection | The migration's source of truth. Read only, never written, budget-capped |
| **Snowflake** | Read-only role + write on two small schemas | The target. Also where every check runs and where all our state lives |

The repo is the input people underestimate. The transform SQL usually **contains the contract** —
a `QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY updated_at DESC) = 1` *is* the
deduplication rule, written down. We don't have to guess it; we read it, and then we check that
the data actually obeys it.

## 4. How it works — the shape of the thing

### 4.1 Two lanes, split by cost

| | **Lane A** | **Lane B** |
|---|---|---|
| Asks | Is the pipeline's logic correct? | Do the two systems agree? |
| Reads | Snowflake only | RDS vs Snowflake |
| Costs | Cents | Dollars — hard budget, abort switch |
| Runs | Every load, unattended | When someone clicks it, or on a PR |
| Reports | Slack + Jira, deduplicated, routed to the owning team | Back to whoever ran it |

Lane A runs when nobody's watching, so it needs machinery: deduplication, sign-offs, ownership,
a shadow period. Lane B has a human reading the output already, so it needs none of that. **One
engine, two speeds.** Every check declares which lane it's in.

Notice that our motivating bug — the dimension that stopped versioning — is a **Lane A** bug.
Lane B is green while it happens. That's why the cheap lane carries the weight.

### 4.2 Every hop of the medallion gets checked

The pipeline is `RDS → Bronze → Silver → Gold`. We check each hop, not just the endpoints,
because each hop has a different contract and a different way of breaking.

```
   RDS ──────▶ BRONZE ──────▶ SILVER ──────▶ GOLD
        L1            L2              L3
   "landed         "deduplicated   "modelled
    faithfully"     correctly"      correctly"
```

**L1 — source to bronze.** Bronze should be a photocopy: same rows, same values, no cleaning, no
dedup. Anything transformed here is a transformation hiding in the ingest layer. This is the only
cross-engine hop, so it's the expensive one.

**L2 — bronze to silver. This hop's contract is deduplication**, and it's the one with the
sharpest failure mode. Silver is supposed to be "bronze, with duplicates collapsed on a business
key, keeping the newest row." Four things can go wrong, and **three of them are invisible to
counting**:

| What goes wrong | Caught by counting rows? |
|---|---|
| Duplicates survived | Yes |
| Rows vanished with no explanation | Yes |
| **The wrong duplicate won** — it kept the stale row | **No.** Counts are perfect |
| **The pick is arbitrary** — two rows tie and there's no tiebreaker, so a rerun returns different data | **No.** Every count matches every time |

Those last two are why L2 gets a formal contract — key, a *total* deterministic pick rule, and
named losses — instead of a row-count check. We recompute the pick from bronze and assert silver
kept exactly that row. And we assert the ordering has no ties, because a pipeline that can return
different answers on a rerun is broken even when today's numbers look right.

**L3 — silver to gold.** Dimensions get the SCD suite (this is where the motivating bug lives).
Facts get conservation, fanout guards, and time-aware foreign keys. Marts get recomputed from
silver and compared group by group.

And when a mart has a measure we *can't* recompute — an average, a distinct count — we say so, in
the coverage report, next to the green ticks. A mart marked green with two silently unverified
measures is a lie of omission.

### 4.3 The eleven-check SCD suite

For each slowly-changing dimension we generate eleven checks from three declarations (business
key, which columns are tracked, which columns are validity dates). Exactly one current row per
key; no overlapping or missing date ranges; a new version appears only when something tracked
actually changed; and — **the one that catches our story** — a tracked change *did* create a new
version.

That last check is a single SQL statement comparing the current dimension row to the latest
staged row. If a tracked column differs and no new version was opened, the dimension is
overwriting instead of versioning. It fires the day the bug ships, not six months later.

## 5. Where the AI sits, and where it deliberately doesn't

This is the part that needs to be precise, because it's the first question anyone asks.

> **The AI writes the checks. The checks are deterministic SQL. The AI cannot decide whether a
> check passed.**

Two agents, both built on LangChain/LangGraph, both at the edges:

**The authoring agent** reads schemas, transform code, and lineage, then proposes: what type of
table is this, which columns are keys vs business vs audit, what's the grain, what's the dedup
contract, which measures are additive. It fills in **parameters for reviewed SQL templates** —
roughly 85% of checks are a pure parameter fill with nothing to read but a config diff. Its only
output channel is a **git pull request**. Nothing it produces goes live without a human merging it.

**The reporting agent** runs *after* a check has already failed and after deterministic evidence
has already been collected — schema diffs, load history, the commits that touched this table,
prior incidents. It picks a category from a **closed list**, groups related failures into one
incident, and writes a summary. It can't invent a root cause because the facts are already in the
prompt and it must cite them; `UNKNOWN` is a valid, reportable answer.

**What the AI never does:** decide a verdict, write to any table, see raw row values while
authoring, or produce free text we have to parse. If the LLM is unavailable, everything still
runs and reports — just with less narrative.

The worst case is a **bad suggestion**, never bad data and never a missed incident. That's the
property that gets this through a governance review.

## 6. Why the generated checks can be trusted

The obvious objection: *you generated hundreds of checks with an LLM — how do I know any of them
work?*

**A check that can never fail is worse than no check at all**, because it reads as coverage. So
before any check goes live it has to pass four gates:

1. **It runs within budget.**
2. **It passes on data we believe is good** — *and* it actually evaluated some rows. A check over
   an empty table returns zero violations and looks identical to a passing one.
3. **It fails when we inject the exact bug it exists to catch.** We zero-copy-clone the table,
   introduce the specific defect, and confirm the check goes red — with the right violation code,
   on the right key. This is the load-bearing one and it's a blocking merge gate.
4. **It gives the same answer twice on frozen data.** Non-deterministic checks flap, and flapping
   checks train people to ignore them.

For the SCD suite that means we literally reproduce the motivating incident — update a tracked
column in place, open no new version — and confirm the check catches it. If that test ever stops
failing, the build goes red, because the product's central claim just broke.

**One thing always needs a human: the grain.** A wrong grain usually produces a check that
*passes*, comparing nothing against nothing. Same for the dedup key and the mart's group-by. So
those get a named person's confirmation, recorded, before the checks can activate.

## 7. What people actually see

Everything is a web app. There is no CLI, no YAML to hand-edit, no terminal.

**The dashboard opens on the honest view**: open incidents, the medallion with per-hop status,
cost — and a panel titled *"What we are not checking"* that isn't collapsible and isn't below the
fold.

**When something breaks**, one Slack message arrives — not thirty. Related failures are grouped;
a break at bronze suppresses the twenty downstream gold failures it explains and files one
incident at the earliest failing layer. The message says what the violation is, which columns were
compared, which were excluded and why, what the pipeline was doing when it happened, and which
commit is probably responsible. It's assigned to the **team queue** and the likely author is
*mentioned* — being auto-assigned a ticket by a bot reads as blame, and our evidence is
correlational, so the wording should be too. Four buttons: acknowledge, false positive, snooze,
view rows.

**If it's the same failure tomorrow**, it's a threaded reply, not a new ticket. If a *new* row
starts failing, that's a new fingerprint and gets a fresh look. An accepted difference is bound to
a commit — when that code changes, the acceptance expires by itself and the check re-asserts. A
sign-off from March can't quietly hide a real bug in July.

**The Review Queue** is where the agent's proposals wait. A data owner sees "we think
`CUSTOMER_ID` is the dedup key, here's the `QUALIFY` clause we read it from, here's the observed
duplicate profile — is that right?" and clicks confirm. That's the 30 minutes of expert time the
whole onboarding depends on, and it's two buttons in a link, not a command someone has to be
talked through. The agent's graph is checkpointed, so it can wait days for that answer.

## 8. What onboarding costs

**Project one:** about a day of setup, plus a **two-week mandatory shadow period** — checks run
and report to a private channel, no tickets filed — during which we find the differences that are
deliberate but undocumented, and tune the noise out before anyone's first impression is formed.

**Project two and after:** a few hours. Onboarding is writing about fifty lines of config —
connections, where the code lives, column rules, ownership — not writing checks. Checks are
generated from known table types. Getting project two to 25% of project one's cost is an explicit
success metric.

Go-live is a deliberate act: a PR flipping one flag, approved by the owning team, backed by a
report the tool generates about its own precision during shadow.

## 9. What success looks like — and what we refuse to measure

| We track | Target |
|---|---|
| Mutation-test pass rate on active checks | **100%.** Non-negotiable |
| Runs reporting their column exclusions | 100% |
| Checks with a human-confirmed grain | 100% |
| Precision — incidents that turn out to be real | >70% at launch, >85% steady |
| Alerts per engineer per week | Under five |
| Time to detect and diagnose | Hours, not days — and falling |
| Onboarding cost, project two vs. one | 25% or less |

**Deliberately not tracked**, because each one rewards the wrong thing:

- *Number of checks.* 500 checks that can't fail is worse than 40 that can.
- *Percentage of checks passing.* Rewards weak checks.
- *Number of tickets filed.* Rewards noise.
- *A headline coverage percentage* without the breakdown behind it. That's the exact false
  comfort we're trying to eliminate.

## 10. The principles we won't trade away

1. **This tool reports on data. It never changes it.** No writes to RDS. In Snowflake it writes
   only to its own two schemas.
2. **No AI output goes live without a human review.** The authoring agent's only output is a PR.
3. **AI is never in the data path.** A bad suggestion, never bad data.
4. **We always say what we didn't check.** Green means "everything we checked, on the columns we
   compared, looks fine" — and the exclusions travel with the result, every time.
5. **We never guess.** If we can't tell whether a batch finished loading, checks come back
   `INCONCLUSIVE`, not `PASS`. "We couldn't tell" and "everything's fine" never share a status.
6. **We don't block deploys without buy-in.** The PR check is non-blocking.
7. **No new infrastructure.** State lives in a schema in the warehouse we already have.

## 11. What we're deliberately not building

Anomaly detection and learned thresholds — too noisy, and we don't have the run history yet.
Streaming sources. BI parity. A circuit breaker that quarantines datasets. Cross-engine hashing
beyond the one hop that needs it. A multi-tenant version. Each of these has a trigger condition
written down in `16`; none of them belong in v1.

## 12. The honest limitations

- **Coverage will never be 100%**, and we report it rather than rounding it up.
- **Lane B only catches a mismatch when someone runs it.** That's a deliberate cost tradeoff; if
  it becomes a problem, the fix is a hook, not a cron job.
- **Some queries are non-deterministic by nature**, and the tool needs to say so rather than flag
  a false failure.
- **Floating-point comparison uses tolerance**, never exact equality.
- **The biggest risk isn't technical.** It's that nobody acts on the reports. Fourteen of the
  twenty risks in the register are handled structurally, by a design decision rather than by
  relying on people to follow a process. That one isn't, and it's why we confirm a named,
  willing owner before we build the reporting path at all.

---

## Where to go next

| You want | Read |
|---|---|
| The full v1 scope and what's cut | [`01`](01_OVERVIEW_AND_SCOPE.md) |
| The three inputs and what breaks if an assumption is wrong | [`02`](02_INPUTS_AND_ASSUMPTIONS.md) |
| Components and control flow | [`03`](03_ARCHITECTURE.md) |
| The SCD suite, with SQL | [`06`](06_CHECK_MODEL_AND_TEMPLATES.md) |
| The dedup contract and layer parity | [`07B`](07B_LAYER_PARITY_MEDALLION.md) |
| How the agents are wired | [`09`](09_AGENTS_LANGGRAPH.md) |
| Why the checks can be trusted | [`10`](10_VALIDATION_GATES.md) |
| Screens, API, roles | [`12`](12_APPLICATION_SURFACE.md) |
| The build order | [`15`](15_BUILD_PLAN.md) |
| What we still need answered | [`16`](16_OPEN_QUESTIONS.md) |
