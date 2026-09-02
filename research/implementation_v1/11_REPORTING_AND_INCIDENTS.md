# 11 — Reporting and Incident Lifecycle

Lane A runs unattended, so it needs machinery Lane B does not: deduplication, sign-offs,
ownership, and a shadow period. Lane B returns its result to the person who ran it and stops.

## 1. Incident fingerprinting and dedup

```python
fingerprint = sha256(
    check_id + "|" + "|".join(sorted(canonical(k) for k in failing_keys))
)
```

| Situation | Behaviour |
|---|---|
| Same fingerprint, open incident exists | Update it: `OCCURRENCE_COUNT += 1`, `LAST_SEEN_RUN_ID`, post a threaded Slack reply, add a Jira comment. **No new ticket.** |
| Same check, **different** failing rows | New fingerprint → new incident. New failures always get a fresh look. |
| Fingerprint disappears for 2 consecutive runs | Auto-resolve with a note. Never silently. |
| Fingerprint returns after resolution | Reopen the same incident, flag as recurrence — recurrence is a stronger signal than a first occurrence |

This is what makes "one break, one incident with updates" true by construction rather than by
policy, and it is why `SAMPLE_ROWS` and `FAILING_FINGERPRINT` are recorded on every result.

## 1b. Layer suppression — one incident at the earliest failing layer

Dedup happens twice, on two different axes. Section 1 collapses *the same failure across runs*.
This collapses *one failure across layers*, within a run.

The medallion DAG in `engine/layer_graph.py` is known, so this is a computation, not an inference:

```
L2 dedup_pick_rule_fidelity FAILS on SILVER.CUSTOMERS
   └─ every L3 check on GOLD.DIM_CUSTOMER, GOLD.FCT_ORDER, GOLD.MART_DAILY_REVENUE
      that is downstream of SILVER.CUSTOMERS is recorded with
      SUPPRESSED_BY = <the L2 incident id>
   └─ ONE incident is filed, at L2, listing the downstream objects it explains
```

Rules:

- Suppressed results are **still written to `CHECK_RESULTS`** with their full detail. Nothing is
  discarded; the dashboard shows them under the parent incident.
- Suppression only applies along a real lineage edge. Two unrelated failures in the same run stay
  two incidents.
- A downstream failure that is **not** explained by the upstream one — a different violation code
  on a different key set — is filed separately, with a note that an upstream incident is open.
- If the upstream incident is later marked a false positive, the suppressed downstream results are
  re-evaluated on the next run rather than being retroactively promoted.

This is what makes a thirty-check medallion cascade land as one Slack message.

## 2. Sign-offs and exceptions that expire

The design doc's four defences against stale exception lists, implemented:

1. **Expire on code change.** A sign-off records `BOUND_COMMIT_SHA` plus `WATCHED_PATHS`. Every
   run re-resolves those paths at HEAD; if they moved, the sign-off is expired and the incident
   reopens with a link to the PR that expired it.
2. **Row-level tracking.** A sign-off may carry a `FINGERPRINT`. Signing off on 12 known-bad rows
   does not sign off on the 13th.
3. **Both-sides git history.** Evidence includes commits on the transform and on the source DDL,
   which is how an intentional change is told apart from a one-sided fix.
4. **Ask the author in the PR.** The Lane B PR gate posts the difference and asks for intent
   while the author still remembers why.

A hard ceiling (`EXPIRES_AT`, default +90 days) sits behind all of that, so nothing lives forever
by accident.

## 3. Ownership routing

```
1. CODEOWNERS entry for the transform file that produces the failing table   → owning team
2. If absent: manifest `owner.team` for the project                          → owning team
3. git blame on the transform, last 90 days, weighted by lines touched       → likely author
```

**Jira issues are assigned to the team queue. The likely author is only mentioned**, with the
commit link and the phrase "likely related commit", never "caused by". Auto-assignment that reads
as blame is how a monitoring tool gets socially rejected (risk R6), and the tool's evidence is
correlational, so the wording should be too.

## 4. Slack (Block Kit)

```
🔴  DIM_CUSTOMER — history is being overwritten instead of versioned
    Project orders · Lane A · run 01JB… · first seen 2 runs ago · 47 keys affected

    Category      SCD_DEGRADED_TO_OVERWRITE   (confidence 0.88)
    Evidence      · commit 3ab77e1 "simplify customer merge" (2026-08-29, @sam)
                    changed sql/transforms/dim_customer.sql: MERGE ... WHEN MATCHED
                    THEN UPDATE replaced the type-2 insert branch
                  · 47 current rows have a tracked column differing from staging
                    with no version opened since 2026-08-29
                  · no schema change; no permission change; load history normal
    Checked       CUSTOMER_ID, NAME, SEGMENT, COUNTRY, CREDIT_LIMIT
    Excluded      _LOADED_AT, ETL_BATCH_ID, DW_UPDATED_AT (audit) · EMAIL (pii, hashed)
    Owner         @data-platform · likely related commit by @sam
    Jira          DPO-412

    [ Acknowledge ]  [ False positive ]  [ Snooze 7d ]  [ View sample rows ]
```

- Repeat occurrences are **threaded replies**, never new top-level messages.
- `View sample rows` opens a modal with redacted rows; PII columns show a hash prefix only, and
  the click is recorded in `LLM_EGRESS_LOG`-adjacent audit terms.
- `False positive` writes to `FEEDBACK` and directly feeds the precision metric. It is the
  cheapest honest signal we get, so it is one click.
- Every AI-written sentence is visually separated from evidence and labelled.

## 5. Jira (REST v3)

| Event | Action |
|---|---|
| New incident, severity high, `go_live: true` | Create issue in `owner.jira_project`, assign to team queue |
| Recurrence | Comment with run id and new occurrence count. No new issue |
| Sign-off | Transition to `On Hold` with the sign-off reason and its expiry |
| Auto-resolve | Transition to `Done` with a "no longer reproducing since run X" comment |
| Sign-off expired by a code change | Reopen with the expiring commit linked |

Issue body carries the evidence bundle, the compared/excluded column lists, the scope predicate,
and the exact SQL — so the reader can reproduce the finding without the tool.

## 6. Shadow mode

`project.yaml` `go_live: false` is the default and stays that way for a mandatory **two-week
shadow period** (assumption C3, risk R3).

| | Shadow | Live |
|---|---|---|
| Checks run | Yes | Yes |
| State written | Yes | Yes |
| Slack | `shadow_channel` only | `slack_channel` |
| Jira | **Never** | Yes |
| Dashboard | Yes, marked SHADOW | Yes |

Go-live requires a report the tool generates itself: incident volume per week, precision from
`FEEDBACK`, and the list of undeclared differences found (B4). Flipping `go_live` is a PR against
`project.yaml` — reviewable, attributable, revertible.

## 7. Lane B output

No incidents, no Jira, no dedup. Three formats from the same result object:

- **The Parity screen** — live progress via SSE, then differing rows and causing columns (default)
- **JSON over the API** — for orchestrators that want to call it themselves
- **A PR comment** plus a neutral check status, from the GitHub webhook path

Every Lane B output states: tiers reached, rows compared, cost spent against budget, transforms
applied, transforms expired, columns compared, columns excluded, and whether the result is
`COMPLETE` or `PARTIAL`.

## 8. What every report must contain

Non-negotiable, in all lanes and all channels:

1. **What was compared** — the column list.
2. **What was excluded, and why** — audit, PII-hashed, or transform-declared.
3. **What was read** — the scope predicate.
4. **What we could not check** — unvalidated objects and inconclusive results.
5. **What it cost.**

Coverage will never reach 100%. A green result means *everything we checked, on the columns we
compared, looks fine* — so exclusions and coverage always travel with it. Reporting a green
result without them is the failure mode this product was built to end.
