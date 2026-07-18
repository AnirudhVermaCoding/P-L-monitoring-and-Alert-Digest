"""Streamlit UI for the Daily FC P&L Monitor.

Simple, non-technical-friendly: upload a file (or use bundled data), click Run, watch
progress, view the dashboard / P&L / anomalies / notifications, download outputs, and
apply manual overrides.

Long runs happen on a background thread so the screen never freezes; progress is polled
with an st.fragment, and results are persisted to SQLite so you can leave and come back.
Per the Streamlit multithreading docs, the worker thread does NOT call any st.* function.
"""
from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

# override=True so the .env file is the single source of truth — otherwise a stale
# EMAIL_MODE/XAI_API_KEY left in the process environment would silently win over .env.
load_dotenv(override=True)

from agent.graph import run_pipeline  # noqa: E402
from core.config import load_config  # noqa: E402
from core.engine import latest_status_by_fc  # noqa: E402
from core.jobstore import get_job, start as start_thread  # noqa: E402
from core.memory import (  # noqa: E402
    add_override,
    get_history,
    get_overrides,
    init_db,
    record_daily_results,
    record_run,
    trend_note,
)

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
SCRATCH = REPO_ROOT / "outputs" / "_uploads"
RUN_DATE = "2026-07-17"

COLOUR_HEX = {"Red": "#e74c3c", "Yellow": "#f1c40f", "Green": "#2ecc71", "Blue": "#3498db"}

st.set_page_config(page_title="FC P&L Monitor", page_icon="📊", layout="wide",
                   initial_sidebar_state="expanded")

# On Streamlit Community Cloud there is no .env file — secrets are configured in the app's
# Secrets manager and surfaced via st.secrets. Mirror them into os.environ so the pipeline
# (which reads os.environ for XAI_API_KEY / EMAIL_MODE / SMTP_*) works identically to local.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:  # noqa: BLE001 - no secrets configured is fine (falls back to defaults)
    pass

init_db()


# --------------------------- background worker ---------------------------

def _start_job(source: str, criteria_source: str | None = None) -> str:
    """Kick off the pipeline on a daemon thread. The worker never touches st.*."""

    def worker(job_id: str) -> None:
        job = get_job(job_id)

        def progress(msg: str) -> None:
            job["progress"] = msg

        try:
            result = run_pipeline(source, run_date=RUN_DATE, criteria_source=criteria_source,
                                  progress_cb=progress)
            if result.get("errors"):
                job["status"] = "error"
                job["error"] = "; ".join(result["errors"])
                return
            # Persist to memory so results survive a page refresh.
            run_id = f"run-{job_id[:8]}"
            record_run(run_id, RUN_DATE, source, "ok",
                       {"days": len(result["pnl_df"]),
                        "anomalies": len(result["anomalies_df"]),
                        "llm_used": result["llm_used"]})
            record_daily_results(run_id, result["pnl_df"], result["anomalies_df"])
            job["result"] = result
            job["status"] = "done"
            job["progress"] = "Complete"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job["status"] = "error"
            job["error"] = f"Unexpected error: {exc}"

    return start_thread(worker)


# --------------------------- sidebar / inputs ---------------------------

with st.sidebar:
    st.header("📊 FC P&L Monitor")
    st.caption("Upload daily cost & revenue data, or use the bundled sample.")

    uploaded = st.file_uploader("Upload daily P&L (CSV or Excel)", type=["csv", "xlsx"])
    criteria_file = st.file_uploader(
        "Target ranges (optional — only if separate from the data)",
        type=["csv", "xlsx"],
        help="Leave empty if your ranges are a sheet in the data file, or to use config.yaml. "
             "The 'Ideal FC Criteria' sheet inside the data workbook is picked up automatically.",
    )

    st.markdown("**…or use bundled data:**")
    use_sample = st.button("Sample workbook (100 days, 1 FC)")
    use_extended = st.button("Extended test data (2 FCs, all scenarios)")

    st.divider()
    import os
    from agent.insights import resolve_llm
    llm_cfg = resolve_llm()
    st.caption(f"LLM: {'🟢 ' + llm_cfg['label'] if llm_cfg else '⚪ not set (template mode)'}")
    st.caption(f"Email mode: **{os.environ.get('EMAIL_MODE', 'outbox')}**")


# Resolve which source to run. Buttons fire True only on their click-rerun, so they're
# one-shot. st.file_uploader, however, returns the file on EVERY rerun — so we must guard
# it with a signature check, or the post-completion st.rerun() would relaunch the job in a
# loop.
def _save_upload(upload) -> str:
    """Persist an uploaded file to scratch and return its path."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    dest = SCRATCH / upload.name
    dest.write_bytes(upload.getbuffer())
    return str(dest)


# An optional separate criteria file travels with whichever data source is run.
criteria_path = _save_upload(criteria_file) if criteria_file is not None else None

source_to_run = None
if uploaded is not None:
    sig = f"{uploaded.name}:{uploaded.size}:{getattr(criteria_file, 'name', '')}"
    if st.session_state.get("last_upload_sig") != sig:
        st.session_state["last_upload_sig"] = sig
        source_to_run = _save_upload(uploaded)
elif use_sample:
    source_to_run = str(DATA_DIR / "Case Study 3.xlsx")
elif use_extended:
    source_to_run = str(DATA_DIR / "extended_test_data.csv")

if source_to_run:
    st.session_state["job_id"] = _start_job(source_to_run, criteria_path)
    st.session_state["source_name"] = Path(source_to_run).name


# --------------------------- progress polling ---------------------------

@st.fragment(run_every=1)
def _poll_running(job_id: str):
    """Auto-refreshing status line, active ONLY while a job is running.

    When the job finishes it triggers a full script rerun so the main body renders the
    result tabs (a fragment rerun alone does not re-execute the main body). Because this
    fragment is only mounted while status == running, polling stops once we're done —
    no perpetual reruns when idle.
    """
    job = get_job(job_id)
    if job is None:
        return
    if job["status"] == "running":
        st.info(f"⏳ {job['progress']}  —  you can leave this page; results are saved.")
    else:
        st.rerun()  # full rerun: render results or the error


def _progress_area():
    job_id = st.session_state.get("job_id")
    job = get_job(job_id) if job_id else None
    if job is None:
        return
    if job["status"] == "running":
        _poll_running(job_id)
    elif job["status"] == "error":
        st.error(f"❌ {job['error']}")
    elif job["status"] == "done":
        st.success("✅ Analysis complete.")
        st.session_state["last_result"] = job["result"]


def _current_result():
    job_id = st.session_state.get("job_id")
    job = get_job(job_id) if job_id else None
    if job and job["status"] == "done":
        return job["result"]
    return st.session_state.get("last_result")


# --------------------------- main panel ---------------------------

st.title("Daily FC P&L Monitor & Alert Digest")
st.caption("**Case Study 3** — Upload → Analyse → View FC colour status → Download report / Send digest")
_progress_area()

result = _current_result()

if result is None:
    st.markdown(
        "**How it works — 4 steps:**  \n"
        "1. **Upload** your daily P&L file in the sidebar (or click a bundled sample below it).  \n"
        "2. **Analyse** runs automatically once a file is chosen.  \n"
        "3. **View** each FC's colour status, main reason, and recommended action.  \n"
        "4. **Download** the report or review the digest emails."
    )
    st.caption(
        "Under the hood the monitor computes CM1/CM2, flags any day where a cost line or "
        "margin breaches its target, writes a plain-language alert, and routes emails to the "
        "right owner."
    )
    hist = get_history()
    if not hist.empty:
        st.subheader("Last recorded status (from memory)")
        st.dataframe(hist.tail(20), width="stretch")
    st.stop()

pnl = result["pnl_df"]
anoms = result["anomalies_df"]
insights = result["insights"]
notifications = result["notifications"]

# Two ways to read the same results: a plain-English Simple view (default, for a
# non-technical evaluator) and the full Advanced tabs. The block switcher sits on top.
view = st.segmented_control(
    "View", ["🟢 Simple", "🔧 Advanced"], default="🟢 Simple",
    label_visibility="collapsed", key="view_mode",
    help="Simple = one plain-English status card per FC. Advanced = full tables, charts, "
         "insights, notifications and overrides.",
)
view = view or "🟢 Simple"  # segmented_control returns None if the pill is deselected

if view == "🟢 Simple":
    summary = latest_status_by_fc(pnl, insights)

    st.subheader("FC colour status — latest day")
    counts = {c: sum(1 for s in summary if s["Colour"] == c)
              for c in ("Red", "Yellow", "Green", "Blue")}
    m = st.columns(4)
    m[0].metric("🔴 Red", counts["Red"], help="Margin severely compressed")
    m[1].metric("🟡 Yellow", counts["Yellow"], help="Borderline")
    m[2].metric("🟢 Green", counts["Green"], help="Healthy")
    m[3].metric("🔵 Blue", counts["Blue"], help="Suspiciously high — check for missing costs")

    for s in summary:
        chip = COLOUR_HEX.get(s["Colour"], "#888")
        with st.container(border=True):
            head = st.columns([2, 5])
            head[0].markdown(
                f"### {s['FC']}<br><span style='background:{chip};color:white;padding:3px 12px;"
                f"border-radius:6px;font-size:0.85rem'>{s['Colour']}</span>",
                unsafe_allow_html=True,
            )
            head[1].markdown(
                f"**Status:** {s['Colour']} · CM2 {s['cm2_pct']:.1f}%  \n"
                f"**Main reason:** {s['main_reason']}  \n"
                f"**Largest cost breach:** {s['largest_breach']}  \n"
                f"**Recommended action:** {s['recommended_action']}"
            )

    st.divider()
    st.subheader("Report & digest")
    dl1, dl2 = st.columns(2)
    dl1.download_button(
        "⬇️ Download P&L report (CSV)", pnl.to_csv(index=False).encode("utf-8"),
        "computed_pnl.csv", "text/csv", width="stretch",
    )
    dl2.download_button(
        "⬇️ Download anomalies log (CSV)", anoms.to_csv(index=False).encode("utf-8"),
        "anomalies_log.csv", "text/csv", width="stretch",
        help="Date | FC | Line Item | % of Revenue | Target Range | Status",
    )
    if notifications:
        mode = notifications[0].get("mode", "outbox")
        verb = "sent" if mode == "smtp" else "written to the demo outbox (no real email sent)"
        st.caption(f"✉️ {len(notifications)} digest / alert email(s) were {verb} during "
                   "analysis — one summary per owner. Preview below.")
        log_df = pd.DataFrame(
            [{k: n.get(k) for k in ("to", "subject", "kind", "status", "mode")}
             for n in notifications]
        )
        st.dataframe(log_df, width="stretch")
        preview = next((n for n in notifications if n.get("html")), None)
        if preview:
            with st.expander(f"Preview: {preview['subject']}"):
                components.html(preview["html"], height=480, scrolling=True)
    else:
        st.caption("✉️ No emails were needed — no material breaches on the latest day.")

    st.stop()  # Simple view is complete; skip the Advanced tabs below.

# ---- Advanced view: the full tabbed dashboard (unchanged) ----
tab_dash, tab_pnl, tab_anom, tab_notif, tab_over = st.tabs(
    ["📈 Dashboard", "🧮 P&L", "🚩 Anomalies", "✉️ Notifications", "⚙️ Overrides"]
)

with tab_dash:
    crit = result.get("criteria_info", {})
    if crit.get("source") == "input file":
        st.success(f"🎯 Target ranges read from the **input file** "
                   f"({len(crit.get('targets', {}))} line items) — overriding config.yaml.")
    else:
        st.caption("🎯 Target ranges: using **config.yaml** defaults "
                   "(no criteria table found in the input).")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Days analysed", len(pnl))
    c2.metric("🔴 Red days", int((pnl["Colour"] == "Red").sum()))
    c3.metric("🔵 Blue (suspicious)", int((pnl["Colour"] == "Blue").sum()))
    c4.metric("Insights", len(insights), help="LLM" if result["llm_used"] else "template")

    st.subheader("CM2% trend by FC")
    fig = px.line(pnl, x="Date", y="CM2 %", color="FC", markers=True)
    fig.add_hrect(y0=15, y1=30, fillcolor="green", opacity=0.06, line_width=0)
    fig.add_hline(y=12, line_dash="dot", line_color="red", annotation_text="Red < 12")
    fig.add_hline(y=33, line_dash="dot", line_color="blue", annotation_text="Blue > 33")
    st.plotly_chart(fig, width="stretch")

    st.subheader("Daily colour grid")
    grid = pnl.pivot_table(index="FC", columns="Date", values="Colour", aggfunc="first")

    def _style(v):
        return f"background-color: {COLOUR_HEX.get(v, '')}; color: white;" if v else ""

    st.dataframe(grid.style.map(_style), width="stretch")

    # Trend notes from memory.
    hist = get_history()
    notes = [f"**{fc}**: {trend_note(hist, fc)}" for fc in pnl["FC"].unique()
             if trend_note(hist, fc)]
    if notes:
        st.subheader("Trend memory")
        for n in notes:
            st.markdown("- " + n)

with tab_pnl:
    st.subheader("Computed P&L (per day, per FC)")
    st.dataframe(pnl, width="stretch")
    csv = pnl.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download P&L (CSV)", csv, "computed_pnl.csv", "text/csv")
    buf = BytesIO()
    pnl.to_excel(buf, index=False)
    st.download_button(
        "⬇️ Download P&L (Excel)", buf.getvalue(), "computed_pnl.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with tab_anom:
    st.subheader("Flagged anomalies log")
    st.caption("Date | FC | Line Item | % of Revenue | Target Range | Status")
    st.dataframe(anoms, width="stretch")
    st.download_button("⬇️ Download anomalies (CSV)",
                       anoms.to_csv(index=False).encode("utf-8"),
                       "anomalies_log.csv", "text/csv")

    st.subheader("Plain-language insights")
    for ins in insights:
        chip = COLOUR_HEX.get(ins["Colour"], "#888")
        with st.expander(f"{ins['FC']} — {ins['Date']}  ·  CM2 {ins['CM2 %']:.1f}% ({ins['Colour']})"):
            st.markdown(
                f"<span style='background:{chip};color:white;padding:2px 8px;"
                f"border-radius:4px'>{ins['Colour']}</span>",
                unsafe_allow_html=True,
            )
            st.write(ins["insight"])

with tab_notif:
    st.subheader("Notifications dispatched")
    n_cfg = load_config().notifications
    st.caption(
        f"Alerts are rolled up to **one summary email per owner**, scoped to "
        f"**{n_cfg.get('scope', 'latest_day').replace('_', ' ')}**, and owners are paged only "
        f"for breaches **≥ {n_cfg.get('materiality_pp', 1.0):g}pp** beyond range. Every breach "
        f"(material or not) is still in the Anomalies tab. Configure in `config.yaml`."
    )
    if notifications:
        log_df = pd.DataFrame(
            [{k: n.get(k) for k in ("to", "subject", "kind", "status", "mode", "detail")}
             for n in notifications]
        )
        st.dataframe(log_df, width="stretch")
        st.caption(f"{len(notifications)} messages. In 'outbox' mode these are written to "
                   "outputs/outbox/ — open a few below.")
        for n in notifications[:8]:
            with st.expander(f"{n['kind']} → {n['to']}: {n['subject']}"):
                if n.get("html"):
                    components.html(n["html"], height=520, scrolling=True)
                else:
                    st.code(n.get("body", ""), language="text")
    else:
        st.info("No notifications generated (no anomalies).")

with tab_over:
    st.subheader("Manual overrides")
    st.caption("Acknowledge a flagged item to mute its alerts on future runs "
               "(kept in the log, excluded from emails). This is how the system learns "
               "from human feedback.")
    if anoms.empty:
        st.info("No anomalies to override.")
    else:
        options = anoms.assign(
            label=lambda d: d["FC"] + " | " + d["Date"].astype(str) + " | " + d["Line Item"]
        )
        pick = st.selectbox("Select a flagged item", options["label"].tolist())
        note = st.text_input("Note (optional)", "expected — reviewed")
        col_a, col_b = st.columns(2)
        if col_a.button("✅ Acknowledge (mute)"):
            fc, date, item = [p.strip() for p in pick.split("|")]
            add_override(fc, date, item, "acknowledge", RUN_DATE, note=note)
            st.success(f"Muted {item} for {fc} on {date}. Re-run to see it excluded.")
        if col_b.button("🚫 Mark false positive"):
            fc, date, item = [p.strip() for p in pick.split("|")]
            add_override(fc, date, item, "false_positive", RUN_DATE, note=note)
            st.success(f"Marked false positive: {item} {fc} {date}.")

    ov = get_overrides()
    if not ov.empty:
        st.subheader("Active overrides")
        st.dataframe(ov[["fc", "date", "line_item", "action", "note", "ts"]], width="stretch")
