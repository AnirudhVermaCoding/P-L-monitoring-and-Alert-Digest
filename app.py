"""Streamlit UI for the Daily FC P&L Monitor.

Simple, non-technical-friendly: upload mixed files (or use bundled data), review detected
FCs/targets/recipients, run analysis, review notification drafts, explicitly deliver them,
download outputs, and apply manual overrides.

Long runs happen on a background thread so the screen never freezes; progress is polled
with an st.fragment, and results are persisted to SQLite so you can leave and come back.
Per the Streamlit multithreading docs, the worker thread does NOT call any st.* function.
"""
from __future__ import annotations

import os
import hashlib
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
from agent.notifier import (  # noqa: E402
    NotificationRouting,
    deliver_notifications,
    is_valid_email,
)
from core.config import load_config  # noqa: E402
from core.engine import latest_status_by_fc  # noqa: E402
from core.intake import GLOBAL_SCOPE, inspect_inputs  # noqa: E402
from core.report import build_anomalies_xlsx, build_report_xlsx  # noqa: E402

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
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

def _start_job(sources: list[str], fc_assignments: dict[str, str],
               target_assignments: dict[str, str],
               routing: NotificationRouting) -> str:
    """Kick off the pipeline on a daemon thread. The worker never touches st.*."""

    def worker(job_id: str) -> None:
        job = get_job(job_id)

        def progress(msg: str) -> None:
            job["progress"] = msg

        try:
            result = run_pipeline(
                sources,
                run_date=RUN_DATE,
                fc_assignments=fc_assignments,
                target_assignments=target_assignments,
                routing=routing,
                dispatch_notifications=False,
                progress_cb=progress,
            )
            if result.get("errors"):
                job["status"] = "error"
                job["error"] = "; ".join(result["errors"])
                return
            # Persist to memory so results survive a page refresh.
            run_id = f"run-{job_id[:8]}"
            record_run(run_id, RUN_DATE, ", ".join(sources), "ok",
                       {"days": len(result["pnl_df"]),
                        "anomalies": len(result["anomalies_df"]),
                        "llm_used": result["llm_used"]})
            record_daily_results(run_id, result["pnl_df"], result["anomalies_df"])
            result["run_token"] = job_id
            job["result"] = result
            job["status"] = "done"
            job["progress"] = "Complete"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            job["status"] = "error"
            job["error"] = f"Unexpected error: {exc}"

    return start_thread(worker)


def _start_send_job(drafts: list[dict], run_token: str) -> str:
    """Deliver reviewed drafts on a worker thread so SMTP cannot freeze Streamlit."""

    def worker(job_id: str) -> None:
        job = get_job(job_id)
        try:
            job["progress"] = "Delivering reviewed notifications…"
            job["result"] = deliver_notifications(drafts, f"{RUN_DATE}-{run_token[:8]}")
            job["status"] = "done"
            job["progress"] = "Delivery complete"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"Unexpected delivery error: {exc}"

    return start_thread(worker)


# --------------------------- sidebar / inputs ---------------------------

with st.sidebar:
    st.header("📊 FC P&L Monitor")
    st.caption("Upload P&L and target files together; their roles are detected automatically.")

    uploaded_files = st.file_uploader(
        "Upload one or more CSV / Excel files",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        help="Files may contain several FCs, one FC per file, embedded targets, or separate target tables.",
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


# Upload changes reset only run-scoped/session-scoped data. Addresses never touch config/SQLite.
def _reset_run_state() -> None:
    for key in list(st.session_state):
        if key.startswith(("fc_assign_", "target_scope_", "manager_email_")) or key in {
            "job_id", "send_job_id", "last_result", "sent_run_token", "manpower_owner_email",
        }:
            del st.session_state[key]


def _save_uploads(uploads) -> tuple[list[str], str]:
    """Persist uploads under collision-safe content-hash names and return a stable signature."""
    SCRATCH.mkdir(parents=True, exist_ok=True)
    paths, signature_parts = [], []
    for upload in uploads:
        payload = bytes(upload.getbuffer())
        digest = hashlib.sha256(payload).hexdigest()
        safe_name = Path(upload.name).name
        dest = SCRATCH / f"{digest[:12]}__{safe_name}"
        if not dest.exists() or dest.stat().st_size != len(payload):
            dest.write_bytes(payload)
        paths.append(str(dest))
        signature_parts.append(f"{safe_name}:{digest}")
    signature = hashlib.sha256("|".join(signature_parts).encode("utf-8")).hexdigest()
    return paths, signature


new_paths: list[str] | None = None
new_signature: str | None = None
if use_sample:
    new_paths = [str(DATA_DIR / "Case Study 3.xlsx")]
    new_signature = "sample-workbook"
elif use_extended:
    new_paths = [str(DATA_DIR / "extended_test_data.csv")]
    new_signature = "extended-test-data"
elif uploaded_files:
    saved_paths, uploaded_signature = _save_uploads(uploaded_files)
    if uploaded_signature != st.session_state.get("last_seen_upload_signature"):
        st.session_state["last_seen_upload_signature"] = uploaded_signature
        new_paths, new_signature = saved_paths, uploaded_signature
else:
    st.session_state.pop("last_seen_upload_signature", None)
    if st.session_state.get("input_origin") == "upload":
        _reset_run_state()
        for key in ("input_paths", "input_signature", "input_origin"):
            st.session_state.pop(key, None)

if new_paths is not None and new_signature != st.session_state.get("input_signature"):
    _reset_run_state()
    st.session_state["input_paths"] = new_paths
    st.session_state["input_signature"] = new_signature
    st.session_state["input_origin"] = (
        "upload" if new_signature not in {"sample-workbook", "extended-test-data"} else "sample"
    )


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


def _send_progress_area(result: dict | None) -> None:
    send_id = st.session_state.get("send_job_id")
    job = get_job(send_id) if send_id else None
    if job is None:
        return
    if job["status"] == "running":
        _poll_running(send_id)
    elif job["status"] == "error":
        st.error(f"❌ {job['error']}")
    elif result is not None:
        result["notifications"] = job["result"]
        st.session_state["last_result"] = result
        st.session_state["sent_run_token"] = result.get("run_token", "")
        st.success("✅ Reviewed notifications delivered.")


# --------------------------- main panel ---------------------------

st.title("Daily FC P&L Monitor & Alert Digest")
st.caption("**Case Study 3** — Upload → Review inputs → Analyse → Review drafts → Send")
_progress_area()

result = _current_result()
_send_progress_area(result)

input_paths = st.session_state.get("input_paths", [])
if result is None and input_paths:
    cfg = load_config()
    initial = inspect_inputs(input_paths, cfg.targets)

    st.subheader("1. Review detected inputs")
    st.caption(
        "The monitor classifies every file/sheet, merges FC data, and resolves each target. "
        "Confirm any inferred FC or target scope before analysis."
    )

    fc_assignments: dict[str, str] = {}
    for table in initial.tables:
        if table.needs_fc:
            default_fc = table.fcs[0] if table.fcs else table.suggested_fc
            fc_assignments[table.id] = st.text_input(
                f"FC name for {table.source} / {table.sheet or 'CSV'}",
                value=default_fc,
                key=f"fc_assign_{table.id}",
                help="This table has no FC column. The name was inferred from its file/sheet name.",
            ).strip()

    # Re-inspect once so target-scope choices reflect confirmed FC names.
    scoped_preview = inspect_inputs(input_paths, cfg.targets, fc_assignments)
    target_assignments: dict[str, str] = {}
    scope_options = [GLOBAL_SCOPE] + scoped_preview.fcs
    for table in scoped_preview.tables:
        if table.target_scope_editable:
            inferred = GLOBAL_SCOPE if table.target_scope == "Global" else table.target_scope
            index = scope_options.index(inferred) if inferred in scope_options else 0
            target_assignments[table.id] = st.selectbox(
                f"Target scope for {table.source} / {table.sheet or 'CSV'}",
                scope_options,
                index=index,
                format_func=lambda value: "Global (all FCs)" if value == GLOBAL_SCOPE else value,
                key=f"target_scope_{table.id}",
            )

    preview = inspect_inputs(
        input_paths, cfg.targets, fc_assignments, target_assignments,
    )
    st.dataframe(pd.DataFrame([table.as_dict() for table in preview.tables]),
                 width="stretch", hide_index=True)

    for warning in preview.warnings:
        st.warning(warning)
    for error in preview.errors:
        st.error(error)

    if preview.fcs:
        st.markdown("**Resolved target coverage**")
        st.dataframe(preview.target_coverage(), width="stretch", hide_index=True)

        st.subheader("2. Notification recipients")
        st.caption(
            "Manager digests are routed per FC. Only material Manpower breaches create an "
            "owner alert; all other breaches remain in the dashboard and reports."
        )
        manager_emails: dict[str, str] = {}
        manager_columns = st.columns(2)
        for index, fc in enumerate(preview.fcs):
            key_hash = hashlib.sha1(fc.encode("utf-8")).hexdigest()[:10]
            manager_emails[fc] = manager_columns[index % 2].text_input(
                f"{fc} manager email",
                value=cfg.manager_for(fc),
                key=f"manager_email_{key_hash}",
            ).strip()
        manpower_owner = st.text_input(
            "Shared Manpower line-owner email",
            value=cfg.owner_for("Manpower"),
            key="manpower_owner_email",
        ).strip()

        invalid = [f"{fc} manager" for fc, email in manager_emails.items()
                   if not is_valid_email(email)]
        if not is_valid_email(manpower_owner):
            invalid.append("Manpower owner")
        if invalid:
            st.warning(
                "Analysis can continue, but delivery will skip invalid recipients: "
                + ", ".join(invalid) + "."
            )

        running_job = get_job(st.session_state.get("job_id", ""))
        analysis_running = bool(running_job and running_job["status"] == "running")
        if st.button(
            "Run analysis",
            type="primary",
            disabled=bool(preview.errors) or analysis_running,
            width="stretch",
        ):
            routing = NotificationRouting(
                fc_managers=manager_emails,
                line_owners={"Manpower": manpower_owner},
                enabled_owner_items={"Manpower"},
            )
            st.session_state.pop("send_job_id", None)
            st.session_state.pop("sent_run_token", None)
            st.session_state["job_id"] = _start_job(
                input_paths, fc_assignments, target_assignments, routing,
            )
            st.rerun()

if result is None:
    st.markdown(
        "**How it works:** Upload one or more files in the sidebar, confirm the detected FCs, "
        "targets and recipients, then click **Run analysis**. Results and notification drafts "
        "are shown before anything is delivered."
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
drafts = result.get("notification_drafts", [])

st.subheader("Notification review")
if drafts:
    draft_table = pd.DataFrame([{
        "To": draft.get("to", ""),
        "Type": draft.get("kind", ""),
        "FC": draft.get("fc", ""),
        "Subject": draft.get("subject", ""),
        "Recipient": "Valid" if draft.get("recipient_valid") else "Missing / invalid",
    } for draft in drafts])
    st.dataframe(draft_table, width="stretch", hide_index=True)
    valid_count = sum(bool(draft.get("recipient_valid")) for draft in drafts)
    run_token = result.get("run_token", "")
    already_sent = bool(run_token and st.session_state.get("sent_run_token") == run_token)
    send_job = get_job(st.session_state.get("send_job_id", ""))
    sending = bool(send_job and send_job["status"] == "running")
    mode = os.environ.get("EMAIL_MODE", "outbox").strip().lower()
    action_label = (
        f"Send {valid_count} reviewed notification(s)"
        if mode == "smtp" else f"Write {valid_count} reviewed notification(s) to outbox"
    )
    if already_sent:
        st.success("These notification drafts have already been delivered for this analysis run.")
    elif valid_count == 0:
        st.warning("No draft has a valid recipient. Correct the addresses and run analysis again.")
    if st.button(
        action_label,
        type="primary",
        disabled=already_sent or sending or valid_count == 0,
        key="send_reviewed_notifications",
    ):
        st.session_state["send_job_id"] = _start_send_job(drafts, run_token)
        st.rerun()
else:
    st.info("No notification drafts were required for this run.")

if st.button("Edit input assignments or recipients", key="edit_run_setup"):
    for key in ("job_id", "send_job_id", "last_result", "sent_run_token"):
        st.session_state.pop(key, None)
    st.rerun()

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
        "⬇️ Full report (Excel, colour-coded)", build_report_xlsx(pnl, anoms),
        "fc_pnl_report.xlsx", _XLSX_MIME, width="stretch",
        help="Two sheets — P&L and Anomalies — with the Colour column filled Red/Yellow/Green/Blue.",
    )
    dl2.download_button(
        "⬇️ Anomalies log (Excel, colour-coded)", build_anomalies_xlsx(anoms),
        "anomalies_log.xlsx", _XLSX_MIME, width="stretch",
        help="Each row tinted by the day's colour; Colour column shown Red/Yellow/Green/Blue.",
    )
    st.download_button(
        "…or plain anomalies CSV (raw data)", anoms.to_csv(index=False).encode("utf-8"),
        "anomalies_log.csv", "text/csv",
        help="Date | FC | Line Item | % of Revenue | Target Range | Status | Colour",
    )
    if drafts:
        st.caption(
            f"✉️ {len(drafts)} digest / alert draft(s) were prepared without sending. "
            "Use the reviewed-notifications action above when ready."
        )
        if notifications:
            st.markdown("**Delivery log**")
        log_df = pd.DataFrame(
            [{k: n.get(k) for k in ("to", "subject", "kind", "status", "mode")}
             for n in notifications]
        ) if notifications else pd.DataFrame()
        if not log_df.empty:
            st.dataframe(log_df, width="stretch")
        preview = next((draft for draft in drafts if draft.get("html")), None)
        if preview:
            with st.expander(f"Preview: {preview['subject']}"):
                components.html(preview["html"], height=480, scrolling=True)
    else:
        st.caption("✉️ No notification drafts were needed for this run.")

    st.stop()  # Simple view is complete; skip the Advanced tabs below.

# ---- Advanced view: the full tabbed dashboard (unchanged) ----
tab_dash, tab_pnl, tab_anom, tab_notif, tab_over = st.tabs(
    ["📈 Dashboard", "🧮 P&L", "🚩 Anomalies", "✉️ Notifications", "⚙️ Overrides"]
)

with tab_dash:
    crit = result.get("criteria_info", {})
    uploaded_targets = len(crit.get("targets", {})) + sum(
        len(targets) for targets in crit.get("targets_by_fc", {}).values()
    )
    if crit.get("source") == "input files":
        st.success(
            f"🎯 {uploaded_targets} target definition(s) read from the uploaded input(s); "
            "missing FC/item targets fall back to config.yaml."
        )
    else:
        st.caption("🎯 Target ranges: using **config.yaml** defaults "
                   "(no criteria table found in the input).")
    resolved_rows = []
    for fc, targets in crit.get("resolved_targets_by_fc", {}).items():
        for item, target in targets.items():
            resolved_rows.append({
                "FC": fc,
                "Line Item": item,
                "Target Range": f"{target['min']:g}-{target['max']:g}%",
                "Source": crit.get("target_sources_by_fc", {}).get(fc, {}).get(item, ""),
            })
    if resolved_rows:
        with st.expander("Resolved targets and provenance"):
            st.dataframe(pd.DataFrame(resolved_rows), width="stretch", hide_index=True)

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
    st.download_button(
        "⬇️ Download full report (Excel, colour-coded)", build_report_xlsx(pnl, anoms),
        "fc_pnl_report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="P&L + Anomalies sheets, with the Colour column filled Red/Yellow/Green/Blue.",
    )

with tab_anom:
    st.subheader("Flagged anomalies log")
    st.caption("Date | FC | Line Item | % of Revenue | Target Range | Status | Colour")
    if "Colour" in anoms.columns and not anoms.empty:
        def _colour_cell(v):
            hexc = COLOUR_HEX.get(v)
            return f"background-color: {hexc}; color: white; font-weight: 600;" if hexc else ""
        st.dataframe(anoms.style.map(_colour_cell, subset=["Colour"]), width="stretch")
    else:
        st.dataframe(anoms, width="stretch")
    ac1, ac2 = st.columns(2)
    ac1.download_button("⬇️ Anomalies (Excel, colour-coded)", build_anomalies_xlsx(anoms),
                        "anomalies_log.xlsx", _XLSX_MIME, width="stretch")
    ac2.download_button("⬇️ Anomalies (CSV, raw)", anoms.to_csv(index=False).encode("utf-8"),
                        "anomalies_log.csv", "text/csv", width="stretch")

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
    st.subheader("Notification drafts and delivery")
    n_cfg = load_config().notifications
    st.caption(
        f"The web run creates manager digests plus one shared Manpower-owner summary, scoped "
        f"to **{n_cfg.get('scope', 'latest_day').replace('_', ' ')}** and material breaches "
        f"**≥ {n_cfg.get('materiality_pp', 1.0):g}pp** beyond range. Other line-item breaches "
        "remain visible but are not emailed."
    )
    if drafts:
        for draft in drafts[:8]:
            recipient_state = "" if draft.get("recipient_valid") else " ⚠ invalid recipient"
            with st.expander(
                f"DRAFT · {draft['kind']} → {draft.get('to') or '(missing)'}: "
                f"{draft['subject']}{recipient_state}"
            ):
                if draft.get("html"):
                    components.html(draft["html"], height=520, scrolling=True)
                else:
                    st.code(draft.get("body", ""), language="text")
    else:
        st.info("No notification drafts were required.")
    if notifications:
        st.markdown("**Delivery log**")
        log_df = pd.DataFrame(
            [{k: n.get(k) for k in ("to", "subject", "kind", "status", "mode", "detail")}
             for n in notifications]
        )
        st.dataframe(log_df, width="stretch")
    elif drafts:
        st.caption("Nothing has been delivered yet. Use the reviewed-notifications action above.")

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
