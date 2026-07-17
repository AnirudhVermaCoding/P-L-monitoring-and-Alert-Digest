"""LangGraph pipeline: ingest -> compute -> detect -> insight -> digest -> notify.

API pattern copied from https://docs.langchain.com/oss/python/langgraph/graph-api
(StateGraph + START/END edges + add_conditional_edges). Each node returns a partial
state dict that LangGraph merges into the running state. A `progress_cb` lets the UI show
live status without any st.* calls inside the graph.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import pandas as pd
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from agent.insights import generate_insights
from agent.notifier import build_digests, dispatch
from core.config import Config, load_config
from core.engine import compute_pnl, detect_anomalies
from core.loader import LoaderError, load_pnl


class PnlState(TypedDict, total=False):
    source: str                 # file path to load
    run_date: str               # label for outbox folder
    config: Config
    apply_overrides: bool
    raw_df: pd.DataFrame
    pnl_df: pd.DataFrame
    anomalies_df: pd.DataFrame
    insights: list[dict]
    digests: dict[str, str]
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
    try:
        raw = load_pnl(state["source"])
        return {"raw_df": raw, "progress": "Data loaded"}
    except LoaderError as exc:
        return {"errors": [str(exc)], "progress": "Load failed"}
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"Unexpected error reading input: {exc}"], "progress": "Load failed"}


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
    _emit(state, "Dispatching notifications…")
    cfg = state["config"]
    log = dispatch(state["digests"], state["anomalies_df"], state["pnl_df"], cfg,
                   state["run_date"])
    return {"notifications": log, "progress": f"{len(log)} notifications dispatched"}


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


def run_pipeline(source: str, run_date: str = "run", config: Optional[Config] = None,
                 apply_overrides: bool = True,
                 progress_cb: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """Execute the full graph once and return the final state dict."""
    graph = build_graph()
    state: PnlState = {
        "source": source,
        "run_date": run_date,
        "config": config or load_config(),
        "apply_overrides": apply_overrides,
        "errors": [],
        "progress_cb": progress_cb,
    }
    result = graph.invoke(state)
    return result
