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
from core.engine import contributors_for_day, recommended_action

RUNTIME_API_VERSION = 1

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

    lines.append(recommended_action(facts["colour"], has_breach=bool(breached_items)))

    return " ".join(lines)


def resolve_llm() -> dict | None:
    """Figure out which LLM provider/model to use from env, or None if no key is set.

    Auto-detects the provider from the key prefix so the user doesn't have to configure it:
    a Groq key ("gsk_...") uses Groq's free OpenAI-compatible API; an xAI key ("xai-...")
    uses Grok. Returns {provider, label, key, model, base_url} or None.
    """
    key = (os.environ.get("XAI_API_KEY", "").strip()
           or os.environ.get("GROQ_API_KEY", "").strip()
           or os.environ.get("LLM_API_KEY", "").strip())
    if not key:
        return None
    if key.startswith("gsk_"):
        model = os.environ.get("GROQ_MODEL", "").strip() or "llama-3.3-70b-versatile"
        return {"provider": "groq", "label": f"Groq ({model})", "key": key,
                "model": model, "base_url": "https://api.groq.com/openai/v1"}
    model = os.environ.get("XAI_MODEL", "").strip() or "grok-4.3"
    return {"provider": "xai", "label": f"Grok ({model})", "key": key,
            "model": model, "base_url": "https://api.x.ai/v1"}


def _build_llm(cfg: dict):
    """Build a LangChain chat model for the resolved provider (OpenAI-compatible for Groq)."""
    if cfg["provider"] == "xai":
        from langchain_xai import ChatXAI  # lazy import: no key => never imported
        return ChatXAI(model=cfg["model"], temperature=0, timeout=30, max_retries=1)
    from langchain_openai import ChatOpenAI  # Groq speaks the OpenAI API
    return ChatOpenAI(model=cfg["model"], api_key=cfg["key"], base_url=cfg["base_url"],
                      temperature=0, timeout=30, max_retries=1)


def _llm_insight(facts: dict, llm) -> str:
    """Ask the LLM to phrase the pre-computed facts. Raises on any failure (caller falls back)."""
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

    Tries the configured LLM (Groq or xAI, auto-detected) when a key is set, capped at
    MAX_LLM_CALLS; always falls back to a deterministic template on any error or with no key.
    """
    llm_cfg = resolve_llm()

    insights: list[dict] = []
    llm_used = False
    llm_calls = 0

    if anomalies_df.empty:
        return insights, llm_used

    # Build the model once (reused across days). If it can't be built, stay on template.
    llm = None
    if llm_cfg is not None:
        try:
            llm = _build_llm(llm_cfg)
        except Exception:  # noqa: BLE001
            llm = None

    # One insight per (FC, date) that has any anomaly. Spend the limited LLM budget on the
    # most notable days first (Red/Blue, then Yellow, then Green; latest dates first) so the
    # important insights are AI-written and only routine ones fall back to the template.
    colour_by_key = {(r["FC"], r["Date"]): r["Colour"] for _, r in pnl_df.iterrows()}
    severity = {"Red": 0, "Blue": 1, "Yellow": 2, "Green": 3}
    groups = list(anomalies_df.groupby(["FC", "Date"], sort=False))
    # Stable sort: first by date descending, then by severity ascending. Because Python's sort
    # is stable, severity wins overall while latest dates stay first within each severity band.
    groups.sort(key=lambda g: str(g[0][1]), reverse=True)
    groups.sort(key=lambda g: severity.get(colour_by_key.get(g[0], "Green"), 3))

    for (fc, date), day_anoms in groups:
        match = pnl_df[(pnl_df["FC"] == fc) & (pnl_df["Date"] == date)]
        if match.empty:
            continue
        pnl_row = match.iloc[0]  # keeps FC/Date as accessible columns

        facts = _facts_for_day(pnl_row, day_anoms, config)

        text = None
        if llm is not None and llm_calls < MAX_LLM_CALLS:
            try:
                text = _llm_insight(facts, llm)
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
