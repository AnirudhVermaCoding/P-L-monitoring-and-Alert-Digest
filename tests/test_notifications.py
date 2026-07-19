"""Tests for review-before-send drafts and run-scoped recipient routing."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.insights import generate_insights
from agent.notifier import (
    NotificationRouting,
    build_digests,
    deliver_notifications,
    is_valid_email,
    prepare_notifications,
)
from core.config import load_config
from core.engine import compute_pnl, detect_anomalies


def _results():
    cfg = load_config()
    rows = []
    for fc in ("FC-A", "FC-B", "FC-C"):
        rows.append({
            "Date": "2026-07-17", "FC": fc, "Revenue": 100000,
            "Manpower": 30000, "Packaging": 9000, "Power and Fuel": 11000,
            "FC Rent": 16500, "Equipment Rentals": 6500, "Overheads": 11000,
        })
    pnl = compute_pnl(pd.DataFrame(rows), cfg)
    anomalies = detect_anomalies(pnl, cfg)
    insights, _ = generate_insights(pnl, anomalies, cfg)
    digests = build_digests(pnl, anomalies, insights, cfg)
    return cfg, pnl, anomalies, digests


def test_manager_per_fc_and_shared_manpower_owner():
    cfg, pnl, anomalies, digests = _results()
    routing = NotificationRouting(
        fc_managers={
            "FC-A": "a.manager@example.com",
            "FC-B": "b.manager@example.com",
            "FC-C": "c.manager@example.com",
        },
        line_owners={"Manpower": "manpower.owner@example.com"},
        enabled_owner_items={"Manpower"},
    )
    drafts = prepare_notifications(digests, anomalies, pnl, cfg, routing)
    digests_only = [draft for draft in drafts if draft["kind"] == "digest"]
    owners = [draft for draft in drafts if draft["kind"] == "owner_alert"]
    assert {draft["to"] for draft in digests_only} == {
        "a.manager@example.com", "b.manager@example.com", "c.manager@example.com",
    }
    assert len(owners) == 1, owners
    assert owners[0]["to"] == "manpower.owner@example.com"
    assert "Packaging" not in owners[0]["subject"], owners[0]["subject"]
    assert all(draft["recipient_valid"] for draft in drafts)
    print("PASS test_manager_per_fc_and_shared_manpower_owner")


def test_invalid_addresses_are_drafts_but_never_delivered():
    cfg, pnl, anomalies, digests = _results()
    routing = NotificationRouting(
        fc_managers={"default": "not-an-email"},
        line_owners={"Manpower": ""},
        enabled_owner_items={"Manpower"},
    )
    drafts = prepare_notifications(digests, anomalies, pnl, cfg, routing)
    assert drafts and not any(draft["recipient_valid"] for draft in drafts)
    log = deliver_notifications(drafts, "invalid-recipient-test")
    assert {entry["status"] for entry in log} == {"skipped_invalid_recipient"}, log
    assert {entry["mode"] for entry in log} == {"not_delivered"}, log
    assert is_valid_email("one@example.com")
    assert not is_valid_email("one@example.com,two@example.com")
    print("PASS test_invalid_addresses_are_drafts_but_never_delivered")


def run_all():
    test_manager_per_fc_and_shared_manpower_owner()
    test_invalid_addresses_are_drafts_but_never_delivered()
    print("\nALL NOTIFICATION TESTS PASSED")


if __name__ == "__main__":
    run_all()
