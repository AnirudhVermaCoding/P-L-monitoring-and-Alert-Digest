"""Plain-language insight generation.

Design rule: the LLM NEVER computes numbers. All figures (deviations, contributors,
CM values) are computed deterministically in core/engine.py and injected. The LLM only
rephrases them into a readable narrative. If no XAI_API_KEY is present or the API fails,
`fallback_insight` produces the same facts from a template — so the system works with
zero keys (a hard requirement of the brief).
"""
from __future__ import annotations

import os

import pandas as pd

from core.config import Config
from core.engine import contributors_for_day

# Cap LLM calls to stay well within any free tier.
MAX_LLM_CALLS = 20


def _facts_for_day(pnl_row: pd.Series, day_anoms: pd.DataFrame, config: Config) -> dict:
    """Assemble the deterministic facts an insight needs."""
    contribs = contributors_for_day(pnl_row, config)
    cm2 = pnl_row["CM2 %"]
    cm1 = pnl_row["CM1 %"]
    colour = pnl_row["Colour"]

    breaches = []
    for _, a in day_anoms.iterrows():
        breaches.append({
            "line_item": a["Line Item"],
            "pct": a["% of Revenue"],
            "target": a["Target Range"],
            "status": a["Status"],
        })

    return {
        "date": pnl_row["Date"],
        "fc": pnl_row["FC"],
        "cm1_pct": cm1,
        "cm2_pct": cm2,
        "colour": colour,
        "breaches": breaches,
        "top_contributors": contribs[:3],
        "suspicious": colour == "Blue",
    }


def fallback_insight(facts: dict) -> str:
    """Deterministic template insight (no LLM). Matches the brief's example shape."""
    date, fc = facts["date"], facts["fc"]
    cm2 = facts["cm2_pct"]
    lines = []

    if facts["suspicious"]:
        lines.append(
            f"On {date}, {fc} CM2 = {cm2:.1f}% (above the 33% 'suspicious' threshold). "
            f"This is unusually high — likely under-reported costs."
        )
    elif cm2 < 12:
        lines.append(
            f"On {date}, {fc} CM2 = {cm2:.1f}% (below the 12% threshold, colour Red)."
        )
    elif cm2 > 30:
        lines.append(
            f"On {date}, {fc} CM2 = {cm2:.1f}% (above the 30% healthy band)."
        )
    else:
        lines.append(
            f"On {date}, {fc} CM2 = {cm2:.1f}% (colour {facts['colour']}), but line "
            f"item(s) breached their target range."
        )

    breached_items = [
        f"{b['line_item']} = {b['pct']:.1f}% ({b['status'].replace('_', ' ').lower()}, "
        f"target {b['target']})"
        for b in facts["breaches"]
        if b["line_item"] not in ("CM1%", "CM2%")
    ]
    if breached_items:
        lines.append("Breached line items: " + "; ".join(breached_items) + ".")

    if facts["top_contributors"]:
        top = facts["top_contributors"]
        ranked = ", ".join(
            f"{c['line_item']} (+{c['deviation_pp']:.1f}pp, ~{c['impact_value']:,.0f})"
            for c in top
        )
        lines.append(f"Top contributors by impact: {ranked}.")

    if facts["suspicious"]:
        action = "Recommend a cost-completeness audit — verify no invoices are missing."
    elif cm2 < 12:
        action = "Recommend an urgent cost audit of the top contributors above."
    else:
        action = "Recommend the line-item owner review and correct the breach."
    lines.append(action)

    return " ".join(lines)


def _llm_insight(facts: dict, model: str) -> str:
    """Ask Grok to phrase the pre-computed facts. Raises on any failure (caller falls back)."""
    from langchain_xai import ChatXAI  # imported lazily so no key => no dependency at import

    llm = ChatXAI(model=model, temperature=0, timeout=30, max_retries=1)
    prompt = (
        "You are an FC finance analyst. Using ONLY the JSON facts below, write a concise "
        "2-4 sentence alert. Do NOT invent or recompute any numbers; use the ones given. "
        "State the deviation size, which line items breached, the top contributors sorted "
        "by impact, and one recommended action. Plain business English.\n\n"
        f"FACTS:\n{facts}"
    )
    resp = llm.invoke(prompt)
    text = resp.content if hasattr(resp, "content") else str(resp)
    text = text.strip()
    if not text:
        raise ValueError("Empty LLM response")
    return text


def generate_insights(
    pnl_df: pd.DataFrame,
    anomalies_df: pd.DataFrame,
    config: Config,
) -> tuple[list[dict], bool]:
    """Produce one insight per anomalous (FC, date). Returns (insights, llm_used).

    Tries Grok when XAI_API_KEY is set (capped at MAX_LLM_CALLS); always falls back to a
    deterministic template on any error or when no key is present.
    """
    api_key = os.environ.get("XAI_API_KEY", "").strip()
    model = os.environ.get("XAI_MODEL", "grok-4.3").strip() or "grok-4.3"
    use_llm = bool(api_key)

    insights: list[dict] = []
    llm_used = False
    llm_calls = 0

    if anomalies_df.empty:
        return insights, llm_used

    # One insight per (FC, date) that has any anomaly.
    keyed = anomalies_df.groupby(["FC", "Date"], sort=False)

    for (fc, date), day_anoms in keyed:
        match = pnl_df[(pnl_df["FC"] == fc) & (pnl_df["Date"] == date)]
        if match.empty:
            continue
        pnl_row = match.iloc[0]  # keeps FC/Date as accessible columns

        facts = _facts_for_day(pnl_row, day_anoms, config)

        text = None
        if use_llm and llm_calls < MAX_LLM_CALLS:
            try:
                text = _llm_insight(facts, model)
                llm_used = True
                llm_calls += 1
            except Exception:  # noqa: BLE001 - any LLM problem => template fallback
                text = None
        if text is None:
            text = fallback_insight(facts)

        insights.append({
            "FC": fc,
            "Date": date,
            "Colour": facts["colour"],
            "CM2 %": facts["cm2_pct"],
            "insight": text,
            "facts": facts,
        })

    return insights, llm_used
