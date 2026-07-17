# Case Study 3 — Daily FC P&L Monitor & Alert Digest

An agentic workflow that watches each fulfilment centre's daily P&L, flags the moment a
margin or a cost line slips, explains **which line item caused it** in plain English, and
routes an alert to the person who owns that cost — with a colour-coded digest to each FC
manager and a central dashboard across all FCs.

Built with **Streamlit** (UI), **LangGraph** (agent pipeline), **Grok / x.ai** (optional
LLM phrasing), and **SQLite** (memory). **It runs with zero paid API keys** — the LLM and
email steps both have free, deterministic fallbacks.

---

## Quickstart (under 15 minutes, no keys needed)

Requires Python 3.12–3.14 on Windows/macOS/Linux.

```bash
# 1. From the repo root, create a virtual environment
py -3.14 -m venv venv          # Windows (or: python3 -m venv venv)
venv\Scripts\activate          # Windows  (macOS/Linux: source venv/bin/activate)

# 2. Install dependencies (~2–3 min)
pip install -r requirements.txt

# 3. (Optional) enable extras — everything works without this
copy .env.example .env         # macOS/Linux: cp .env.example .env

# 4. Launch the web app
streamlit run app.py
```

Then in the browser: click **"Extended test data (2 FCs, all scenarios)"** in the sidebar
→ watch progress → explore the Dashboard / P&L / Anomalies / Notifications / Overrides tabs.

**Prefer the command line?**
```bash
python run_pipeline.py                          # runs on data/extended_test_data.csv
python run_pipeline.py "data/Case Study 3.xlsx" # runs on the original sample
```
Outputs land in `outputs/` (computed P&L, anomalies log) and `outputs/outbox/` (emails).

---

## What it does (maps to the brief)

| Requirement | Where |
|---|---|
| Per-day, per-FC P&L: each cost as % of revenue, CM1, CM1%, CM2, CM2% | `core/engine.py` → **P&L tab**, `outputs/computed_pnl.csv` |
| Flagged anomalies log: `Date \| FC \| Line Item \| % of Revenue \| Target Range \| Status` | `core/engine.py:detect_anomalies` → **Anomalies tab**, `outputs/anomalies_log.csv` |
| Plain-language insight per anomaly (deviation, breached items, top contributors, action) | `agent/insights.py` → **Anomalies tab** |
| Daily colour-coded digest per FC to its manager, over a real channel | `agent/notifier.py` (email/outbox) → **Notifications tab** |
| Central dashboard across FCs | **Dashboard tab** (colour grid + CM2% trend) |
| "Gets smarter over time" | `core/memory.py` — see below |
| Web app: upload, see progress, view, download; large batch doesn't freeze; bad input handled | `app.py` |

### Business rules implemented
- **CM1** = Revenue − Manpower − Packaging. **CM2** = CM1 − (Power&Fuel + FC Rent + Equipment Rentals + Overheads). Both reported as % of revenue.
- A day is **flagged** when any cost line is outside its target %-of-revenue range, or CM1% deviates >±3pp from the 65–70% band (i.e. <62 or >73), or CM2% deviates >±3pp from the 15–30% band (i.e. <12 or >33).
- **Colour code (CM2%):** 🔴 Red < 12 · 🟡 Yellow 12–14.9 or 30–32.9 · 🟢 Green 15–30 · 🔵 Blue > 33 (suspicious — check for under-reported costs).
- Target ranges, margin bands, colours, and **who gets emailed for each line item** all live in `config.yaml` (editable by a non-technical user).

---

## Approach & why

**Deterministic math, LLM only for language.** Every number the business acts on — CM1/CM2,
percentages, deviations, top contributors, colour — is computed in pure Python
(`core/engine.py`) so it's auditable and reproducible. The LLM (Grok) is handed those facts
and asked only to *phrase* them. It never calculates. This is the defensible choice for a
finance tool: you can't have an LLM inventing a margin, and it means the system produces
identical, correct numbers whether or not an API key is present.

**LangGraph pipeline.** The workflow is a small state graph — `ingest → compute → detect →
insight → digest → notify` — with a conditional edge that routes bad input straight to a
clean error instead of crashing (`agent/graph.py`). Each node updates a `progress` field so
the UI can show live status. LangGraph makes the stages explicit and easy to extend (e.g.
add a "escalate" node) and keeps the LLM step isolated and swappable.

**Free path is the default, not an afterthought.**
- **No `XAI_API_KEY`** → insights come from a deterministic template that produces the same
  structure as the LLM (deviation, breached items, ranked contributors, recommended action).
- **`EMAIL_MODE=outbox`** (default) → each email is written to `outputs/outbox/<date>/` as a
  readable file. No mail account, no cost. Set `EMAIL_MODE=smtp` with a free Gmail
  app-password to actually send; any SMTP failure automatically falls back to the outbox so
  the pipeline never breaks on a mail problem.

**Non-technical, non-freezing UI.** Streamlit with a background worker thread: the pipeline
runs off the main thread (the worker never calls `st.*`, per Streamlit's threading guidance),
progress is polled with an `st.fragment`, and every result is persisted to SQLite — so a user
can start a run, close the tab, and come back to find it done.

### Notification routing (as requested)
When a cost line breaches its range, the **owner of that line item** is emailed
(`config.yaml → owners`): Manpower → ops.manpower, Packaging → ops.packaging, and so on.
Each FC manager separately receives the daily colour digest (`config.yaml → fc_managers`).

To keep this signal-not-noise, owner alerts are **rolled up and thresholded**
(`config.yaml → notifications`):
- **One summary email per owner**, listing all their breaches — never one email per breach.
- **Materiality threshold** (`materiality_pp`, default 1.0): an owner is paged only when a line
  item is at least that many percentage points beyond its range. Smaller breaches still appear in
  the anomalies log and dashboard — they just don't page anyone.
- **Scope** (`scope`, default `latest_day`): a daily monitor alerts on each FC's most recent day.
  Set `all_days` to summarise an entire backfilled batch (still one email per owner).

On the 100-day sample this turns ~380 would-be emails into a handful (one summary per breaching
owner for the monitored day, plus one digest per FC).

---

## How this gets smarter over time

The `core/memory.py` SQLite layer is the foundation:

- **Memory of past days** — every day's CM1/CM2/colour/anomalies is stored (`daily_results`).
  Results survive restarts and power the "last recorded status" view and trend notes. The
  next step is baseline-vs-target: flag a day that's normal against target but abnormal
  against *this FC's own history*.
- **Manual overrides** — a manager can **acknowledge** or mark a flag a **false positive** in
  the Overrides tab. Muted items stay in the log but drop out of future emails
  (`agent/graph.py:detect_node` applies them). Per-FC target adjustments are supported by the
  schema. This is a human-in-the-loop feedback signal that should, over time, auto-tune
  thresholds.
- **Trend tracking** — `trend_note()` computes a 7-day rolling CM2% and consecutive
  non-green **streaks** ("3rd non-green day in a row — consider escalation"), surfaced in the
  dashboard and digests. Natural extensions: drift detection (a line item creeping toward its
  limit for N days = early warning), day-of-week seasonality, and tracking whether owners
  actually resolved past alerts.

---

## Test data

- `data/Case Study 3.xlsx` — the provided sample (100 days, single FC).
- `data/extended_test_data.csv` — generated by `data/make_test_data.py`, adds a second FC
  (FC-Mumbai) engineered so a single run demonstrates **all four required scenarios**, each
  verified by the real engine before it's written:
  - **Clean day** — 2026-07-16 (every line in range, CM2 23%)
  - **CM2 breach (Red)** — 2026-07-10 (CM2 4%)
  - **Line-item-only breach** — 2026-07-04/-08 (Packaging out of range, CM2 stays healthy)
  - **Suspicious high (Blue)** — 2026-07-14 (CM2 39%, under-reported costs)
- `data/bad_inputs/` — empty file, missing-columns file, corrupt "xlsx" — for the error path.

Regenerate with `python -m data.make_test_data`.

Produced outputs are in `outputs/` (`computed_pnl.csv/.xlsx`, `anomalies_log.csv`) and a
curated set of example emails in `outputs/sample_outbox/`.

---

## Tests

```bash
python -m tests.test_engine
```
Hand-computed checks for CM1/CM2, colour bands, the four scenarios, and contributor ranking.

---

## Optional: enabling Grok and real email

Both are optional; the app is fully functional without them.

- **Grok (x.ai):** create a key at https://console.x.ai (free signup credits; more via the
  data-sharing opt-in). Put it in `.env` as `XAI_API_KEY=...`. Default model is `grok-4.3`
  (cheapest text model). The sidebar shows whether a key was found.
- **Email (SMTP):** set `EMAIL_MODE=smtp` and fill `SMTP_*` in `.env`. Free path: a Gmail
  account with an [App Password](https://support.google.com/accounts/answer/185833) on port 587.

Secrets live only in `.env`, which is gitignored. No keys are committed.

---

## Known limitations & what I'd improve with more time

- **Notification tuning is global.** Owner alerts are now rolled up to one summary email per
  owner with a materiality threshold and day-scope (see Notification routing), which keeps the
  count small. Remaining gaps: the threshold is a single global `materiality_pp` rather than
  per-line-item, and there's no cross-run de-duplication/snooze — re-running the same day would
  re-notify. A production version would persist "already paged" state and support per-owner
  quiet hours.
- **Dates.** The sample has no dates, only day numbers; I map FC-Delhi's days onto a real
  calendar ending 2026-07-17 for realism. Real feeds should carry actual dates.
- **Single-node SQLite.** Fine for one machine / one operator; a multi-user deployment would
  want Postgres and a proper job queue (Celery/RQ) instead of a daemon thread.
- **LLM phrasing isn't verified.** The numbers are trustworthy (computed in Python), but the
  LLM's *wording* isn't independently checked; a guardrail pass could confirm it didn't
  contradict the facts.
- **Currency/units** are assumed consistent across the sheet; no multi-currency handling.
- **Overrides** currently key on (FC, date, line item); a rule-based override ("always mute
  Equipment Rentals below-min for FC-Delhi") would scale better than per-day acknowledgements.

---

## Project layout

```
app.py                    Streamlit UI (upload, progress, tabs, downloads, overrides)
run_pipeline.py           Headless CLI runner
config.yaml               Business rules + email routing (edit me)
core/
  config.py               Load/validate config.yaml
  loader.py               Tolerant CSV/Excel ingest + clear error messages
  engine.py               CM1/CM2, %-of-revenue, colour, anomaly detection (pure)
  memory.py               SQLite: runs, daily results, overrides, trends
  jobstore.py             Process-wide background-job store (survives reruns)
agent/
  graph.py                LangGraph pipeline (ingest→compute→detect→insight→digest→notify)
  insights.py             Plain-language insight (Grok + deterministic fallback)
  notifier.py             Digests + email delivery (outbox / SMTP)
data/
  Case Study 3.xlsx       Provided sample
  make_test_data.py       Scenario generator (engine-verified)
  extended_test_data.csv  Generated multi-FC test data
  bad_inputs/             Corrupt/empty/missing-column files for the error path
tests/test_engine.py      Hand-checked engine tests
outputs/                  Produced P&L, anomalies log, sample emails
```
