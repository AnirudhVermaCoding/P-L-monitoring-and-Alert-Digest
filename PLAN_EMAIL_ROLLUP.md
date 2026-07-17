# Plan — Daily-rollup + materiality email notifications

**Goal:** stop generating one email per breach (382 on the sample). Instead:
1. **One email per owner** summarising all their breaches (a daily digest for that owner), and
2. **Only page on material breaches** — trivial sub-threshold breaches still appear in the
   anomalies log and dashboard, but don't trigger an email.
3. **Scope alerts to the day being monitored** (latest day per FC) by default — a "Daily FC
   Monitor" alerts on today, not on 100 days of backfilled history. Configurable.

Net effect on the demo: ~382 owner emails → at most one per owner (≈3–6), plus one digest per FC.

**Key invariant:** materiality/scope affect **who gets emailed only**. The anomalies log
(`outputs/anomalies_log.csv`, Anomalies tab), the dashboard, and memory keep **every** breach.
Do not filter `detect_anomalies` output.

---

## Phase 0 — Anchors (no subagent; I authored this code — exact current state cited)

**Files & signatures being changed:**
- `agent/notifier.py:66` `_owner_messages(anomalies_df, config)` — currently loops
  `groupby(["FC","Date"])` and emits one message per (owner, FC, date). This is the thing to replace.
- `agent/notifier.py:91` `_digest_messages(digests, config)` — already one per FC; **leave as-is**.
- `agent/notifier.py` `dispatch(digests, anomalies_df, config, run_date)` (near line 150) — needs
  `pnl_df` added so it can find the latest day per FC. Called from `agent/graph.py` `notify_node`.
- `agent/graph.py` `notify_node` — calls `dispatch(...)`; must pass `state["pnl_df"]`.
- `core/config.py` `Config` dataclass + `load_config` — add an optional `notifications` section
  with safe defaults (backward-compatible if the YAML lacks it).
- `core/engine.py:113` `contributors_for_day` shows the deviation formula to reuse:
  `dev = pct - max` (above) or `min - pct` (below). Reuse this for materiality.
- Anomaly rows carry `Line Item`, `% of Revenue`, `Target Range`, `Status` (no numeric deviation
  column) — compute deviation in the notifier from `config.targets` + `% of Revenue`.
- Line-item anomalies are the rows where `Line Item` is NOT in `("CM1%","CM2%")`.

**Anti-patterns to avoid:** don't touch `detect_anomalies`; don't invent a `deviation_pp` column on
the anomalies frame (compute locally); don't change `_digest_messages`; keep the outbox/SMTP
fallback logic in `dispatch` intact.

---

## Phase 1 — Config

**`config.yaml`** — add:
```yaml
notifications:
  scope: latest_day        # latest_day = alert only on each FC's most recent day; all_days = whole batch
  materiality_pp: 1.0      # a line-item breach must exceed its range by >= this many pp to page an owner
  always_page_cm_breach: true   # CM1%/CM2% breaches always page (they're the headline), regardless of pp
```

**`core/config.py`** — add `notifications: dict` to the `Config` dataclass and populate it in
`load_config` with `data.get("notifications", {})` merged over defaults:
```python
DEFAULT_NOTIFICATIONS = {"scope": "latest_day", "materiality_pp": 1.0, "always_page_cm_breach": True}
```
So an old config.yaml with no `notifications:` section still works.

**Verification:** `load_config().notifications["scope"] == "latest_day"`; deleting the YAML section
still loads with defaults.

---

## Phase 2 — Notifier rewrite

**`agent/notifier.py`** — add two helpers and rewrite `_owner_messages`:

1. `_line_deviation(row, config) -> float`: for a line-item anomaly row, return pp beyond range
   using `config.targets[item]` and `row["% of Revenue"]` (mirror `contributors_for_day`).
2. `_in_scope_dates(pnl_df, scope) -> dict[fc, set[date]]`: if `scope=="latest_day"`, `{fc: {max date}}`
   per FC from **pnl_df** (so a clean latest day correctly yields no alerts); else all dates.
3. Rewrite `_owner_messages(anomalies_df, pnl_df, config)`:
   - Keep only line-item anomalies within the in-scope dates.
   - Keep only **material** ones: `_line_deviation(...) >= materiality_pp`. (CM rows are handled by
     the digest, not owner emails; `always_page_cm_breach` reserved for future per-owner CM routing.)
   - Group by **owner** (across FCs/dates). Emit **one** message per owner:
     - subject: `"[P&L Alert] {items} — {n} material breach(es) to review"` (items = the line item(s)
       that owner owns that fired).
     - body: grouped by FC → date → bulleted lines `"- {item}: {pct}% ({dir} by {dev:.1f}pp, target {range})"`,
       plus a one-line note that sub-threshold breaches are in the dashboard.
     - carry `kind="owner_alert"`, `to=owner`, and a representative `fc`/`date` for the log.

4. `dispatch(digests, anomalies_df, pnl_df, config, run_date)` — add `pnl_df` param; call the new
   `_owner_messages(anomalies_df, pnl_df, config)`. Everything else (outbox/SMTP + fallback) unchanged.

**`agent/graph.py` `notify_node`** — pass `state["pnl_df"]` into `dispatch(...)`.

**Documentation reference:** deviation formula at `core/engine.py:123-128`; current dispatch/outbox
logic already in `agent/notifier.py` — reuse verbatim.

**Verification checklist:**
- `python run_pipeline.py` on `data/extended_test_data.csv`:
  - owner_alert messages ≤ number of owners, and **exactly one per distinct owner** (assert no
    duplicate `to` among `kind=="owner_alert"`).
  - digest messages == number of FCs (unchanged).
  - Total notifications drops from 382 to single digits.
- `outputs/anomalies_log.csv` row count **unchanged** (materiality didn't drop log rows).
- A known sub-threshold breach (e.g. Delhi latest day Packaging ~0.7pp over) is **absent** from any
  owner email body but **present** in the anomalies log.
- A known material breach (Delhi latest day Manpower ~2.2pp over) **is** in the owner email.
- Bad input still routes to a clean error (unchanged).

**Anti-pattern guards:** don't drop rows from the anomalies frame returned by `detect_node`; don't
regress the SMTP→outbox fallback; owner grouping must be by owner, not by (owner, FC, date).

---

## Phase 3 — Surface it in the app + README

- **`app.py` Notifications tab:** add a caption stating the active scope + materiality (read from
  `config`), e.g. "Showing alerts for each FC's latest day; owners paged only for breaches ≥ 1.0pp.
  Full breach list is in the Anomalies tab." No logic change.
- **`README.md`:** move the "email volume" bullet out of *Known limitations* into the notifications
  description as an implemented feature (one summary email per owner, materiality threshold,
  latest-day scope, all configurable in `config.yaml`). Keep an honest residual note: threshold is
  global (not per-line-item), and there's no cross-run de-duplication/snooze yet.

**Verification:** README no longer claims per-breach emails; Notifications tab shows the caption;
`grep -n "one email per breach\|hundreds of" README.md` returns nothing stale.

---

## Phase 4 — Regenerate outputs, re-verify, commit

1. `rm -f pnl_monitor.db && rm -rf outputs/outbox && python run_pipeline.py` → confirm small email count.
2. Refresh `outputs/sample_outbox/` with the new (few) owner summaries + 2 digests.
3. `python -m tests.test_engine` still passes (engine untouched — sanity check).
4. Boot Streamlit, run extended data, confirm Notifications tab shows the reduced set + caption.
5. Grep guards: no changes to `detect_anomalies`; `_digest_messages` unchanged; no `deviation_pp`
   column added to any DataFrame.
6. Commit: "Roll up owner alerts into one summary email per owner + materiality threshold".

**Done criteria:** running the sample produces a handful of emails (one summary per owner for the
monitored day + one digest per FC) instead of 382; the anomalies log and dashboard remain complete;
behaviour is configurable in `config.yaml`; README and UI reflect it honestly.
