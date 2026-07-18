"""Headless runner for the FC P&L pipeline (no UI). Useful for demos and evaluators.

Usage:
    python run_pipeline.py [path-to-csv-or-xlsx]

Defaults to data/extended_test_data.csv. Writes computed outputs to outputs/ and
notification emails to outputs/outbox/. Works with zero API keys.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # .env is the single source of truth

from agent.graph import run_pipeline  # noqa: E402
from core.memory import init_db, record_daily_results, record_run  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = REPO_ROOT / "data" / "extended_test_data.csv"
OUTPUTS = REPO_ROOT / "outputs"
RUN_DATE = "2026-07-17"


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_SOURCE)
    print(f"Running pipeline on: {source}\n")

    init_db()
    result = run_pipeline(source, run_date=RUN_DATE,
                          progress_cb=lambda m: print(f"  … {m}"))

    if result.get("errors"):
        print("\nINPUT ERROR:")
        for e in result["errors"]:
            print("  -", e)
        return 0  # clean exit with message, not a crash

    pnl = result["pnl_df"]
    anoms = result["anomalies_df"]
    insights = result["insights"]
    notifications = result["notifications"]

    OUTPUTS.mkdir(exist_ok=True)
    pnl.to_csv(OUTPUTS / "computed_pnl.csv", index=False)
    anoms.to_csv(OUTPUTS / "anomalies_log.csv", index=False)
    try:
        pnl.to_excel(OUTPUTS / "computed_pnl.xlsx", index=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  (xlsx export skipped: {exc})")

    run_id = f"run-{RUN_DATE}"
    record_run(run_id, RUN_DATE, source, "ok",
               {"days": len(pnl), "anomalies": len(anoms), "llm_used": result["llm_used"]})
    record_daily_results(run_id, pnl, anoms)

    print("\n===== SUMMARY =====")
    print(f"Days analysed:     {len(pnl)}  across FCs: {sorted(pnl['FC'].unique())}")
    print(f"Colour counts:     {pnl['Colour'].value_counts().to_dict()}")
    print(f"Anomalies flagged: {len(anoms)}")
    print(f"Insights:          {len(insights)}  (LLM used: {result['llm_used']})")
    print(f"Notifications:     {len(notifications)}  (mode: "
          f"{notifications[0]['mode'] if notifications else 'n/a'})")
    print("\nSample insights:")
    for ins in insights[:3]:
        print(f"  [{ins['Colour']}] {ins['FC']} {ins['Date']}: {ins['insight'][:160]}")
    print(f"\nOutputs written to {OUTPUTS}/ and outputs/outbox/{RUN_DATE}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
