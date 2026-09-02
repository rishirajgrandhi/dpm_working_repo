# Data Pipeline Integrity Agent — Scope Research

Companion to [PLAN.md](PLAN.md). PLAN.md defines the V1 slice (intra-Snowflake, git-blame ownership, Jira filing). This document maps the **full opportunity space**: what the idea can become, what source/target combinations are possible, which of them are genuinely hard, and where the architectural seams must sit today so the widening is cheap later.

Nothing here changes V1 scope. It exists to make sure V1's interfaces don't paint us into a corner.

---

## 1. Reframing the Idea

PLAN.md describes the product as "parity checks + RCA + Jira". The more durable framing — and the one that determines scope — is three separable engines:

| Engine | Question it answers | Commodity? |
|---|---|---|
| **Comparison engine** | "Do these two datasets agree?" | Yes — Datafold data-diff, Soda, Great Expectations, dbt tests, Monte Carlo all do versions of this |
| **Causation engine** | "*Why* don't they agree?" | No — this is mostly human labour today |
| **Routing engine** | "Who fixes it, and how do they find out?" | No — Monte Carlo/Bigeye alert; they rarely assign correctly |

**Implication for scope:** the comparison engine should be built for breadth and cheapness (many source types, low cost per check), because breadth is what generates the volume of incidents. The differentiated value — and where effort should concentrate — is the causation + routing layer. If we ever have to choose between "one more connector" and "better RCA grounding", RCA wins.

A second reframing worth holding: **a check is a hypothesis about an invariant.** Parity (A must equal B) is one invariant class. Once the engine can evaluate arbitrary invariants with the same RCA/routing pipeline behind it, the product stops being "a diff tool" and becomes "a data-incident agent". §5 lists the invariant classes.

---

## 2. Source / Target Taxonomy

The `source` / `target` fields in `checks.yaml` are currently Snowflake identifiers. The realistic universe of what can occupy those slots:

### 2.1 By system class

| Class | Examples | Typical role | Access mechanism | Hard parts |
|---|---|---|---|---|
| **Cloud DW** | Snowflake, BigQuery, Redshift, Databricks/Delta, Synapse, ClickHouse | source *and* target | SQL, pushdown-friendly | Cheap and easy. Dialect differences in hash/date functions |
| **OLTP relational** | Postgres/RDS, MySQL/Aurora, SQL Server, Oracle | almost always source | SQL, but **production OLTP** | Can't run heavy scans on prod; replica lag; moving target during scan |
| **Object storage / lakehouse** | S3/GCS/ADLS + Parquet, Iceberg, Delta, Hudi, plain CSV/JSON | source or target | External tables, DuckDB, engine-native readers | File-level vs row-level semantics; partial writes; no transactions on raw CSV; schema-on-read drift |
| **Vendor / SFTP file feeds** | daily CSV drops, fixed-width, Excel | source of truth for ingestion checks | Download + parse | Encoding, delimiters, header drift, "the vendor resent Tuesday's file" |
| **SaaS systems of record** | Salesforce, HubSpot, NetSuite, Workday, Zendesk, Shopify, Stripe | source (and *target* for reverse-ETL) | REST/bulk APIs, or their own reporting endpoints | Rate limits, pagination bugs, soft deletes invisible via API, API-reported totals ≠ queryable rows, no cheap COUNT |
| **Ad / marketing platforms** | Google Ads, Meta, TikTok, LinkedIn | source | API | **Retroactive restatement** — numbers for last week legitimately change. Naive parity is guaranteed to false-alarm |
| **Payment / financial** | Stripe, Adyen, bank statements, GL/ERP | source of truth for reconciliation | API/file | Money semantics: currency, rounding, timezone-of-settlement, fees. Zero tolerance politically, non-zero tolerance technically |
| **Streaming** | Kafka, Kinesis, Pub/Sub, Debezium CDC | source | offsets/consumer lag, not row scans | No stable "total" to compare against; only offset-window counts and DLQ depth |
| **Serving / NoSQL stores** | DynamoDB, MongoDB, Elasticsearch/OpenSearch, Redis, Cassandra | usually **target** (egress side) | API/query, weak aggregation | Eventual consistency; scans expensive; no SQL aggregates |
| **Feature stores / vector DBs** | Feast, Tecton, Pinecone, pgvector | target | API | Coverage/count parity only; embeddings aren't value-comparable — compare presence + dimension + staleness |
| **BI / semantic layer** | Looker, Tableau extracts, Power BI datasets, Cube, cached agg tables | target | API or extract query | "Dashboard says 4.2M, warehouse says 4.3M" — highest-visibility failure class, rarely monitored |
| **Reverse-ETL destinations** | Braze, Salesforce, Klaviyo, ad audiences | target | API | Sync failures silently shrink audiences; nobody notices until campaign metrics drop |
| **The same table, at a different time** | Snowflake Time Travel, Iceberg snapshots, dated snapshot tables | both | SQL `AT(TIMESTAMP =>)` | Cheapest high-value check that needs no second system (§3.10) |

### 2.2 The pairing matrix

Not all N×N pairs are equally valuable. Ranked by (value × feasibility):

**Tier A — do these, high value, tractable**
- Snowflake ↔ Snowflake (stage-to-stage) — V1
- Snowflake ↔ Snowflake, different timestamps (self-parity / silent-backfill detection)
- Postgres/MySQL → Snowflake (ingestion fidelity, the single most common real complaint)
- S3/Iceberg → Snowflake (landing fidelity)
- Snowflake → BI cached aggregate / semantic layer
- Old model ↔ new model during a refactor (regression parity)
- Legacy platform ↔ new platform during a migration (dual-run parity)

**Tier B — high value, meaningfully harder**
- Salesforce/SaaS API → Snowflake (needs a snapshot-landing strategy, §4.G)
- Snowflake → reverse-ETL destination (needs destination-side counting)
- Financial reconciliation vs external statement (needs tolerance semantics, §6.4)
- Cross-cloud DW ↔ DW (Snowflake ↔ BigQuery) — mostly a migration case

**Tier C — possible, low priority or awkward**
- Kafka → warehouse (offset math, not parity)
- Vector/feature store coverage
- Elasticsearch/DynamoDB egress parity (expensive scans)
- Ad platforms (restatement makes strict parity meaningless; becomes drift monitoring instead)

**Design consequence:** V1 must not assume both sides share a connection. The `Check` interface should take **two independent handles plus a comparison strategy**, even while both handles happen to be the same Snowflake session. That single decision is what makes Tier A/B additive rather than a rewrite.

---

## 3. Comparison Topologies

"Source vs target" is one shape out of many. Each of these reuses the same RCA/routing pipeline, which is why they're scope-relevant rather than scope-creep.

1. **Stage-to-stage (linear)** — raw → staging → mart. Localizes *where* in the chain loss occurred. Running the whole chain as one check-set lets RCA say "row loss originates at staging, not the mart".
2. **Ingestion fidelity** — external system → landing table. Catches CDC gaps, connector pauses, pagination bugs.
3. **Migration dual-run** — old stack vs new stack producing the same output. Very high willingness-to-pay; naturally time-boxed; the migration team *wants* daily diffs. Strong wedge use case.
4. **Refactor regression** — same input, old SQL vs new SQL, run in CI on a PR rather than on a cron. This turns the product into a *pre-merge gate*, which is where it prevents rather than reports incidents. Arguably the highest-leverage extension in this whole document.
5. **Egress parity** — warehouse → serving store / SaaS / BI.
6. **Replica / region parity** — primary vs replica vs DR copy.
7. **External reconciliation** — internal numbers vs vendor's own report (Stripe payouts vs revenue table; ad spend vs invoice).
8. **Fan-in** — sum of N sources = union table. Catches "one region's feed stopped" which per-source freshness checks miss if the union still looks healthy.
9. **Fan-out / rollup consistency** — mart aggregates = detail table aggregates; daily/weekly/monthly rollups agree with each other.
10. **Self-parity across time** — for tables that should be append-only or immutable-history: does yesterday's row for order #123 still say what it said yesterday? Detects **silent restatements and unannounced backfills**, which are the failures nobody catches. Needs only one system + time travel.
11. **Cross-entity presence** — a customer that exists in the CRM, the warehouse, the billing system, and the product DB. Set-diff across four systems; surfaces onboarding/offboarding pipeline gaps.
12. **Contract conformance** — the target conforms to a declared schema/semantic contract, independent of any source. Not parity, same plumbing.

---

## 4. Comparison Strategies (how a check is actually executed)

This is the technical heart of widening. Strategy is chosen from `(source_type, target_type, table_size, cost_budget)`.

- **A. Single-engine pushdown.** Both sides same engine → one SQL statement, no data movement. V1's case. Cheapest by an order of magnitude.
- **B. Federated pushdown.** Make the second side visible to the first: Snowflake external tables over S3, Iceberg catalogs, BigQuery federated queries, Postgres FDW, Databricks Lakehouse Federation. When available this converts a cross-platform check into strategy A. **Underrated — check for this before writing a connector.**
- **C. Both-sides aggregate pull.** Each side computes its own aggregates (counts, sums, hash-of-hashes) in its own dialect; the agent compares small result sets. Data volume moved ≈ kilobytes. This is the default cross-platform mechanism.
- **D. Hierarchical / bisecting hash (Merkle-style).** Bucket keys (e.g. `mod(hash(key), 1000)` or by date partition), compare bucket-level hashes, recurse only into mismatching buckets. Gives *row-level* localization at *aggregate* cost. This is the core trick behind Datafold's data-diff and the right approach for "which rows differ, without pulling the table".
- **E. Key-set diff.** Pull only (hashed) keys from both sides, set-difference in the agent or a temp table. Answers missing-vs-extra-vs-changed, which is the first thing an engineer asks.
- **F. Stratified sample cell compare.** For diverging buckets/keys, pull full rows for a bounded sample and diff column-by-column to name the offending column. Bounded cost, high explanatory value — this is what makes an RCA writeup concrete.
- **G. Snapshot-and-land.** For APIs/files where you can't query: land a snapshot into the warehouse, then use strategy A against it. Costs storage, but makes every SaaS/file source uniform. **The one strategy that makes Tier B tractable.**
- **H. Offset/metadata accounting.** For streams and loaders: compare produced offsets vs consumed vs loaded vs rejected. Not row comparison at all — a different `Check` subtype.
- **I. Metadata-only checks.** Row counts from `INFORMATION_SCHEMA`/table stats, freshness from `LAST_ALTERED`. Nearly free; good for a broad cheap tier running hourly with expensive checks daily.

**Recommended layering:** cheap tier (I) hourly → volume/aggregate tier (A/C) daily → localization tier (D/E/F) only *on failure*. Localization is RCA evidence gathering, not a routine check. PLAN.md §4.2's "bounded sample of what diverged" should be explicitly reframed as this on-failure escalation.

---

## 5. Check / Invariant Catalog

PLAN.md starts with `row_count` and `checksum`. The full catalog, grouped, with the failure mode each actually catches:

**Volume**
- Total row count; count per partition/day/tenant/region (catches partial loss the total hides)
- Delta count vs previous run; count within a bounded watermark window
- Duplicate-key count, PK uniqueness

**Value**
- Order-independent aggregate checksum over key + business columns
- Per-column checksums (tells you *which* column broke, for barely more cost)
- Numeric `sum`/`min`/`max`/`avg` per column — cheap, and hash-normalization-proof
- Distinct-count per column; top-N category frequency

**Set membership**
- Keys in source missing from target / extra in target
- Orphaned foreign keys; referential integrity across tables

**Cell-level**
- Column-by-column diff on joined sampled keys → "`amount` differs on 412 of 500 sampled rows, target is always 100× smaller" (a currency-minor-units bug, instantly diagnosable)

**Completeness / quality**
- Null rate, empty-string rate, whitespace-only rate, per column
- Default/sentinel flooding (`1970-01-01`, `-1`, `'unknown'` spiking)
- Truncation signature (values at exactly `varchar(n)` length)

**Freshness / timeliness**
- `max(updated_at)` vs now; vs source's max; SLA-relative ("must be ≤ 2h old by 08:00")
- Load-completed-by-deadline
- Lag between source and target watermark

**Schema / contract**
- Column added/removed/renamed; type, precision, nullability change
- Enum/domain expansion (a new `status` value the transform silently drops)
- Column ordering (only matters for positional loads)

**Distribution drift**
- Numeric quantiles, stddev; categorical frequency shift (PSI/KL/chi-square)
- Seasonality-aware volume bands (day-of-week baselines) — necessary to avoid Monday-morning false alarms

**Business invariants** (highest signal, lowest generality — must be user-authored)
- `revenue = qty × unit_price − discount` within tolerance
- Double-entry balances sum to zero; subledger ties to GL
- Status transitions legal; no negative inventory; no future-dated events
- SCD2: no overlapping validity ranges, no gaps, exactly one current row per key
- Join-fanout guard: output rows ≤ expected multiple of input rows (catches the classic accidental many-to-many)

**Cost / operational** (adjacent but shares the plumbing)
- Warehouse credits per pipeline vs baseline; runtime regression; retry counts

A useful internal distinction: **parity checks** (need two datasets), **invariant checks** (need one), **trend checks** (need history). All three feed the same RCA/routing pipeline; only the evidence collector differs. Building for all three from the start costs little; retrofitting the runner for single-dataset checks later is annoying.

---

## 6. The Hard Problems (where naive implementations die)

These are the reasons cross-platform parity has a reputation for being painful. Each needs a deliberate answer, not a discovered one.

### 6.1 Canonicalization — "the hashes never match"
The single most common failure of homegrown checksum comparison. Two engines will disagree on identical data unless values are canonicalized to text under an explicit policy:

- **Numerics:** `1.50` vs `1.5` vs `1.500`; `NUMBER(38,2)` vs `float`. Cast to fixed scale, or round then format.
- **Floats:** never hash a float. Round to a declared precision or compare via `sum` with tolerance instead.
- **Timestamps:** timezone-naive vs `TIMESTAMP_TZ`; microsecond vs nanosecond vs second precision; DST. Normalize to UTC, truncate to a declared precision.
- **Strings:** trailing whitespace (Oracle `CHAR` padding), case-insensitive collations, Unicode normalization form (NFC vs NFD — real with names/addresses), encoding.
- **NULL:** must map to a sentinel that cannot collide with real data; `NULL` vs `''` must be a *policy decision* (Oracle famously conflates them).
- **Booleans:** `true`/`1`/`'Y'`/`'t'`.
- **JSON/semi-structured:** key ordering, whitespace, numeric formatting — normalize or exclude.
- **Hash function portability:** `MD5` exists nearly everywhere; `HASH()` is Snowflake-specific; BigQuery has `FARM_FINGERPRINT`. Standardize on MD5 (or SHA-256) over a canonical string, then aggregate order-independently — `sum(bigint from first 8 bytes)` or bitwise XOR (watch overflow; XOR is overflow-free but blind to duplicate pairs, so pair it with a row count).

**Recommendation:** a `Canonicalizer` module with per-engine, per-type rules and an explicit per-check override, built in V1 even for intra-Snowflake use (staging vs prod often differ in exactly these ways). This is the highest-risk omission in PLAN.md as written.

### 6.2 Temporal alignment — both sides are moving
Comparing a live OLTP table to a warehouse copy will *always* differ because the source moved during the scan. Mechanisms, roughly in order of preference:
- **Watermark-bounded comparison:** compare only rows with `updated_at <= T`, where `T = min(source_watermark, target_watermark) − grace`. Requires a reliable monotonic column.
- **Point-in-time snapshots:** Snowflake `AT(TIMESTAMP =>)`, Iceberg/Delta snapshot IDs, Postgres repeatable-read snapshot. Best fidelity when available.
- **Exclude the hot partition:** compare all days except today.
- **Grace/tolerance window plus re-verify:** on divergence, wait N minutes and re-run before declaring failure. **Cheapest and most effective single noise reducer** — most cross-system divergences are just lag.
- **Hard deletes:** if the source hard-deletes and CDC doesn't capture it, "extra rows in target" is permanent and expected. Needs a per-check `deletes: propagated|not_propagated` declaration or it becomes a chronic false alarm.
- **Late-arriving data and legitimate restatement:** ad platforms and finance restate history by design. For these, parity must be *as-of* comparison or drift monitoring, not equality.

### 6.3 Semantic gaps between the two sides
Source and target are frequently *not* supposed to be identical: intentional filtering (test accounts, soft-deleted rows), deduplication, type widening, PII masking, currency conversion, row-access policies. Every check needs optional `source_filter` / `target_filter` / column mapping / exclusion list. Without it, real pipelines produce permanent red checks and the tool gets ignored.

**Subtle trap:** row-access policies and column masking mean *the agent's service role may legitimately see a different dataset than the pipeline does*. A permission change can look exactly like data loss. Worth an explicit RCA hypothesis class (§8) and a startup assertion on the role's grants.

### 6.4 Tolerance semantics
Binary pass/fail is wrong for most real checks. Needed: absolute and relative thresholds, per-check severity tiers (warn / fail / page), a minimum-absolute-difference floor so tiny tables don't alarm on one row, and *directional* thresholds (target having extra rows may be fine; missing rows never is). For money: tolerance must be expressible in minor units per currency.

### 6.5 Cost
An unbounded checksum over a billion-row table is a real credit burn, and a monitoring tool that costs more than the incidents it finds gets switched off. Controls: partition-scoped checks (only recent partitions), metadata-only tier, clustering-aware predicates, sampling with declared confidence, a per-check credit budget with a hard abort, and cost attribution recorded per check so the tool can report its own ROI. Consider a dedicated small warehouse with a resource monitor.

### 6.6 Data egress and PII
The agent will hold diverging sample rows — and PLAN.md sends evidence to an LLM. That is a data-governance event. Needed: per-column `pii: true` annotations (or default-deny with an allowlist of comparable/sendable columns), hash-only comparison for sensitive columns, redaction before the LLM call, and a record of what left the perimeter. Cheap to add now, very expensive to retrofit after a security review. Also relevant: whether the deployment can use an LLM at all in-region, and whether Bedrock/Vertex hosting is required.

### 6.7 Config scale
Five hand-written checks work. Five hundred don't. At some point you need check *generation*: enumerate table pairs from lineage or naming convention, apply a default check profile by table class, learn thresholds from history, and let YAML hold only the exceptions. Worth designing the config model so generated and hand-written checks coexist (a `source: generated|manual` provenance field), even if generation itself is deferred.

---

## 7. RCA Signal Universe

PLAN.md's three signals (schema diff, Snowflake history, git) are the right start. The broader menu, by category — with the point that **RCA quality scales with signal breadth far more than with prompt quality**:

**Warehouse / engine internals**
- Snowflake: `QUERY_HISTORY`, `LOAD_HISTORY`, `COPY_HISTORY`, `TASK_HISTORY`, `ACCESS_HISTORY` (**column-level lineage — very high value**), `OBJECT_DEPENDENCIES`, `TABLE_STORAGE_METRICS`, DDL history, stream lag, warehouse queuing, resource monitor suspensions
- BigQuery `INFORMATION_SCHEMA.JOBS`, Redshift `STL_*`, Databricks system tables / Delta history
- Postgres/MySQL: replication lag, slot retention, long transactions, error logs

**Orchestration**
- Airflow/Dagster/Prefect task failures, retries, SLA misses, duration anomalies, DAG code version
- Snowflake Tasks / dbt Cloud run history

**Ingestion tooling**
- Fivetran/Airbyte/Stitch sync logs, connector pauses, schema-change events, API rate-limit hits, incremental-cursor resets (**a cursor reset is a classic silent-loss root cause**)
- Kafka consumer lag, DLQ depth, rebalance events

**Transformation layer**
- dbt `manifest.json` (lineage, owners, tests) + `run_results.json`; failed/skipped models; source freshness results
- For custom Python/SQL (V1's case): script logs, exit codes, row-count logging if present

**Code and deploy**
- Commits in the failure window; the diff; blame; PR title/description/reviewers (**PR description is unusually informative context for an LLM**)
- CI runs, deploy markers, config/secret changes, Terraform diffs, IAM/grant changes

**Cross-check correlation**
- Did other checks fail in the same run? Do the failing tables share a lineage ancestor? → collapse 40 alerts into **one root incident with one ticket**. This is arguably the highest-value RCA feature in the document and it needs only data the system already has.
- Historical recurrence: has this check failed this way before, and what closed the last ticket? Feeding prior resolved incidents into the prompt turns the system into an institutional-memory engine — the strongest compounding-value mechanism available.

**External**
- Upstream SaaS status pages / incident feeds
- Business calendar (holidays, campaign starts/ends, releases, seasonality) — the difference between "anomaly" and "Black Friday"

---

## 8. RCA Output: classify, don't free-associate

PLAN.md's structured output should include a **hypothesis class** from a closed taxonomy, not just prose. Closed-set classification is far more reliable than open generation, it makes accuracy measurable, and it lets each class carry its own evidence requirements and routing rules:

1. Upstream load late / failed / partial
2. Transform code change (recent commit)
3. Upstream schema change
4. Source-system behaviour change (new enum, pagination, API version, deleted records)
5. Duplicate or double-run (re-processing without idempotency)
6. Late-arriving data / legitimate backfill / restatement
7. Filter or join regression (over-filtering, fanout)
8. Type / precision / timezone / encoding conversion
9. Permission, masking, or row-access-policy change
10. Infrastructure failure, timeout, throttling, partial write
11. Hard deletes not propagated (CDC gap)
12. Expected business change (seasonality, campaign, real volume shift) → **suppress or downgrade, don't file**
13. The check itself is stale or wrong → route to the monitoring team, not the pipeline owner

Each class implies a different assignee, severity, and ticket template. Classes 12 and 13 are what protect the tool's credibility — a system that can say "this is real, not a bug" is trusted; one that always files is noise. Recording predicted class vs the class a human confirmed on close gives a real accuracy metric to improve against.

---

## 9. Ownership Resolution — an ensemble, not one signal

git blame alone is fragile (file moves, formatter commits, a departed author, one person who touched everything). Candidate signals, best combined with weights and a confidence score:

- Blame on the *specific changed lines* in the failure window (not whole-file)
- Author of the most suspicious commit identified by RCA — usually the strongest single signal
- `CODEOWNERS` / directory ownership
- dbt `meta.owner`, model-level `group`
- Airflow DAG `owner`, task-level owner
- Data-catalog owner (DataHub, Atlan, Collibra, Snowflake tags/object tagging)
- Table-level tags/comments in the warehouse
- PagerDuty/Opsgenie on-call schedule for the owning team (right answer for time-sensitive failures — assign to a *person on duty*, not whoever last edited the file)
- Fallback team queue, always

Also worth modelling: **assign vs notify** are different. Ticket assignee should probably be the on-call or team queue, with the likely-culprit author @-mentioned rather than assigned — being auto-assigned a ticket by a bot because you renamed a column reads as blame, and social friction is a real adoption risk.

---

## 10. Action Surface — what happens after RCA

Jira is one output. The full menu, roughly by escalation:

- **Passive:** write to the state store; quality scorecard / trend dashboard; weekly digest
- **Informational:** Slack thread (with buttons: *acknowledge / false positive / snooze / escalate* — the feedback signal that lets accuracy be measured); email
- **Tracked:** Jira issue, comment on an existing incident, GitHub issue
- **Contextual:** comment on the suspect PR; annotate the table in the catalog so consumers see the warning; post the incident onto the BI dashboard that consumes the table (**closes the loop with the people actually harmed**)
- **Paging:** PagerDuty for SLA-critical datasets
- **Protective (circuit breaker):** mark the dataset stale/quarantined so downstream jobs and dashboards refuse or warn. **Prevents bad data from spreading — plausibly more valuable than any ticket.** Requires downstream cooperation, so it's a later-phase item, but the state store should be designed as something downstream systems can *query*, not just an internal log.
- **Corrective:** trigger a re-run of the failed task; re-sync a connector
- **Ambitious:** open a draft PR with a proposed fix (defensible for narrow classes — a missed enum value, a type cast — reckless in general)

Feedback capture is not optional garnish: without a "was this right?" signal, RCA accuracy can never be tuned, and §8's class-13 self-correction never learns.

---

## 11. Use-Case Gallery

Concrete scenarios, to sanity-check that the abstractions above pay for themselves:

| Scenario | Topology | Check | Likely root cause |
|---|---|---|---|
| Orders missing in the mart after a dbt refactor | stage-to-stage | key-set diff | inner join replaced left join (class 7) |
| Revenue off by 100× | ingestion | per-column sum | cents vs dollars (class 8) |
| Salesforce opportunities stale for 3 days | SaaS → DW | freshness + count | connector paused on auth expiry (class 10) |
| Dashboard total ≠ SQL total | DW → BI | aggregate parity | extract cached / filter in BI layer |
| One region's data silently absent | fan-in | count per region | one source feed stopped, union still non-empty (class 1) |
| Historical figures changed without notice | self-parity across time | checksum of frozen partitions | unannounced backfill (class 6) |
| Migration cutover risk | dual-run | full column-level diff | dialect semantics (NULL ordering, integer division) |
| Marketing audience shrank 40% | reverse-ETL egress | destination count | sync partial failure (class 10) |
| Duplicate payouts | invariant | PK uniqueness | non-idempotent retry (class 5) |
| Vendor invoice ≠ internal spend | external reconciliation | sum with tolerance | timezone-of-day attribution / restatement |
| GL doesn't tie to subledger | business invariant | balance sums to zero | dropped adjustment rows |
| New `status` value dropped silently | contract | enum domain | source behaviour change (class 4) |
| PR would break parity | refactor regression, in CI | diff old vs new | caught **before** merge |

The last row is the strategic one: the same engine, moved from cron to CI, changes the product from detective to preventive.

---

## 12. Scope Tiers

**T0 — V1 as written in PLAN.md.** Intra-Snowflake, one table pair, row count + checksum, git blame, Jira, cron. Correct starting point. Two additions I'd argue belong in T0 because they're cheap now and structural later: (a) the canonicalization layer (§6.1), (b) two independent connection handles in the `Check` interface (§2.2). Both are hours of work now and refactors later.

**T1 — credibility.** Grace-window re-verify before alerting (§6.2); tolerance/severity model (§6.4); on-failure localization via bisecting hash + sampled cell diff (§4.D/F); hypothesis-class taxonomy (§8); Slack feedback buttons (§10); correlated-incident grouping (§7). This tier is what determines whether anyone trusts the tool; it contains no new connectors.

**T2 — breadth of source.** Connector abstraction + `source_type`/`target_type`; Postgres/RDS → Snowflake; S3/Iceberg; snapshot-and-land for one SaaS source; federated pushdown where available. Plus PII/egress policy (§6.6) before this ships, since cross-system means data movement.

**T3 — breadth of check.** Freshness, null-rate, distribution drift with seasonality baselines, schema contracts, SCD2 and business invariants, fanout guards.

**T4 — breadth of signal.** Orchestrator logs, dbt manifest/run_results, ingestion-tool APIs, Snowflake `ACCESS_HISTORY` lineage, historical-incident retrieval, business calendar.

**T5 — leverage.** CI/pre-merge regression mode; circuit breaker / quarantine; catalog and BI annotation; check auto-generation from lineage; migration dual-run mode as a packaged offering.

**T6 — speculative.** Auto-remediation PRs; learned thresholds; natural-language check authoring; cost-optimization advisories.

Dependency note: T1 before T2. Widening sources before the noise-control layer exists multiplies false positives across more surfaces and burns trust exactly once.

---

## 13. Competitive / Build-vs-Buy Reality

Worth stating plainly, since it shapes where to spend effort:

- **Datafold data-diff** (open source lineage exists) already does cross-database bisecting-hash diffing well. Consider it as a *component* for §4.D/E rather than reimplementing — reimplementing hierarchical hashing across dialects is weeks of subtle work.
- **Great Expectations / Soda / dbt tests** cover the single-dataset invariant space (§5) declaratively. Wrapping or emitting to them may beat authoring a check DSL.
- **Monte Carlo / Bigeye / Metaplane / Elementary** do anomaly detection + lineage + alerting, some with LLM summaries.
- **What none of them do well:** grounded multi-signal RCA that names a specific commit, classifies the failure, resolves the right human, and files a properly-assigned, deduped ticket that closes itself. That is the defensible core, and it's the part that benefits most from an agentic approach.

So: buy/borrow the comparison engine where practical; build the causation and routing engine.

---

## 14. Open Questions That Change the Design

Answering these reshapes priorities materially:

1. **Which pain is actually being solved — ingestion fidelity, transform regressions, migration risk, or financial reconciliation?** Each favours a different Tier-A pair and a different first connector.
2. **Is there a migration or major refactor in flight?** If yes, dual-run parity (§3.3) or CI regression mode (§3.4) is a stronger V1 than cron monitoring, with a built-in deadline and an eager user.
3. **What non-Snowflake system is most likely to be the second one?** Postgres, S3, or Salesforce lead to quite different connector work.
4. **Do the pipelines have a reliable `updated_at` / watermark?** If not, §6.2 gets much harder and snapshot-based comparison becomes mandatory.
5. **Is there an orchestrator (Airflow/dbt) at all?** Its absence is why git blame is the ownership signal in V1; its presence would immediately be the best RCA signal available.
6. **What's the data-governance stance on sending sample values to an LLM?** Determines whether §6.6 is T0 or T2, and whether hosting must be Bedrock/Vertex.
7. **Realistic incident volume today?** Ten a month favours quality and depth; hundreds favours grouping, suppression, and auto-generation.
8. **Who owns the fix — a platform team with an on-call rotation, or individual authors?** Decides whether ownership resolution should target people or queues (§9).
9. **Is prevention (CI gate) politically viable, or only detection?** A blocking check needs organizational buy-in that a cron job doesn't.
10. **Is this an internal tool or a potential product?** Multi-tenancy, per-customer connectors, and secret isolation are cheap to plan for and painful to add.
