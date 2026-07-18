"""HTML email templates — clean, scannable, executive-friendly.

Email clients are picky: they strip <style> blocks and <head>, and many don't support flexbox
or grid. So these templates use table-based layout with INLINE styles only (no <style> block,
no media queries), which renders consistently in Gmail, Outlook, and Apple Mail. Designed to be
read in 10 seconds: a colour header, the headline number, a small metrics row, a breach table,
the plain-language insight, and one recommended action.
"""
from __future__ import annotations

COLOUR_HEX = {"Red": "#d64545", "Yellow": "#d9a441", "Green": "#2e9e5b", "Blue": "#3b7dd8"}
# Soft backgrounds for the "bottom line" band — the tint carries the status at a glance.
COLOUR_TINT = {"Red": "#fdecec", "Yellow": "#fdf6e6", "Green": "#e9f7ef", "Blue": "#eaf1fb"}
COLOUR_WORD = {
    "Red": "Margin below threshold",
    "Yellow": "Borderline margin",
    "Green": "Healthy",
    "Blue": "Unusually high — verify costs",
}


def _verdict(colour: str, has_breach: bool) -> str:
    """The single sentence a busy manager reads first. No jargon, states the call."""
    if colour == "Red":
        return "Margin is below target — needs a look today."
    if colour == "Blue":
        return "Margin is unusually high — check no costs are missing."
    if colour == "Yellow":
        return "Margin is borderline — worth keeping an eye on."
    if has_breach:  # Green overall, but a cost line is out of range
        return "Margin is healthy, but one cost line is over budget."
    return "All healthy — no action needed today."
_INK = "#1f2933"
_MUTED = "#6b7280"
_BORDER = "#e5e7eb"
_BG = "#f4f5f7"
_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif")


def _shell(inner: str, preheader: str = "") -> str:
    """Wrap content in a centered, 600px, email-safe container."""
    pre = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>'
           if preheader else "")
    return (
        f'<div style="margin:0;padding:0;background:{_BG};">'
        f'{pre}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_BG};padding:24px 12px;">'
        f'<tr><td align="center">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="width:600px;max-width:100%;background:#ffffff;border:1px solid {_BORDER};'
        f'border-radius:12px;overflow:hidden;font-family:{_FONT};">'
        f'{inner}'
        f'</table>'
        f'<div style="color:{_MUTED};font-family:{_FONT};font-size:11px;margin-top:14px;">'
        f'Automated by the Daily FC P&amp;L Monitor · figures are system-computed and auditable'
        f'</div>'
        f'</td></tr></table></div>'
    )


def _header(accent: str, eyebrow: str, title: str, subtitle: str = "") -> str:
    sub = (f'<div style="color:rgba(255,255,255,0.9);font-size:13px;margin-top:4px;">{subtitle}</div>'
           if subtitle else "")
    return (
        f'<tr><td style="background:{accent};padding:22px 28px;">'
        f'<div style="color:rgba(255,255,255,0.85);font-size:11px;font-weight:600;'
        f'letter-spacing:1.2px;text-transform:uppercase;">{eyebrow}</div>'
        f'<div style="color:#ffffff;font-size:22px;font-weight:700;margin-top:6px;">{title}</div>'
        f'{sub}'
        f'</td></tr>'
    )


def _metric(label: str, value: str, colour: str = _INK) -> str:
    return (
        f'<td align="center" style="padding:14px 8px;border:1px solid {_BORDER};">'
        f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.6px;">{label}</div>'
        f'<div style="color:{colour};font-size:20px;font-weight:700;margin-top:4px;">{value}</div>'
        f'</td>'
    )


def _fact_row(label: str, value: str, strong: bool = False) -> str:
    """One key: value line in the at-a-glance block."""
    colour = _INK if strong else _MUTED
    weight = "700" if strong else "500"
    return (
        f'<tr>'
        f'<td style="padding:6px 0;width:120px;vertical-align:top;color:{_MUTED};'
        f'font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">{label}</td>'
        f'<td style="padding:6px 0;color:{colour};font-size:14px;font-weight:{weight};'
        f'line-height:1.4;">{value}</td>'
        f'</tr>'
    )


def _cm2_legend(colors: dict) -> str:
    """A compact CM2% colour-band key so the window chips explain themselves.

    Built from the live config thresholds (red_below / yellow_low / green_high / yellow_high)
    so the legend always matches the rules the engine actually applies.
    """
    if not colors:
        return ""
    r = colors.get("red_below", 12)
    yl = colors.get("yellow_low", 15)
    gh = colors.get("green_high", 30)
    yh = colors.get("yellow_high", 33)
    bands = [
        ("Red", f"&lt; {r:g}%"),
        ("Yellow", f"{r:g}–{yl:g}% or {gh:g}–{yh:g}%"),
        ("Green", f"{yl:g}–{gh:g}%"),
        ("Blue", f"&gt; {yh:g}%"),
    ]
    parts = ""
    for name, rng in bands:
        dot = (f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
               f'background:{COLOUR_HEX.get(name, "#999")};margin-right:5px;"></span>')
        parts += (
            f'<span style="display:inline-block;margin:2px 14px 2px 0;color:{_MUTED};'
            f'font-size:12px;white-space:nowrap;">{dot}'
            f'<b style="color:{_INK};">{name}</b> {rng}</span>'
        )
    return (
        f'<div style="margin-top:12px;padding-top:10px;border-top:1px solid {_BORDER};">'
        f'<div style="color:{_MUTED};font-size:10px;text-transform:uppercase;'
        f'letter-spacing:0.6px;margin-bottom:6px;">CM2% colour bands</div>{parts}</div>'
    )


def _drivers_block(facts: dict) -> str:
    """The 'why' as a small scannable table (ranked cost drivers), not a paragraph.

    Uses the pre-computed, revenue-weighted top contributors. Returns "" when there is no
    single cost driver (e.g. a pure margin swing) so the caller can fall back to the text.
    """
    contribs = (facts or {}).get("top_contributors") or []
    if not contribs:
        return ""
    rows = ""
    for c in contribs:
        over = c["direction"] == "above max"
        sev = COLOUR_HEX["Red"] if c["deviation_pp"] >= 3 else COLOUR_HEX["Yellow"]
        rows += (
            f'<tr>'
            f'<td style="padding:9px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_INK};font-weight:600;">{c["line_item"]}</td>'
            f'<td style="padding:9px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_INK};text-align:right;">{c["pct"]:.1f}%</td>'
            f'<td style="padding:9px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_MUTED};text-align:center;">{c["target"]}</td>'
            f'<td style="padding:9px 12px;border-bottom:1px solid {_BORDER};text-align:right;'
            f'font-size:13px;color:{sev};font-weight:700;white-space:nowrap;">'
            f'{c["deviation_pp"]:.1f}pp {"over" if over else "under"}</td>'
            f'</tr>'
        )
    heads = "".join(
        f'<th align="{al}" style="padding:8px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">{lbl}</th>'
        for lbl, al in (("Cost line", "left"), ("Actual", "right"),
                        ("Target", "center"), ("vs target", "right"))
    )
    return (
        f'<tr><td style="padding:14px 28px 6px;">'
        f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.6px;margin-bottom:8px;">What\'s driving it</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;border:1px solid {_BORDER};border-radius:8px;'
        f'overflow:hidden;">'
        f'<tr style="background:{_BG};">{heads}</tr>{rows}</table>'
        f'</td></tr>'
    )


def render_digest_html(d: dict) -> str:
    """Daily digest for a busy FC manager. Built to be read top-down in ~10 seconds:
    a colour header, a one-line verdict, the biggest issue and the one action, the headline
    numbers — then the longer narrative demoted to an optional 'Detail' block below."""
    colour = d["colour"]
    accent = COLOUR_HEX.get(colour, _INK)
    tint = COLOUR_TINT.get(colour, _BG)
    rev = f'₹{int(d["revenue"]):,}'
    cm2 = f'{d["cm2"]:.1f}%'
    cm1 = f'{d["cm1"]:.1f}%'
    largest_breach = d.get("largest_breach", "None")
    action = d.get("action", "")
    has_breach = bool(largest_breach) and largest_breach != "None"

    header = _header(accent, f'Daily P&L Digest · {colour}', d["fc"],
                     f'{d["date"]} · {COLOUR_WORD.get(colour, "")}')

    # 1. The verdict — the first (and, for a busy reader, maybe only) thing they read.
    verdict = (
        f'<tr><td style="padding:20px 28px 4px;">'
        f'<div style="background:{tint};border-left:4px solid {accent};'
        f'padding:14px 18px;border-radius:8px;">'
        f'<div style="color:{_MUTED};font-size:11px;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.8px;">Bottom line</div>'
        f'<div style="color:{_INK};font-size:17px;font-weight:700;line-height:1.4;'
        f'margin-top:5px;">{_verdict(colour, has_breach)}</div>'
        f'</div></td></tr>'
    )

    # 2. The two facts that matter next: biggest issue + the one action.
    facts = (
        f'<tr><td style="padding:12px 28px 4px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'{_fact_row("Biggest issue", largest_breach if has_breach else "None — all cost lines in range", strong=has_breach)}'
        f'{_fact_row("Do next", action or "—", strong=True)}'
        f'</table></td></tr>'
    )

    # 3. Headline numbers (CM2 in the status colour).
    metrics = (
        f'<tr><td style="padding:14px 28px 8px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;text-align:center;">'
        f'<tr>{_metric("Revenue", rev)}{_metric("CM1", cm1)}'
        f'{_metric("CM2", cm2, accent)}</tr>'
        f'</table></td></tr>'
    )

    blocks = ""
    if d.get("trend"):
        blocks += (
            f'<tr><td style="padding:6px 28px;">'
            f'<div style="background:#fff8e6;border-left:3px solid {COLOUR_HEX["Yellow"]};'
            f'padding:10px 14px;border-radius:6px;color:{_INK};font-size:13px;">'
            f'<b>Trend:</b> {d["trend"]}</div></td></tr>'
        )
    # 4. The "why", structured: a ranked driver table beats a paragraph of numbers. Only a
    # margin-only day with no single cost driver falls back to the muted narrative.
    drivers = _drivers_block(d.get("facts") or {})
    if drivers:
        blocks += drivers
    elif d.get("insight"):
        blocks += (
            f'<tr><td style="padding:14px 28px 6px;">'
            f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
            f'letter-spacing:0.6px;margin-bottom:6px;">Detail</div>'
            f'<div style="color:{_MUTED};font-size:13px;line-height:1.5;">{d["insight"]}</div>'
            f'</td></tr>'
        )

    # Window summary as small colour chips.
    chips = ""
    for c, n in d.get("window_counts", {}).items():
        chips += (
            f'<span style="display:inline-block;background:{COLOUR_HEX.get(c, "#999")};'
            f'color:#fff;font-size:12px;font-weight:600;padding:3px 10px;border-radius:12px;'
            f'margin:2px 4px 2px 0;">{c}: {n}</span>'
        )
    # CM2% band legend, so the chips are self-explanatory (what does "Red" mean?).
    legend = _cm2_legend(d.get("cm2_colors") or {})
    window = (
        f'<tr><td style="padding:14px 28px 24px;">'
        f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.6px;margin-bottom:8px;">Last {d.get("window_days", 0)} days</div>'
        f'{chips}{legend}</td></tr>'
    )

    return _shell(header + verdict + facts + metrics + blocks + window,
                  preheader=f'{d["fc"]} {d["date"]}: {_verdict(colour, has_breach)}')


def render_owner_html(owner_items: list[str], breaches: list[dict], threshold: float,
                      scope: str) -> str:
    """Beautiful alert for a cost owner. `breaches` = list of dicts (fc,date,item,pct,dev,...)."""
    accent = COLOUR_HEX["Red"] if any(b["dev"] >= 3 for b in breaches) else COLOUR_HEX["Yellow"]
    title = ", ".join(owner_items)
    n = len(breaches)

    header = _header(accent, "Action needed · Cost line alert", title,
                     f'{n} material breach{"es" if n != 1 else ""} · scope: {scope.replace("_", " ")}')

    pt = "point" if threshold == 1 else "points"
    intro = (
        f'<tr><td style="padding:20px 28px 8px;color:{_INK};font-size:14px;line-height:1.55;">'
        f'The cost line(s) you own breached the target range by at least '
        f'<b>{threshold:g} percentage {pt}</b>. Please review and confirm whether this is '
        f'expected.</td></tr>'
    )

    # Breach table.
    rows = ""
    for b in sorted(breaches, key=lambda x: (str(x["fc"]), str(x["date"]), -x["dev"])):
        dev_colour = COLOUR_HEX["Red"] if b["dev"] >= 3 else COLOUR_HEX["Yellow"]
        rows += (
            f'<tr>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_MUTED};">{b["fc"]}<br>{b["date"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_INK};font-weight:600;">{b["item"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_INK};text-align:right;">{b["pct"]:.1f}%</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {_BORDER};font-size:13px;'
            f'color:{_MUTED};text-align:center;">{b["target"]}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid {_BORDER};text-align:right;">'
            f'<span style="color:{dev_colour};font-weight:700;font-size:13px;">'
            f'{b["direction"]}<br>+{b["dev"]:.1f}pp</span></td>'
            f'</tr>'
        )
    table = (
        f'<tr><td style="padding:14px 28px 24px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;border:1px solid {_BORDER};border-radius:8px;'
        f'overflow:hidden;">'
        f'<tr style="background:{_BG};">'
        f'<th align="left" style="padding:9px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">FC / Date</th>'
        f'<th align="left" style="padding:9px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">Line item</th>'
        f'<th align="right" style="padding:9px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">Actual</th>'
        f'<th align="center" style="padding:9px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">Target</th>'
        f'<th align="right" style="padding:9px 12px;font-size:11px;color:{_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.5px;">Deviation</th>'
        f'</tr>{rows}</table></td></tr>'
    )

    note = (
        f'<tr><td style="padding:0 28px 24px;color:{_MUTED};font-size:12px;line-height:1.5;">'
        f'Smaller sub-threshold breaches (if any) are visible on the P&amp;L dashboard\'s '
        f'Anomalies tab.</td></tr>'
    )

    return _shell(header + intro + table + note,
                  preheader=f'{n} material breach(es) in {title}')
