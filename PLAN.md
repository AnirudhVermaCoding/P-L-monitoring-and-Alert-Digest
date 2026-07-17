# Implementation Plan — Prozo Case Study 3: Daily FC P&L Monitor & Alert Digest

**Stack (decided):** Python 3.14 venv · Streamlit UI (simple) · LangGraph agentic pipeline · Grok (x.ai) via `langchain-xai` for plain-language insights · SMTP email alerts with file-outbox fallback · SQLite for run history / memory.

**Non-negotiable constraints from the brief:**
- Evaluators run WITHOUT paid API keys → every LLM/email step needs a deterministic free fallback.
- No secrets in repo → `.env` + `.env.example`, `.gitignore` from Phase 1.
- Bad input must produce clear messages, not crashes.
- Long runs must not freeze the UI; user can leave and come back (persist results to disk).
- Test run must demonstrate: 1 clean day, 1 CM2 breach, 1 line-item-only breach, 1 suspicious Blue day.

---

## Phase 0 — Documentation Discovery (COMPLETED — findings below are verified, use as-is)

### Allowed APIs (verified July 17, 2026)

**LangGraph 1.2.9** (pure-python, works on 3.14; do NOT install `langgraph-cli[inmem]` — broken on 3.14):
```python
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict

class State(TypedDict): ...
builder = StateGraph(State)
builder.add_node("name", fn)           # fn(state) -> partial state dict (merged)
builder.add_edge(START, "name")
builder.add_conditional_edges("a", router_fn)   # router_fn(state) -> next node name
builder.add_edge("name", END)
graph = builder.compile()              # MUST compile
result = graph.invoke({...})
```
Docs: https://docs.langchain.com/oss/python/langgraph/graph-api
Anti-patterns: `set_entry_point()`/`set_finish_point()` are legacy — use START/END edges.

**Grok via langchain-xai 1.2.2:**
```python
from langchain_xai import ChatXAI
llm = ChatXAI(model="grok-4.3", temperature=0)   # reads env XAI_API_KEY
```
- Base URL (if using ChatOpenAI instead): `https://api.x.ai/v1`.
- Model IDs: `grok-4.3` (cheapest, use it), `grok-4.5` (flagship). **`grok-3-mini`, `grok-4-fast` are RETIRED** — never hardcode them. Optionally verify live via `GET https://api.x.ai/v1/models`.
- Free tier: ~$25 signup credits (+data-sharing opt-in credits) — document in README but code must not require a key.
Docs: https://docs.langchain.com/oss/python/integrations/providers/xai

**Streamlit 1.59.2** (officially supports Python 3.14):
- Background work: plain `threading.Thread` that touches NO `st.*` calls; write progress/results to a module-level store + SQLite; poll from UI with `@st.fragment(run_every=2)`. Docs: https://docs.streamlit.io/develop/concepts/design/multithreading and .../execution-flow/st.fragment
- Upload: `st.file_uploader(label, type=["csv","xlsx"])` → file-like → `pd.read_excel/read_csv`.
- Download: `st.download_button(label, data=bytes, file_name=..., mime="text/csv" | "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")`.

**Python 3.14 note:** streamlit, pydantic 2.13+, langchain-core 1.4+ officially support 3.14; langgraph core and plotly are pure-python and work. If any `pip install` fails on 3.14, fall back to Python 3.13 in the venv — record it in README.

### Sample data facts (verified by direct inspection of `Case Study 3.xlsx`)
- Sheet `Ideal FC Criteria`: 6 line items with target %-of-revenue ranges (header junk in first 2 rows/col): Manpower 25–27%, Packaging 5–7%, Power and Fuel 10–12%, FC Rent 15–18%, Equipment Rentals 5–8%, Overheads 10–12%.
- Sheet `Day Wise PnL`: header on Excel row 2 (pandas `header=1`, drop all-NaN col 0). Columns: `Day, Revenue, Manpower, Packaging, Power and Fuel, FC Rent, Equipment Rentals, Overheads`. 100 rows, Day 1–100, **single FC, no FC column, no dates**.
- Computed profile: CM1% range 60.0–70.4 (mean 65.4); CM2% range 9.6–28.7. Color bands present in sample: 3 Red (<12), 11 Yellow-low, 86 Green, **0 Yellow-high, 0 Blue (>33)** → Blue scenario MUST come from generated test data. Line-item breaches are plentiful (e.g., Manpower above-max on 67 days).
- Column-name mapping needed: brief says "Manpower Cost", "Packaging Cost", "Power & Fuel" but the sheet uses "Manpower", "Packaging", "Power and Fuel" → ingestion must normalize aliases.

### Business rules (from brief — implement exactly)
- CM1 = Revenue − Manpower − Packaging. CM2 = CM1 − (Power & Fuel + FC Rent + Equipment Rentals + Overheads). Report both as % of revenue.
- Flag a day when: any cost line outside its target range, OR CM1% deviates >±3pp from 65–70 band (i.e., <62 or >73), OR CM2% deviates >±3pp from 15–30 band (i.e., <12 or >33).
- Color: Red CM2 < 12; Yellow 12–14.9 or 30–32.9; Green 15–30; Blue > 33 (suspicious — check underreported costs).

---

## Phase 1 — Scaffold + deterministic P&L engine

**What to implement** (all paths relative to repo root `C:\Users\bhavy\OneDrive\Desktop\Prozo_Case_Study_3`):
1. Files: `requirements.txt`, `.gitignore` (`.env`, `venv/`, `__pycache__/`, `outputs/outbox/`, `*.db`), `.env.example` (XAI_API_KEY, SMTP_HOST/PORT/USER/PASSWORD, EMAIL_MODE=outbox), `config.yaml` (target ranges, CM bands, color thresholds, line-item owner emails, FC manager emails).
2. `data/Case Study 3.xlsx` — copy from `C:\Users\bhavy\Downloads\Case Study 3.xlsx`.
3. `core/loader.py` — read xlsx/csv; header sniffing (handle the `header=1`, all-NaN col layout AND a clean-header layout); column alias normalization (`Manpower Cost`→`Manpower`, `Power & Fuel`→`Power and Fuel`, case/space-insensitive); optional `FC` column (default `FC-1`); optional `Date` column (else Day number). Raise `LoaderError` with a human message on: empty file, missing required columns, non-numeric revenue, zero/negative revenue rows (flag, don't crash).
4. `core/engine.py` — pure functions, no I/O:
   - `compute_pnl(df, targets) -> DataFrame` adding per-line % of revenue, CM1, CM1%, CM2, CM2%, color.
   - `detect_anomalies(pnl_df, targets) -> DataFrame` with columns `Date | FC | Line Item | % of Revenue | Target Range | Status` (Status: ABOVE_MAX / BELOW_MIN / CM1_BREACH / CM2_BREACH / SUSPICIOUS_HIGH). CM rows use Line Item = "CM1%"/"CM2%".
5. `core/venv setup`: `py -3.14 -m venv venv` (fallback 3.13), install `streamlit langgraph langchain-xai pandas openpyxl plotly python-dotenv pyyaml`.
6. `tests/test_engine.py` (plain `assert` via pytest or a `python -m tests.test_engine` script) — hand-computed expectations for Day 1 of the sample (Revenue 112453, Manpower 34221, Packaging 8769 → CM1 = 69463, CM1% ≈ 61.77 → CM1 breach since <62) plus a synthetic clean row and a Blue row.

**Verification checklist:**
- `venv\Scripts\python -c "import streamlit, langgraph, langchain_xai"` succeeds.
- Engine run over the sample file reproduces Phase-0 profile: 3 Red days, 86 Green, CM2% min 9.6 / max 28.7.
- Loader gives readable errors for: empty CSV, missing `Revenue`, a text file renamed `.xlsx`.

**Anti-pattern guards:** no `st.*` imports in `core/`; no hardcoded absolute paths inside `core/` (pass paths in); don't invent pandas params — `pd.read_excel(f, sheet_name=..., header=1)` only.

---

## Phase 2 — Test data extension (multi-FC scenarios)

**What to implement:**
1. `data/make_test_data.py` — deterministic (seeded) generator producing `data/extended_test_data.csv` with columns `Date, FC, Revenue, Manpower, Packaging, Power and Fuel, FC Rent, Equipment Rentals, Overheads`:
   - FC-Delhi: the 100 sample days mapped to dates ending at the run date.
   - FC-Mumbai: ~14 days engineered to include ≥1 fully clean day (every line inside range AND CM1/CM2 in band — derive by picking mid-range percentages of revenue, e.g. Manpower 26%, Packaging 6%, P&F 11%, Rent 16.5%, Equip 6.5%, OH 11% → CM2 = 23%), ≥1 Red CM2 breach day, ≥1 line-item-only breach day (one line out of range while CM2 stays 15–30 — e.g. Packaging 9% offset by Rent 13.5%... careful: Rent below-min is itself a breach; instead push Packaging to 8.5% and pull Overheads to 10.0% (in range) and Equipment to 5.0% (in range) so only Packaging breaches and CM2 ≈ 21%), ≥1 Blue day (understate several costs so CM2 ≈ 36%).
   - Verify each engineered day by running `core.engine` inside the generator and asserting the intended flags — the generator fails loudly if scenarios drift.
2. Also emit `data/bad_inputs/` samples: `empty.csv`, `missing_columns.csv`, `garbage.xlsx` (text bytes) for demo of error handling.

**Verification:** run generator; assert printout lists the 4 required scenario days with FC + date; engine confirms them.

**Anti-pattern guards:** no randomness without a fixed seed; don't hand-type CSV rows without engine-verifying them.

---

## Phase 3 — LangGraph agentic workflow

**What to implement:** `agent/graph.py` using EXACTLY the Phase-0 verified API.
1. State: `TypedDict` with `raw_df, targets, pnl_df, anomalies_df, insights (list[dict]), digests (dict[fc, str]), notifications (list[dict]), errors (list[str]), llm_used (bool), progress (str)`.
2. Nodes (each updates `progress` so the UI can show live status):
   - `ingest` → loader; on LoaderError put message in `errors` and route to END via conditional edge.
   - `compute` → engine.compute_pnl.
   - `detect` → engine.detect_anomalies.
   - `insight` → per anomalous day+FC, produce plain-language insight: deviation size, breached lines, top contributors sorted by impact (pp deviation × revenue), recommended action. Try `ChatXAI(model="grok-4.3")` with a strict prompt (facts computed deterministically are injected; LLM only phrases them); on missing `XAI_API_KEY`/API error → `fallback_insight()` template producing the same fields (matches the brief's example sentence shape). Set `llm_used` accordingly. Batch: ONE LLM call per FC-day with all its anomalies, and cap total calls (e.g. 20) to stay in free tier.
   - `digest` → per-FC daily color-coded digest text (subject: `[GREEN/RED/...] FC-x — YYYY-MM-DD CM2 12.3%`).
   - `notify` → routing table from `config.yaml`: each breached line item → its owner email; digest → FC manager email; append send-records to `notifications`.
3. Graph: START→ingest→(conditional: errors→END)→compute→detect→insight→digest→notify→END.
4. CLI runner `run_pipeline.py` so everything works headless (also useful for the demo + evaluators).

**Documentation references:** LangGraph pattern and ChatXAI snippet in Phase 0 above — copy them verbatim.

**Verification:** `venv\Scripts\python run_pipeline.py data/extended_test_data.csv` with no XAI key → completes, `llm_used=False`, insights present for every anomaly day, digest text contains color + CM2%. With key (if available) → LLM path works. Bad inputs → clean error strings, exit code 0 with message, no traceback.

**Anti-pattern guards:** LLM must NEVER compute numbers (all figures injected pre-computed); no `set_entry_point`; nodes return partial dicts, never mutate state in place and return None.

---

## Phase 4 — Notifications (email with free path)

**What to implement:** `agent/notifier.py`
1. `EMAIL_MODE=outbox` (default): write each email as a markdown/`.eml` file to `outputs/outbox/<date>/<recipient>__<subject>.md` — zero-cost, evaluator-friendly, shown in UI.
2. `EMAIL_MODE=smtp`: `smtplib.SMTP` + `email.message.EmailMessage`, host/port/user/password from env (README: Gmail app-password free path, port 587 STARTTLS). HTML body with color chip. Any SMTP failure → log + automatic fallback to outbox, never crash the pipeline.
3. Recipient routing from `config.yaml`: `owners: {Manpower: ops.manpower@example.com, ...}` and `fc_managers: {FC-Delhi: ..., default: ...}` — editable in the UI (Phase 6).

**Verification:** run pipeline in outbox mode → files exist, one digest per FC per day flagged + one per line-item breach grouped per owner per day (group to avoid spam: one email per owner per day listing all their breached days/FCs); SMTP mode with bad creds → warning captured, outbox fallback files still produced.

**Anti-pattern guards:** no credentials in code or config.yaml (env only); never send real mail in tests/default config (example.com addresses + outbox default).

---

## Phase 5 — Memory, overrides, trends (the "gets smarter" story)

**What to implement:** `core/memory.py` (SQLite, file `pnl_monitor.db`):
1. Tables: `runs(run_id, ts, source_file, status, summary_json)`, `daily_results(run_id, fc, date, cm1_pct, cm2_pct, color, anomalies_json)`, `overrides(fc, date, line_item, action, note, ts)` — override actions: `acknowledge` (mute alert), `false_positive`, `adjust_target(min,max)` per FC.
2. Trend features computed at digest time from history: 7-day rolling CM2%, consecutive-breach streaks ("3rd Red day in a row — escalate"), line item drifting toward its limit for 3+ days ("early warning"). Include in digest + insights.
3. Overrides applied in `detect`: acknowledged anomalies marked `MUTED` (kept in log, excluded from emails); per-FC adjusted targets take precedence over config.yaml.
4. README section "How it gets smarter": memory of past days (baseline vs. target — flag deviation from own baseline), manual overrides (feedback loop), trend tracking, and future: learned seasonality, owner-response tracking, auto-tuned thresholds.

**Verification:** run pipeline twice on overlapping data → second run's digest mentions streaks; add an override via function call → anomaly becomes MUTED and drops from outbox email.

**Anti-pattern guards:** SQLite accessed through one module with `check_same_thread=False` + a lock (Streamlit threads); no ORM dependency.

---

## Phase 6 — Streamlit UI (simple)

**What to implement:** `app.py` — single page + sidebar, minimal chrome:
1. Sidebar: file uploader (`type=["csv","xlsx"]`), "Use bundled sample/extended data" buttons, XAI key status indicator (from env only — NEVER a text input that gets stored), email mode selector, "Run analysis" button.
2. Run handling (copy Phase-0 threading pattern): button starts `threading.Thread(target=pipeline_worker)` writing progress + results to a module-level `JOBS` dict and SQLite; `@st.fragment(run_every=2)` poll shows progress ("Computing P&L… / Generating insights… 4/9"), survives page refresh by reloading last run from SQLite → "leave and come back" satisfied. Worker thread contains zero `st.*` calls.
3. Results tabs:
   - **Dashboard**: FC × date color grid (Styler background per color code), KPI row (days, red count, blue count), CM2% trend line (plotly) per FC.
   - **P&L**: full computed table, `st.download_button` CSV + XLSX.
   - **Anomalies**: the flagged log table + per-anomaly plain-language insight expander; download CSV.
   - **Notifications**: rendered outbox emails (or SMTP send log).
   - **Overrides**: `st.data_editor` over active anomalies → acknowledge/false-positive buttons; per-FC target range editor.
4. Bad input → `st.error(human message)` from LoaderError; nonsense question/no-op file → clear message.

**Verification:** `venv\Scripts\streamlit run app.py`; upload each bad-input file → friendly error; run extended data → all tabs populate; download buttons produce openable files; start run, refresh browser tab mid-run → results appear after completion.

**Anti-pattern guards:** no `st.*` inside worker thread (Phase-0 doc constraint); no `add_script_run_ctx` hack; don't block the main script with `thread.join()`.

---

## Phase 7 — End-to-end verification, outputs, README

1. Full clean-room test: fresh venv per README steps, timed <15 min; run headless pipeline + Streamlit on: original xlsx, extended CSV, all bad inputs.
2. Commit produced artifacts to `outputs/`: computed P&L xlsx, anomalies CSV, sample outbox emails, dashboard screenshot.
3. `README.md`: quickstart (venv, pip install, streamlit run — no key needed), free path explicitly (works with zero keys; optional XAI_API_KEY for LLM phrasing, optional Gmail app password for real email), architecture diagram (mermaid: LangGraph nodes), approach & why (deterministic math + LLM only for language — defendable), "gets smarter" section (Phase 5), known limitations honestly (single-currency, day-granularity, free-tier caps, SQLite single-node), what I'd improve.
4. Grep checks: `grep -r "XAI_API_KEY" --include="*.py"` shows env reads only; `grep -rE "grok-3|grok-4-fast"` → no hits (retired models); no `set_entry_point`; `.env` not tracked.
5. `git init` + initial commit (repo is not yet a git repo).

**Done criteria:** all brief outputs exist — per-day per-FC P&L, anomalies log with exact column set, plain-language insights, color digest via a real channel (email/outbox), central dashboard, README extensibility section, 4 required test scenarios demonstrably firing.
