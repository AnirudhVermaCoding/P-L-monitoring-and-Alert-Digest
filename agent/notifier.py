"""Build daily digests and deliver notifications.

Two delivery modes (env EMAIL_MODE):
  outbox (default) -> write each email to outputs/outbox/<date>/... as a .md file.
                      Zero-cost, needs no account, and is easy for an evaluator to inspect.
  smtp             -> actually send via smtplib. Any SMTP failure automatically falls back
                      to outbox so the pipeline never crashes on a mail problem.

Routing (from config.yaml):
  - each breached cost line item -> its owner (grouped: one email per owner per FC per day)
  - each FC's daily colour digest -> that FC's manager
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

from agent.email_templates import render_digest_html, render_owner_html
from core.config import Config
from core.engine import latest_status_by_fc

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTBOX = REPO_ROOT / "outputs" / "outbox"

COLOUR_EMOJI = {"Red": "🔴", "Yellow": "🟡", "Green": "🟢", "Blue": "🔵"}


def build_digests(pnl_df: pd.DataFrame, anomalies_df: pd.DataFrame, insights: list[dict],
                  config: Config, trend_notes: dict[str, str] | None = None) -> dict[str, dict]:
    """One structured colour-coded digest per FC (its most recent day + window rollup).

    Returns {fc: {...}} where each dict carries the fields the HTML email needs plus a plain
    `text` version used as the outbox/UI fallback.
    """
    trend_notes = trend_notes or {}
    digests: dict[str, dict] = {}
    insight_by_key = {(i["FC"], i["Date"]): i["insight"] for i in insights}
    facts_by_key = {(i["FC"], i["Date"]): i.get("facts") for i in insights}
    # The at-a-glance verdict fields (biggest breach + one recommended action) for the busy
    # manager come from the same pure helper the Simple view uses — one source of truth.
    status_by_fc = {s["FC"]: s for s in latest_status_by_fc(pnl_df, insights)}

    for fc, fc_pnl in pnl_df.groupby("FC", sort=False):
        fc_pnl = fc_pnl.sort_values("Date")
        latest = fc_pnl.iloc[-1]
        colour = latest["Colour"]
        emoji = COLOUR_EMOJI.get(colour, "")
        trend = trend_notes.get(fc, "")
        latest_insight = insight_by_key.get((fc, latest["Date"]), "")
        counts = fc_pnl["Colour"].value_counts().to_dict()
        status = status_by_fc.get(fc, {})
        largest_breach = status.get("largest_breach", "None")
        action = status.get("recommended_action", "")
        latest_facts = facts_by_key.get((fc, latest["Date"]))
        drivers = (latest_facts or {}).get("top_contributors") or []

        lines = [
            f"{emoji} {fc} — {latest['Date']}",
            f"Colour: {colour} | CM1%: {latest['CM1 %']:.1f} | CM2%: {latest['CM2 %']:.1f}",
            f"Biggest issue: {largest_breach}",
            f"Recommended: {action}",
        ]
        if trend:
            lines.append(f"Trend: {trend}")
        if drivers:  # a clean list beats the free-text paragraph
            lines += ["", "What's driving it:"]
            for c in drivers:
                lines.append(
                    f"  - {c['line_item']}: {c['pct']:.1f}% (target {c['target']}, "
                    f"{c['deviation_pp']:.1f}pp {'over' if c['direction'] == 'above max' else 'under'})"
                )
        elif latest_insight:  # fallback for margin-only days with no single cost driver
            lines += ["", "Detail: " + latest_insight]
        summary = ", ".join(f"{COLOUR_EMOJI.get(c,'')} {c}: {n}" for c, n in counts.items())
        lines += ["", f"Window summary ({len(fc_pnl)} days): {summary}"]

        digests[fc] = {
            "fc": fc, "date": latest["Date"], "colour": colour,
            "cm1": float(latest["CM1 %"]), "cm2": float(latest["CM2 %"]),
            "revenue": float(latest["Revenue"]), "trend": trend, "insight": latest_insight,
            "largest_breach": largest_breach, "action": action, "facts": latest_facts,
            "window_counts": counts, "window_days": int(len(fc_pnl)),
            "cm2_colors": config.colors,
            "text": "\n".join(lines),
        }
    return digests


def _line_deviation(row: pd.Series, config: Config) -> float:
    """pp by which a line-item anomaly is outside its target range (0 if in range)."""
    item = row["Line Item"]
    t = config.targets.get(item)
    if t is None:
        return 0.0
    pct = row["% of Revenue"]
    if pct > t["max"]:
        return pct - t["max"]
    if pct < t["min"]:
        return t["min"] - pct
    return 0.0


def _in_scope_dates(pnl_df: pd.DataFrame, scope: str) -> dict[str, set]:
    """Which dates each FC should page on. latest_day => only that FC's most recent day."""
    scoped: dict[str, set] = {}
    for fc, grp in pnl_df.groupby("FC", sort=False):
        if scope == "latest_day":
            scoped[fc] = {grp["Date"].max()}
        else:
            scoped[fc] = set(grp["Date"])
    return scoped


def _owner_messages(anomalies_df: pd.DataFrame, pnl_df: pd.DataFrame,
                    config: Config) -> list[dict]:
    """ONE summary email per owner, listing only their MATERIAL breaches within scope.

    Sub-threshold breaches stay in the anomalies log / dashboard but don't page anyone. This
    replaces the old one-email-per-(owner, FC, date) behaviour that produced hundreds of mails.
    """
    if anomalies_df.empty:
        return []

    scope = config.notifications.get("scope", "latest_day")
    threshold = float(config.notifications.get("materiality_pp", 1.0))
    scoped_dates = _in_scope_dates(pnl_df, scope)

    line_anoms = anomalies_df[~anomalies_df["Line Item"].isin(["CM1%", "CM2%"])]

    # owner -> list of (fc, date, item, pct, dev, direction, target)
    by_owner: dict[str, list[dict]] = {}
    for _, a in line_anoms.iterrows():
        fc, date = a["FC"], a["Date"]
        if date not in scoped_dates.get(fc, set()):
            continue
        dev = _line_deviation(a, config)
        if dev < threshold:  # not material — logged, but don't page
            continue
        owner = config.owner_for(a["Line Item"])
        by_owner.setdefault(owner, []).append({
            "fc": fc, "date": date, "item": a["Line Item"],
            "pct": a["% of Revenue"], "dev": dev,
            "direction": "above max" if a["Status"] == "ABOVE_MAX" else "below min",
            "target": a["Target Range"],
        })

    messages = []
    for owner, breaches in by_owner.items():
        items = sorted({b["item"] for b in breaches})
        # Group the body by FC -> date for readability.
        lines: list[str] = []
        for fc in sorted({b["fc"] for b in breaches}):
            lines.append(f"{fc}:")
            fc_breaches = [b for b in breaches if b["fc"] == fc]
            for b in sorted(fc_breaches, key=lambda x: (str(x["date"]), -x["dev"])):
                lines.append(
                    f"  - {b['date']}  {b['item']}: {b['pct']:.1f}% "
                    f"({b['direction']} by {b['dev']:.1f}pp, target {b['target']})"
                )
        subject = (f"[P&L Alert] {', '.join(items)} — "
                   f"{len(breaches)} material breach(es) to review")
        body = (
            f"You own: {', '.join(items)}.\n"
            f"The following breached the target range by at least {threshold:g}pp "
            f"(scope: {scope.replace('_', ' ')}):\n\n"
            + "\n".join(lines)
            + "\n\nSmaller (sub-threshold) breaches, if any, are in the P&L dashboard's "
            "Anomalies tab. Please review and confirm whether this is expected."
        )
        html = render_owner_html(items, breaches, threshold, scope)
        rep = breaches[0]
        messages.append({"to": owner, "subject": subject, "body": body, "html": html,
                         "fc": rep["fc"], "date": rep["date"], "kind": "owner_alert"})
    return messages


def _digest_messages(digests: dict[str, dict], config: Config) -> list[dict]:
    messages = []
    for fc, digest in digests.items():
        colour = digest.get("colour", "Green")
        subject = f"[{colour}] Daily P&L digest — {fc}"
        messages.append({"to": config.manager_for(fc), "subject": subject,
                         "body": digest["text"], "html": render_digest_html(digest),
                         "fc": fc, "date": "digest", "kind": "digest"})
    return messages


def _write_outbox(msg: dict, run_date: str) -> Path:
    """Write the email to the outbox. Prefers a .html file (renders the real design in a
    browser); falls back to .md plain text when a message has no HTML body."""
    day_dir = OUTBOX / run_date
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_to = msg["to"].replace("@", "_at_").replace(".", "_")
    safe_subj = "".join(c if c.isalnum() or c in " -_" else "_" for c in msg["subject"])[:60]
    stem = f"{msg['kind']}__{safe_to}__{safe_subj}"

    if msg.get("html"):
        path = day_dir / f"{stem}.html"
        meta = (f'<div style="font-family:sans-serif;font-size:12px;color:#6b7280;'
                f'padding:8px 12px;">To: {msg["to"]} · Subject: {msg["subject"]}</div>')
        path.write_text(meta + msg["html"], encoding="utf-8")
    else:
        path = day_dir / f"{stem}.md"
        path.write_text(
            f"To: {msg['to']}\nSubject: {msg['subject']}\n\n{msg['body']}\n", encoding="utf-8"
        )
    return path


def _send_smtp(msg: dict) -> None:
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    # Gmail requires the From to be the authenticated account, so ignore a blank or
    # placeholder SMTP_FROM and fall back to the login user.
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not sender or sender.endswith("example.com"):
        sender = user
    if not host or not user or not password:
        raise RuntimeError("SMTP_HOST/SMTP_USER/SMTP_PASSWORD not fully configured")

    email = EmailMessage()
    email["From"] = sender
    email["To"] = msg["to"]
    email["Subject"] = msg["subject"]
    email.set_content(msg["body"])  # plain-text fallback for non-HTML clients
    if msg.get("html"):
        email.add_alternative(msg["html"], subtype="html")
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(email)


def dispatch(digests: dict[str, dict], anomalies_df: pd.DataFrame, pnl_df: pd.DataFrame,
             config: Config, run_date: str) -> list[dict]:
    """Deliver rolled-up owner alerts + FC digests. Returns a send log for the UI."""
    mode = os.environ.get("EMAIL_MODE", "outbox").strip().lower()
    messages = _owner_messages(anomalies_df, pnl_df, config) + _digest_messages(digests, config)

    log: list[dict] = []
    for msg in messages:
        record = {"to": msg["to"], "subject": msg["subject"], "kind": msg["kind"],
                  "fc": msg["fc"], "mode": mode, "status": "", "detail": ""}
        try:
            if mode == "smtp":
                _send_smtp(msg)
                record["status"] = "sent"
            else:
                path = _write_outbox(msg, run_date)
                record["status"] = "written"
                record["detail"] = str(path.relative_to(REPO_ROOT))
        except Exception as exc:  # noqa: BLE001 - never let mail issues break the pipeline
            # Fall back to outbox and note why.
            path = _write_outbox(msg, run_date)
            record["status"] = "fallback_outbox"
            record["mode"] = "outbox"
            record["detail"] = f"SMTP failed ({exc}); wrote {path.relative_to(REPO_ROOT)}"
        record["body"] = msg["body"]
        record["html"] = msg.get("html", "")
        log.append(record)
    return log
