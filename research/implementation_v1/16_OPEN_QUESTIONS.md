# 16 — Open Questions and Decisions Needed

Ordered by when the answer is needed. Each says what we do if there is no answer, so nothing here
blocks a start — but the first block genuinely shapes the build.

## Block 0 — Scope reconciliation (needed before M0, and it may reshape M0)

`../PARITY_SCOPE.md`, `../final_idea/`'s design doc, and this directory describe **three different
environments**, and nothing on record says which one is real or when it changed. The parent design
doc is the *narrowest* of the three — intra-Snowflake, single-engine — so the RDS→Snowflake premise
at the top of `00_README` was introduced here, not inherited. Everything in Block 1 assumes the answer to Q0a is
"the RDS→Snowflake picture in these docs." If it is not, M0's foundations are wrong, not just
incomplete — so this block is genuinely first.

`PARITY_SCOPE.md` describes: API sources (Ringside, Tradable Bits, …) landing into a Snowflake
medallion, **plus a legacy RDS + dbt-on-Redshift estate being re-implemented in Snowflake.** It
calls the legacy↔new family (its **F4**) "ultimately the key" and the hardest, and devotes its
longest section to it. **None of F4 exists in these v1 docs**: no Redshift, no legacy↔new object
mapping, no `equivalent_to` relation, no migration readiness scorecard, no `expected_differences`
register. Nor does its **F1**: v1's L1 assumes a *queryable* RDS source, while `PARITY_SCOPE.md`
§4 argues at length that an API source has no queryable reference and needs raw-payload retention
or ingestion-manifest reconciliation instead.

| # | Question | Default if unanswered | Cost of the default being wrong |
|---|---|---|---|
| ~~Q0a~~ | ~~Which environment is real?~~ | **ANSWERED 2026-09-02: (a) RDS → Snowflake only.** The legacy Redshift/dbt estate in `PARITY_SCOPE.md` is **not** in this environment. Build exactly to these docs | — |
| ~~Q0b~~ | ~~Is legacy↔new parity in v1?~~ | **Moot** — Q0a settled it. `PARITY_SCOPE.md`'s F4 family, the `equivalent_to` relation, and the migration readiness scorecard are **out of scope**, and `PARITY_SCOPE.md` should be read as opportunity-space research, not as a requirement | — |
| **Q0c** | Are any monitored sources non-queryable APIs? | Assume not — every source is queryable RDS | v1's L1 has no reference to compare against. The fix (raw-payload retention in the extractor) is upstream of us and has its own lead time — `PARITY_SCOPE.md` §10.1 calls it "the first task" |
| ~~Q0d~~ | ~~Where is the parent design doc?~~ | **RESOLVED 2026-09-02** — it is in the repo at the cited path, and every citation in `00_README`, `09`, `11`, `13` checks out (§2.7 = "Handling expected differences", §5 = metadata-only authoring, "14 of 20 risks structural, R8 not" all verified) | — |
| **Q0e** | The design doc is **intra-Snowflake** (its A2: "both sources being compared live in the same engine"; "the initial implementation is being planned with snowflake as the warehousing Platform"). It **defers cross-engine comparison entirely**, and says that if a second engine appears we would **borrow** a diff engine, not build one. `02` D2 records A2 as false but still builds Lane B in-house, in v1, as M5. Is that reversal endorsed? | Proceed as `02` D2 says — build the tiered comparison narrowly | This is a real reversal of the design doc's build-vs-borrow call on the single most expensive component in v1, and it is **not currently listed as a deviation** in `02`. Now added as **D5**. If borrowing (e.g. Datafold data-diff, as `../SCOPE.md` §13 recommends) is still preferred, M5 shrinks substantially |

**Q0a is answered, so M0 is unblocked.** Q0c and Q0e remain: Q0c is cheap and upstream of us,
Q0e would visibly shrink M5. Neither blocks M0's foundations, because every engine touchpoint sits
behind the `Dialect` and `CatalogReader` protocols — a third engine, if one ever appears, is an
additive implementation rather than a redesign.

## Block 1 — Needed before M0

| # | Question | Default if unanswered | Cost of the default being wrong |
|---|---|---|---|
| Q1 | Can we get `DPHM_READER` (read-only, incl. `ACCOUNT_USAGE`) and `DPHM_WRITER` (two schemas) on Snowflake? | Blocked. Nothing works | — |
| Q2 | Can we get a read-only RDS role, and is the runner inside the VPC (or is there a bastion / VPC endpoint)? | Lane B is blocked; Lane A proceeds | Lose M5 entirely |
| Q3 | Which **RDS engine** — PostgreSQL or MySQL? Which version? | Assume PostgreSQL 13+ | Rewrite `catalog/postgres.py` and half of `parity/canonical.py` |
| Q4 | Is there a **read replica** we can point Lane B at? | Point at the primary with strict timeouts and row caps | Lane B becomes off-hours only, or gets refused by the DBA |
| Q5 | Repo access: deploy key or PAT, and can we clone in CI? | Authoring degrades to schema-only; no ownership routing, no commit-bound sign-offs | Large — assumption A3 |
| Q6 | **What is the reliable "batch finished" signal?** Control table, audit column, or can the load job call a hook? | `hook_only`, and checks go `INCONCLUSIVE` until a hook exists | Assumption A4. Without it, scheduled checks read mid-load and report false failures — the fastest way to lose trust |
| Q7 | Which **one dimension** is the M2 pilot? It must be a real SCD2 with a business key distinct from its surrogate key | Blocked — we need a named table | — |
| Q7a | Are the medallion layers actually three physical schemas (`BRONZE`/`SILVER`/`GOLD`), or are some logical/virtual (views, or bronze skipped entirely)? | Assume three physical Snowflake schemas | A collapsed hop means its checks have nothing to compare; the hop becomes `unvalidated` |
| **Q7b** | **Is the bronze→silver dedup expressed in SQL we can read** (`QUALIFY ROW_NUMBER()`, `DISTINCT ON`, a `MERGE`), or is it in Python/Spark? | Parse SQL only; a Python dedup makes the contract LLM-inferred and needs a firmer human confirmation | `07B` §6.1. If the pick rule cannot be read, L2.5 (pick-rule fidelity) cannot be generated automatically |
| **Q7c** | **What is the declared pick rule** — which duplicate is supposed to win, and is there a guaranteed-unique tiebreaker? | Assume `latest source_updated_at`, and report every tie as `L2_PICK_RULE_NOT_TOTAL` | If there is no total ordering, the pipeline is non-reproducible today and does not know it. This is a finding, not a blocker |
| Q7d | Is there a **reject table** for rows silver drops, with a reason column and a closed reason set? | Treat all loss as `dedup_collapse` + unexplained; conservation will fire until losses are named | Noisy first run at L2.4 |
| Q7e | Which gold marts have **non-additive measures** (averages, distinct counts, ratios)? | Infer: `sum`/`count` additive, everything else non-additive and reported unverified | Safe direction — worst case we under-claim coverage |

**Q6 is the one most likely to be answered with "sort of."** A vague answer here is worse than a
"no", because `hook_only` at least fails loudly.

## Block 1b — Needed before M1 (the app ships from M1 onward)

| # | Question | Default |
|---|---|---|
| **Q21** | Which **OIDC provider**, and can we register a client? Can we map groups to the four roles (Viewer / Engineer / Data Owner / Admin)? | Ingress auth only, roles collapsed to one — **and sign-off cannot go live**, because it would be unattributable |
| Q22 | Where does the service **host**? It needs VPC access to RDS, egress to Snowflake and the Anthropic API, and an HTTPS ingress | Assume the internal Kubernetes platform |
| Q23 | Is one replica acceptable for v1? (The worker pool is in-process — see `12` §5) | Yes. Scale-out is a dispatcher swap, deferred until there is a second project |

## Block 2 — Needed before M3 (reporting)

| # | Question | Default |
|---|---|---|
| Q8 | Slack workspace, a bot token with `chat:write` + `commands`, and the two channels (`alerts`, `shadow`) | In-app incidents only, no push. M3's exit criteria are unmeetable, and nobody finds out about a break unless they open the dashboard |
| Q9 | Jira project key, and can we create issues via a service account? | Slack-only reporting |
| Q10 | **Who is the named owner** who will act on Lane A reports? A person, not a team alias | Blocked for go-live. This is risk R8, the one risk the design doc admits is not handled structurally |
| Q11 | Is `CODEOWNERS` present and accurate in the pipeline repo? | Route everything to `owner.team` from config; no likely-author mention |
| Q12 | Is there a "golden window" — a batch range a human will vouch for? (gate 2 needs one) | Use the most recent 7 days with no known incident, and say so in the gate result |

## Block 3 — Needed before M5 (parity)

| # | Question | Default |
|---|---|---|
| Q13 | Can we add a **non-blocking** PR check to the pipeline repo? Can we install a GitHub App with a VPC-reachable webhook? | On-demand-only Lane B, from the app. Assumption C2; the biggest single capability loss |
| Q14 | What are the known deliberate RDS↔Snowflake divergences today? | Discover them in shadow mode as false positives, then declare them. Slower and noisier |
| Q15 | Largest table in scope, by row count and by bytes? | Assume ≤ 500M rows; `bucket_count = 1024`. If far larger, tier 0 buckets need to be finer |
| Q16 | Is the migration still in flight, or has it cut over? | Assume in flight — Lane B matters sooner, and M5 may move ahead of M4 |

## Block 4 — Needed before M6 (authoring)

| # | Question | Default |
|---|---|---|
| Q17 | Anthropic API access from the runner: direct, or does residency require Bedrock/Vertex? | Direct Anthropic. One-file change if wrong (`agents/llm.py`) |
| Q18 | Which domain expert gives 30–60 minutes for grain confirmation, and when? | Blocked at the interrupt. Assumption C3 |
| Q19 | Does a dbt project or a catalog exist? | Assume not. If it does, it is free lineage — a signal, never a requirement |
| Q20 | Where do generated PRs go — the pipeline repo or a separate config repo? | The pipeline repo, so sign-offs and code changes share a history |

## Decisions already made (recorded so they are not relitigated)

| Decision | Rationale |
|---|---|
| LangChain/LangGraph instead of the raw Anthropic SDK | Resumable HITL authoring and graph-enforced evidence ordering. See `02` D1 |
| LangChain confined to `agents/`, enforced in CI | The trust boundary is deterministic SQL; the framework must not creep into it |
| Cross-engine parity is tiered pushdown-then-land, not a general diff engine | Contains the scope explosion that assumption A2 being false would otherwise cause |
| Lane A stays 100% single-engine Snowflake | Where the value is; keeps the motivating bug catchable and cheap |
| State lives in Snowflake, not a new database | No new infrastructure to provision, secure, or back up |
| Mutation testing is a blocking merge gate | A check that cannot fail is worse than no check |
| Grain confirmation is a hard human interrupt | A wrong grain passes while comparing nothing — the most dangerous silent failure |
| Every medallion hop is checked, not just the endpoints | An RDS↔gold comparison is green in all three of the failure modes we most care about |
| The bronze→silver hop is anchored on an explicit **dedup contract** (key + total pick rule + named losses) | A dedup that keeps *an arbitrary* row per key passes every counting check and is silently non-reproducible |
| `dedup_collapse` in the conservation equation is **computed, not asserted** | A conservation check with an unconstrained slack term can never fail |
| "Earliest failing layer" is computed from the hop DAG, not inferred by the LLM | One upstream break must produce one incident, and that should be structural |
| Jira assigns to the team queue; the author is only mentioned | Auto-assignment reads as blame, and our evidence is correlational |
| PR gate is non-blocking in v1 | "No blocking a deploy without buy-in" — a design-doc principle |
| **Scope is RDS → Snowflake only.** No Redshift, no legacy↔new parity, no API-sourced ingestion. `../PARITY_SCOPE.md` is opportunity-space research, not requirements | Confirmed 2026-09-02 (Q0a). Keeps Lane B to one hop and one dialect pair |
| **RDS is PostgreSQL 13+** | Confirmed 2026-09-02 (Q3). MySQL stays a `Dialect` implementation nobody has written |
| **The entire user surface is a web app; there is no CLI** | The review queue is the critical path, and the domain expert who owns the grain answer will click a link, not run a command. See `02` D4 |
| FastAPI + React now, not Streamlit first | Once the dashboard is the whole surface — forms, approvals, RBAC, live progress — Streamlit is a rewrite we would do anyway |
| Structural changes go through a git PR; operational actions write to state | Keeps "every AI output is a reviewable artifact" true even with a UI in front of it |
| Sign-off requires the Data Owner role, not Engineer | If the monitoring team can sign off on its own alerts, the board gets rubber-stamped green |
| `UNKNOWN` is a valid classification | A fabricated root cause costs more trust than an honest shrug |

## Things we are deliberately not deciding yet

- Multi-project / multi-tenant deployment — revisit at project three.
- Anything beyond one replica of the service.
- Anomaly detection and learned thresholds — needs two to three quarters of run history first.
- Any second warehouse or second source engine — v1 is RDS → Snowflake only.
