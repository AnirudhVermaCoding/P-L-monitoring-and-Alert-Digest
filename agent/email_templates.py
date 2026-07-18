"""HTML email templates — clean, scannable, executive-friendly.

Email clients are picky: they strip <style> blocks and <head>, and many don't support flexbox
or grid. So these templates use table-based layout with INLINE styles only (no <style> block,
no media queries), which renders consistently in Gmail, Outlook, and Apple Mail. Designed to be
read in 10 seconds: a colour header, the headline number, a small metrics row, a breach table,
the plain-language insight, and one recommended action.
"""
from __future__ import annotations

COLOUR_HEX = {"Red": "#d64545", "Yellow": "#d9a441", "Green": "#2e9e5b", "Blue": "#3b7dd8"}
COLOUR_WORD = {
    "Red": "Margin below threshold",
    "Yellow": "Borderline margin",
    "Green": "Healthy",
    "Blue": "Unusually high — verify costs",
}
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


def render_digest_html(d: dict) -> str:
    """Beautiful daily digest for an FC manager. `d` is the structured digest dict."""
    colour = d["colour"]
    accent = COLOUR_HEX.get(colour, _INK)
    rev = f'₹{int(d["revenue"]):,}'
    cm2 = f'{d["cm2"]:.1f}%'
    cm1 = f'{d["cm1"]:.1f}%'

    header = _header(accent, f'Daily P&L Digest · {colour}', d["fc"],
                     f'{d["date"]} · {COLOUR_WORD.get(colour, "")}')

    metrics = (
        f'<tr><td style="padding:20px 28px 8px;">'
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
    if d.get("insight"):
        blocks += (
            f'<tr><td style="padding:14px 28px 6px;">'
            f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
            f'letter-spacing:0.6px;margin-bottom:6px;">What happened</div>'
            f'<div style="color:{_INK};font-size:14px;line-height:1.55;">{d["insight"]}</div>'
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
    window = (
        f'<tr><td style="padding:14px 28px 24px;">'
        f'<div style="color:{_MUTED};font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.6px;margin-bottom:8px;">Last {d.get("window_days", 0)} days</div>'
        f'{chips}</td></tr>'
    )

    return _shell(header + metrics + blocks + window,
                  preheader=f'{d["fc"]} {d["date"]}: CM2 {cm2} ({colour})')


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
