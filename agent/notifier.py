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

from core.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTBOX = REPO_ROOT / "outputs" / "outbox"

COLOUR_EMOJI = {"Red": "🔴", "Yellow": "🟡", "Green": "🟢", "Blue": "🔵"}


def build_digests(pnl_df: pd.DataFrame, anomalies_df: pd.DataFrame, insights: list[dict],
                  config: Config, trend_notes: dict[str, str] | None = None) -> dict[str, str]:
    """One colour-coded digest string per FC (its most recent day headline + issues)."""
    trend_notes = trend_notes or {}
    digests: dict[str, str] = {}
    insight_by_key = {(i["FC"], i["Date"]): i["insight"] for i in insights}

    for fc, fc_pnl in pnl_df.groupby("FC", sort=False):
        fc_pnl = fc_pnl.sort_values("Date")
        latest = fc_pnl.iloc[-1]
        colour = latest["Colour"]
        emoji = COLOUR_EMOJI.get(colour, "")

        lines = [
            f"{emoji} {fc} — {latest['Date']}",
            f"Colour: {colour} | CM1%: {latest['CM1 %']:.1f} | CM2%: {latest['CM2 %']:.1f}",
        ]
        trend = trend_notes.get(fc, "")
        if trend:
            lines.append(f"Trend: {trend}")

        latest_insight = insight_by_key.get((fc, latest["Date"]))
        if latest_insight:
            lines.append("")
            lines.append(latest_insight)

        # Roll up how the whole loaded window looked for this FC.
        counts = fc_pnl["Colour"].value_counts().to_dict()
        summary = ", ".join(f"{COLOUR_EMOJI.get(c,'')} {c}: {n}" for c, n in counts.items())
        lines.append("")
        lines.append(f"Window summary ({len(fc_pnl)} days): {summary}")

        digests[fc] = "\n".join(lines)
    return digests


def _owner_messages(anomalies_df: pd.DataFrame, config: Config) -> list[dict]:
    """One grouped email per (owner, FC, date) covering that owner's breached line items."""
    line_anoms = anomalies_df[~anomalies_df["Line Item"].isin(["CM1%", "CM2%"])]
    messages = []
    for (fc, date), grp in line_anoms.groupby(["FC", "Date"], sort=False):
        # Group by owner within the day.
        by_owner: dict[str, list[str]] = {}
        for _, a in grp.iterrows():
            owner = config.owner_for(a["Line Item"])
            by_owner.setdefault(owner, []).append(
                f"- {a['Line Item']}: {a['% of Revenue']:.1f}% "
                f"(target {a['Target Range']}, {a['Status'].replace('_',' ').lower()})"
            )
        for owner, items in by_owner.items():
            subject = f"[P&L Alert] {fc} {date}: {len(items)} cost line(s) out of range"
            body = (
                f"Cost line item(s) breached target on {date} at {fc}:\n\n"
                + "\n".join(items)
                + "\n\nPlease review and confirm whether this is expected."
            )
            messages.append({"to": owner, "subject": subject, "body": body,
                             "fc": fc, "date": date, "kind": "owner_alert"})
    return messages


def _digest_messages(digests: dict[str, str], config: Config) -> list[dict]:
    messages = []
    for fc, digest in digests.items():
        # Colour word lives on the "Colour: X | ..." line. Find that line, not a fixed index.
        colour = "Green"
        colour_line = next((ln for ln in digest.split("\n") if ln.startswith("Colour:")), "")
        for tag in COLOUR_EMOJI:
            if tag in colour_line:
                colour = tag
                break
        subject = f"[{colour}] Daily P&L digest — {fc}"
        messages.append({"to": config.manager_for(fc), "subject": subject,
                         "body": digest, "fc": fc, "date": "digest", "kind": "digest"})
    return messages


def _write_outbox(msg: dict, run_date: str) -> Path:
    day_dir = OUTBOX / run_date
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_to = msg["to"].replace("@", "_at_").replace(".", "_")
    safe_subj = "".join(c if c.isalnum() or c in " -_" else "_" for c in msg["subject"])[:60]
    path = day_dir / f"{msg['kind']}__{safe_to}__{safe_subj}.md"
    path.write_text(
        f"To: {msg['to']}\nSubject: {msg['subject']}\n\n{msg['body']}\n",
        encoding="utf-8",
    )
    return path


def _send_smtp(msg: dict) -> None:
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", user or "pnl-monitor@example.com")
    if not host or not user or not password:
        raise RuntimeError("SMTP_HOST/SMTP_USER/SMTP_PASSWORD not fully configured")

    email = EmailMessage()
    email["From"] = sender
    email["To"] = msg["to"]
    email["Subject"] = msg["subject"]
    email.set_content(msg["body"])
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(email)


def dispatch(digests: dict[str, str], anomalies_df: pd.DataFrame, config: Config,
             run_date: str) -> list[dict]:
    """Deliver all owner alerts + FC digests. Returns a send log for the UI."""
    mode = os.environ.get("EMAIL_MODE", "outbox").strip().lower()
    messages = _owner_messages(anomalies_df, config) + _digest_messages(digests, config)

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
        log.append(record)
    return log
