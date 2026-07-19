"""LangGraph pipeline: ingest -> compute -> detect -> insight -> digest -> notify.

API pattern copied from https://docs.langchain.com/oss/python/langgraph/graph-api
(StateGraph + START/END edges + add_conditional_edges). Each node returns a partial
state dict that LangGraph merges into the running state. A `progress_cb` lets the UI show
live status without any st.* calls inside the graph.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

import pandas as pd
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agent.insights import generate_insights
from agent.notifier import (
    NotificationRouting,
    build_digests,
    deliver_notifications,
    prepare_notifications,
)
from core.config import Config, load_config
from core.engine import compute_pnl, detect_anomalies
from core.intake import inspect_inputs

RUNTIME_API_VERSION = 1


class PnlState(TypedDict, total=False):
    source: str                 # file path to load (day-wise data)
    sources: list[str]          # one or more mixed P&L / target files
    criteria_source: str        # optional separate file holding target ranges
    fc_assignments: dict[str, str]
    target_assignments: dict[str, str]
    run_date: str               # label for outbox folder
    config: Config
    routing: NotificationRouting
    dispatch_notifications: bool
    criteria_info: dict         # which targets came from the input vs config.yaml
    apply_overrides: bool
    raw_df: pd.DataFrame
    pnl_df: pd.DataFrame
    anomalies_df: pd.DataFrame
    insights: list[dict]
    digests: dict[str, dict]
    notification_drafts: list[dict]
    notifications: list[dict]
    trend_notes: dict[str, str]
    llm_used: bool
    errors: list[str]
    progress: str
    progress_cb: Optional[Callable[[str], None]]


def _emit(state: PnlState, msg: str) -> None:
    cb = state.get("progress_cb")
    if cb:
        try:
            cb(msg)
        except Exception:  # noqa: BLE001 - UI callback must never break the graph
            pass


# --------------------------- nodes ---------------------------

def ingest_node(state: PnlState) -> dict:
    _emit(state, "Loading data…")
    cfg = state["config"]
    sources = list(state.get("sources") or [state["source"]])
    if state.get("criteria_source") and state["criteria_source"] not in sources:
        sources.append(state["criteria_source"])
    try:
        intake = inspect_inputs(
            sources,
            cfg.targets,
            state.get("fc_assignments"),
            state.get("target_assignments"),
        )
    except Exception as exc:  # noqa: BLE001 - keep intake failures user-facing
        return {"errors": [f"Unexpected error reading input: {exc}"], "progress": "Load failed"}
    if intake.errors:
        return {"errors": intake.errors, "progress": "Load failed"}

    configured = replace(
        cfg,
        targets={**cfg.targets, **intake.global_targets},
        targets_by_fc=intake.targets_by_fc,
    )
    uploaded_count = len(intake.global_targets) + sum(
        len(targets) for targets in intake.targets_by_fc.values()
    )
    if uploaded_count:
        _emit(
            state,
            f"Using {uploaded_count} uploaded target definition(s) across {len(intake.fcs)} FC(s)",
        )
    return {
        "raw_df": intake.data,
        "config": configured,
        "criteria_info": {
            "source": "input files" if uploaded_count else "config.yaml",
            "targets": intake.global_targets,
            "targets_by_fc": intake.targets_by_fc,
            "resolved_targets_by_fc": intake.resolved_targets_by_fc,
            "target_sources_by_fc": intake.target_sources_by_fc,
            "warnings": intake.warnings,
        },
        "progress": "Data loaded",
    }


def compute_node(state: PnlState) -> dict:
    _emit(state, "Computing P&L (CM1, CM2, colours)…")
    cfg = state["config"]
    pnl = compute_pnl(state["raw_df"], cfg)
    return {"pnl_df": pnl, "progress": "P&L computed"}


def detect_node(state: PnlState) -> dict:
    _emit(state, "Detecting anomalies…")
    cfg = state["config"]
    anoms = detect_anomalies(state["pnl_df"], cfg)

    # Apply manual overrides from memory (acknowledge / false_positive => mute).
    if state.get("apply_overrides"):
        try:
            from core.memory import get_overrides
            ov = get_overrides()
            mutes = ov[ov["action"].isin(["acknowledge", "false_positive"])]
            if not mutes.empty and not anoms.empty:
                muted_keys = set(zip(mutes["fc"], mutes["date"], mutes["line_item"]))
                keep = ~anoms.apply(
                    lambda r: (r["FC"], r["Date"], r["Line Item"]) in muted_keys, axis=1
                )
                anoms = anoms[keep].reset_index(drop=True)
        except Exception:  # noqa: BLE001 - memory is optional
            pass

    return {"anomalies_df": anoms, "progress": f"{len(anoms)} anomalies flagged"}


def insight_node(state: PnlState) -> dict:
    _emit(state, "Generating plain-language insights…")
    cfg = state["config"]
    insights, llm_used = generate_insights(state["pnl_df"], state["anomalies_df"], cfg)
    mode = "Grok LLM" if llm_used else "template (no key)"
    return {"insights": insights, "llm_used": llm_used,
            "progress": f"{len(insights)} insights via {mode}"}


def digest_node(state: PnlState) -> dict:
    _emit(state, "Building per-FC digests…")
    cfg = state["config"]

    # Trend notes from memory (best-effort).
    trend_notes: dict[str, str] = {}
    try:
        from core.memory import get_history, trend_note
        history = get_history()
        for fc in state["pnl_df"]["FC"].unique():
            note = trend_note(history, fc)
            if note:
                trend_notes[fc] = note
    except Exception:  # noqa: BLE001
        pass

    digests = build_digests(state["pnl_df"], state["anomalies_df"],
                            state["insights"], cfg, trend_notes)
    return {"digests": digests, "trend_notes": trend_notes, "progress": "Digests ready"}


def notify_node(state: PnlState) -> dict:
    _emit(state, "Preparing notification drafts…")
    cfg = state["config"]
    drafts = prepare_notifications(
        state["digests"], state["anomalies_df"], state["pnl_df"], cfg,
        state.get("routing"),
    )
    if state.get("dispatch_notifications", True):
        log = deliver_notifications(drafts, state["run_date"])
        progress = f"{len(log)} notifications dispatched"
    else:
        log = []
        progress = f"{len(drafts)} notification drafts ready for review"
    return {"notification_drafts": drafts, "notifications": log, "progress": progress}


def _route_after_ingest(state: PnlState) -> str:
    return "error_end" if state.get("errors") else "compute"


# --------------------------- graph ---------------------------

def build_graph():
    builder = StateGraph(PnlState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("compute", compute_node)
    builder.add_node("detect", detect_node)
    builder.add_node("insight", insight_node)
    builder.add_node("digest", digest_node)
    builder.add_node("notify", notify_node)

    builder.add_edge(START, "ingest")
    builder.add_conditional_edges(
        "ingest", _route_after_ingest, {"compute": "compute", "error_end": END}
    )
    builder.add_edge("compute", "detect")
    builder.add_edge("detect", "insight")
    builder.add_edge("insight", "digest")
    builder.add_edge("digest", "notify")
    builder.add_edge("notify", END)
    return builder.compile()


def run_pipeline(source: str | list[str], run_date: str = "run", config: Optional[Config] = None,
                 apply_overrides: bool = True, criteria_source: Optional[str] = None,
                 fc_assignments: Optional[dict[str, str]] = None,
                 target_assignments: Optional[dict[str, str]] = None,
                 routing: Optional[NotificationRouting] = None,
                 dispatch_notifications: bool = True,
                 progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Execute the full graph once and return the final state dict.

    criteria_source: optional path to a separate file with target ranges. When omitted, the
    data file's own sheets are still scanned for an 'Ideal FC Criteria' table.
    """
    graph = build_graph()
    sources = [source] if isinstance(source, str) else list(source)
    if not sources:
        return {"errors": ["No input files were supplied."], "progress": "Load failed"}
    state: PnlState = {
        "source": sources[0],
        "sources": sources,
        "criteria_source": criteria_source or "",
        "fc_assignments": fc_assignments or {},
        "target_assignments": target_assignments or {},
        "run_date": run_date,
        "config": config or load_config(),
        "dispatch_notifications": dispatch_notifications,
        "apply_overrides": apply_overrides,
        "errors": [],
        "progress_cb": progress_cb,
    }
    if routing is not None:
        state["routing"] = routing
    result = graph.invoke(state)
    return result
