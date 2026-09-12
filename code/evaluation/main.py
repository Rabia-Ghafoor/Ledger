#!/usr/bin/env python3
"""
Self-scoring helper: run the solution's decision engine against
dataset/sample_requests.csv (25 requests with known-correct output columns)
and report a per-field match rate. This is the "score yourself on the solved
samples" step from the README's suggested workflow -- it is a development
aid, not part of the graded submission logic.

Run:
    python3 code/evaluation/main.py
"""

import os
import sys
from datetime import timedelta

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)

import main as engine  # noqa: E402  (code/main.py)

FIELDS = [
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
]


def run():
    profiles = engine.load_profiles()
    home_currency_by_user = {uid: p.home_currency for uid, p in profiles.items()}
    rates = engine.RateTable(engine.read_csv("exchange_rates.csv"))
    all_events = engine.load_events(rates, home_currency_by_user)
    events_by_id = {e.event_id: e for e in all_events}
    messages_by_event = engine.load_messages()
    engine.apply_message_overrides(events_by_id, messages_by_event)

    events_by_user = {}
    for e in all_events:
        events_by_user.setdefault(e.user_id, []).append(e)
    series_by_user = engine.detect_recurring_series(events_by_user)
    options_by_request = engine.load_payment_options()

    samples = engine.read_csv("sample_requests.csv")
    match_counts = {f: 0 for f in FIELDS}
    mismatches = []

    for req in samples:
        profile = profiles[req["user_id"]]
        request_date = engine.parse_date(req["request_date"])
        window_end = request_date + timedelta(days=engine.FORECAST_DAYS)
        result = engine.decide(
            req, profile, events_by_user.get(req["user_id"], []),
            series_by_user.get(req["user_id"], []),
            options_by_request.get(req["request_id"], []), window_end,
        )
        earliest = result["earliest_date_for_full_payment"]
        got = {
            "amount_safe_to_pay": engine.fmt_amount(result["amount_safe_to_pay"]),
            "affordability_status": result["affordability_status"],
            "recommended_payment_method": result["recommended_payment_method"],
            "payment_plan": result["payment_plan"],
            "earliest_date_for_full_payment": engine.fmt_date(earliest) if earliest else "",
            "spending_changes_needed": "|".join(result["spending_changes"]) if result["spending_changes"] else "none",
        }
        row_diffs = []
        for f in FIELDS:
            expected = req[f]
            close_enough = str(got[f]) == str(expected) or (
                f == "amount_safe_to_pay"
                and abs(float(got[f]) - float(expected)) < 1.0
            )
            if close_enough:
                match_counts[f] += 1
            else:
                row_diffs.append((f, got[f], expected))
        if row_diffs:
            mismatches.append((req["request_id"], row_diffs))

    n = len(samples)
    print(f"Scored {n} sample requests from dataset/sample_requests.csv\n")
    for f in FIELDS:
        print(f"  {f}: {match_counts[f]}/{n}")

    print("\nMismatches:")
    for request_id, diffs in mismatches:
        print(f"  {request_id}:")
        for f, got, expected in diffs:
            print(f"    {f}: got={got!r} expected={expected!r}")


if __name__ == "__main__":
    run()
