#!/usr/bin/env python3
"""
Buy or Wait? -- deterministic, rule-based financial decision agent.

Reads dataset/*.csv, reconstructs each user's financial state, forecasts a
90-day cash-flow window, and decides -- per request -- whether to pay in
full, pay partially, use installments, wait, or not proceed.

Run:
    python3 code/main.py

No API keys / LLM calls are used: the engine is fully deterministic rule
logic over the structured data. The one exception is the 16 financial_events
rows with a blank `amount`, whose true value only exists inside a linked
receipt/payslip image (dataset/media/images/<image_id>.png). Those 16 values
were read directly from the images (by a human/agent looking at the PNG,
since OCR/vision would require a paid model call) and are hardcoded below in
RESOLVED_IMAGE_AMOUNTS, each traceable to its source event_id and image_id.
"""

import csv
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE_DIR)
DATASET = os.path.join(ROOT, "dataset")
OUTPUT_PATH = os.path.join(ROOT, "output.csv")

FORECAST_DAYS = 90

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

# --------------------------------------------------------------------------
# Amounts extracted from the 16 financial_events rows with a blank `amount`.
# Each is resolved from the receipt/payslip/invoice image linked via
# images.csv (image_id -> related_event_id). Amount is in the event's own
# `currency` (converted to home currency later, same as any other event).
# --------------------------------------------------------------------------
RESOLVED_IMAGE_AMOUNTS = {
    "event_253": 4365000,      # image_01: Aug-2019 payslip, Net Pay IDR 4,365,000
    "event_1442": 100000,      # image_02: rent receipt, "Outstanding rent balance" = Balance Due INR 1,00,000
    "event_1545": 41272,       # image_03: grocery bill of supply, Net Amount / Cash Paid INR 41,272
    "event_1700": 2854,        # image_04: food-delivery order screenshot, Item Bill INR 2,854
    "event_1786": 704.05,      # image_05: telecom account summary, Amount due till 06-Feb-2026 INR 704.05
    "event_3051": 1995,        # image_06: grocery tax invoice, Total INR 1,995.00
    "event_3231": 8528,        # image_07: restaurant tax invoice, Grand Total INR 8,528
    "event_4535": 15339,       # image_08: maintenance receipt, Total Amount Received INR 15,339.00
    "event_5170": 723,         # image_09: water-bill receipt, Total Amount Received INR 723.00
    "event_6033": 79679.26,    # image_10: grocery tax invoice, Balance Due INR 79,679.26
    "event_6859": 3650,        # image_11: hospital provisional bill, Total Bill Amount INR 3,650.00
    "event_7307": 33.50,       # image_12: CityCab taxi receipt, Total USD 33.50
    "event_7941": 2298,        # image_13: order summary, Total paid INR 2,298
    "event_9421": 4593,        # image_14: handwritten pharmacy bill, TOTAL INR 4,593.00
    "event_9806": 9968,        # image_15: airline tax invoice, Grand Total (Incl Taxes) INR 9,968.00
    "event_10521": 393.22,     # image_16: EV-charging invoice, Total INR 393.22
}

RECURRING_EVENT_TYPES = {"expense", "subscription", "debt_payment"}
INACTIVE_STATUSES = {"cancelled", "failed"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def fmt_date(d):
    return d.strftime("%Y-%m-%d")


def parse_pipe_list(s):
    s = (s or "").strip()
    if not s:
        return []
    return [p.strip() for p in s.split("|") if p.strip()]


def to_float(s, default=None):
    s = (s or "").strip()
    if s == "":
        return default
    return float(s)


def fmt_amount(x):
    r = round(float(x) + 1e-9, 2)
    if abs(r - round(r)) < 1e-6:
        return str(int(round(r)))
    s = f"{r:.2f}"
    if s.endswith("0"):
        s = s[:-1]
    if s.endswith("."):
        s = s[:-1]
    return s


def read_csv(name):
    path = os.path.join(DATASET, name)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------
# Exchange rates
# --------------------------------------------------------------------------
class RateTable:
    def __init__(self, rows):
        self.direct = {}          # (date, from, to) -> rate
        self.pairs_by_date = defaultdict(dict)  # date -> {(from,to): rate}
        self.all_dates_for_pair = defaultdict(list)  # (from,to) -> sorted [dates]
        for r in rows:
            d = r["rate_date"].strip()
            f = r["from_currency"].strip()
            t = r["to_currency"].strip()
            rate = float(r["rate"])
            self.direct[(d, f, t)] = rate
            self.pairs_by_date[d][(f, t)] = rate
            self.all_dates_for_pair[(f, t)].append(d)
        for k in self.all_dates_for_pair:
            self.all_dates_for_pair[k].sort()

    def _lookup_same_date(self, d, f, t):
        if f == t:
            return 1.0
        if (d, f, t) in self.direct:
            return self.direct[(d, f, t)]
        if (d, t, f) in self.direct:
            return 1.0 / self.direct[(d, t, f)]
        # bridge via USD
        if f != "USD" and t != "USD":
            a = self._lookup_same_date(d, f, "USD")
            b = self._lookup_same_date(d, "USD", t)
            if a is not None and b is not None:
                return a * b
        return None

    def _nearest_date(self, target, f, t):
        candidates = set(self.all_dates_for_pair.get((f, t), []))
        candidates |= set(self.all_dates_for_pair.get((t, f), []))
        if not candidates:
            return None
        try:
            td = parse_date(target)
        except ValueError:
            return None
        best = min(candidates, key=lambda d: abs((parse_date(d) - td).days))
        return best

    def convert(self, amount, f, t, on_date):
        if f == t or amount == 0:
            return amount
        rate = self._lookup_same_date(on_date, f, t)
        if rate is None:
            nearest = self._nearest_date(on_date, f, t)
            if nearest is not None:
                rate = self._lookup_same_date(nearest, f, t)
        if rate is None:
            # last resort: bridge through USD using nearest available dates
            fu = self._nearest_date(on_date, f, "USD") or self._nearest_date(on_date, "USD", f)
            ut = self._nearest_date(on_date, "USD", t) or self._nearest_date(on_date, t, "USD")
            a = self._lookup_same_date(fu, f, "USD") if fu else None
            b = self._lookup_same_date(ut, "USD", t) if ut else None
            if a is not None and b is not None:
                rate = a * b
        if rate is None:
            raise ValueError(f"No exchange rate path {f}->{t} near {on_date}")
        return amount * rate


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
class Event:
    __slots__ = (
        "event_id", "user_id", "event_type", "description", "category",
        "direction", "currency", "raw_amount", "amount_home", "event_date",
        "settlement_date", "status", "linked_event_id", "flexibility",
        "minimum_allowed_amount",
    )


def load_events(rates: RateTable, home_currency_by_user):
    rows = read_csv("financial_events.csv")
    events = []
    for r in rows:
        e = Event()
        e.event_id = r["event_id"]
        e.user_id = r["user_id"]
        e.event_type = r["event_type"]
        e.description = r["description"]
        e.category = r["category"]
        e.direction = r["direction"]
        e.currency = r["currency"]
        amt_str = (r["amount"] or "").strip()
        if amt_str == "":
            if e.event_id in RESOLVED_IMAGE_AMOUNTS:
                e.raw_amount = float(RESOLVED_IMAGE_AMOUNTS[e.event_id])
            else:
                e.raw_amount = 0.0
        else:
            e.raw_amount = float(amt_str)
        e.event_date = r["event_date"]
        e.settlement_date = r["settlement_date"] or r["event_date"]
        e.status = r["status"]
        e.linked_event_id = r["linked_event_id"]
        e.flexibility = r["flexibility"]
        e.minimum_allowed_amount = to_float(r["minimum_allowed_amount"], None)

        home_cur = home_currency_by_user.get(e.user_id, e.currency)
        try:
            e.amount_home = rates.convert(e.raw_amount, e.currency, home_cur, e.settlement_date)
        except ValueError:
            e.amount_home = e.raw_amount
        events.append(e)
    return events


class Profile:
    __slots__ = (
        "user_id", "home_currency", "current_available_balance",
        "minimum_balance_to_keep", "financial_priorities",
        "expense_categories_to_protect", "expense_categories_willing_to_reduce",
        "expense_categories_willing_to_stop", "payment_methods_accepted",
        "max_installment_months",
    )


def load_profiles():
    rows = read_csv("financial_profiles.csv")
    profiles = {}
    for r in rows:
        p = Profile()
        p.user_id = r["user_id"]
        p.home_currency = r["home_currency"]
        p.current_available_balance = float(r["current_available_balance"])
        p.minimum_balance_to_keep = float(r["minimum_balance_to_keep"])
        p.financial_priorities = set(parse_pipe_list(r["financial_priorities"]))
        p.expense_categories_to_protect = set(parse_pipe_list(r["expense_categories_to_protect"]))
        p.expense_categories_willing_to_reduce = set(parse_pipe_list(r["expense_categories_user_is_willing_to_reduce"]))
        p.expense_categories_willing_to_stop = set(parse_pipe_list(r["expense_categories_user_is_willing_to_stop"]))
        p.payment_methods_accepted = set(parse_pipe_list(r["payment_methods_user_will_consider"]))
        p.max_installment_months = None
        mim = (r["max_installment_months"] or "").strip()
        if mim:
            p.max_installment_months = int(float(mim))
        profiles[p.user_id] = p
    return profiles


class PaymentOption:
    __slots__ = (
        "payment_option_id", "request_id", "payment_method", "payment_amount",
        "number_of_payments", "first_payment_date", "payment_frequency_days",
        "financing_fee", "total_payable_amount",
    )


def load_payment_options():
    rows = read_csv("request_payment_options.csv")
    by_request = defaultdict(list)
    for r in rows:
        o = PaymentOption()
        o.payment_option_id = r["payment_option_id"]
        o.request_id = r["request_id"]
        o.payment_method = r["payment_method"]
        o.payment_amount = float(r["payment_amount"])
        o.number_of_payments = int(float(r["number_of_payments"]))
        o.first_payment_date = r["first_payment_date"]
        o.payment_frequency_days = to_float(r["payment_frequency_days"], 0) or 0
        o.financing_fee = to_float(r["financing_fee"], 0) or 0
        o.total_payable_amount = to_float(r["total_payable_amount"], None)
        by_request[o.request_id].append(o)
    return by_request


def load_messages():
    rows = read_csv("messages.csv")
    by_event = defaultdict(list)
    for r in rows:
        if r["related_event_id"]:
            by_event[r["related_event_id"]].append(r)
    return by_event


# --------------------------------------------------------------------------
# Lightweight, conservative message-driven overrides.
# Only applied to messages that directly reference a specific event_id
# (messages.csv `related_event_id`), per the dataset contract. Free-text
# messages tied only to a user/request (no event link) are treated as
# untrusted context, not as instructions to alter forecast numbers -- this
# avoids inventing unsupported financial facts from ambiguous / multilingual
# free text.
# --------------------------------------------------------------------------
CANCEL_WORDS = ["cancel", "cancelled", "canceled", "batal", "dibatalkan", "storniert", "annulliert"]
DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")


def apply_message_overrides(events_by_id, messages_by_event):
    for event_id, msgs in messages_by_event.items():
        e = events_by_id.get(event_id)
        if e is None:
            continue
        text = " ".join(m["message_text"] for m in msgs).lower()
        if any(w in text for w in CANCEL_WORDS):
            e.status = "cancelled"
            continue
        m = DATE_RE.search(text)
        if m and ("postpone" in text or "delay" in text or "ditunda" in text or "reschedul" in text):
            new_date = m.group(1)
            e.event_date = new_date
            e.settlement_date = new_date


# --------------------------------------------------------------------------
# Recurring-expense detection
# --------------------------------------------------------------------------
class RecurringSeries:
    __slots__ = ("user_id", "description", "category", "cycle_days",
                 "last_date", "last_amount", "representative", "flexibility",
                 "minimum_allowed_amount", "direction")


def detect_recurring_series(events_by_user):
    """Detect recurring cash-flow series per user from settled/known history:
    recurring expenses (debit: expense/subscription/debt_payment) AND
    recurring salary (credit income, category 'salary') -- most users have no
    explicit future salary row and only show a repeating settled history, so
    salary must be projected the same way recurring expenses are, or the
    90-day forecast would show one-way depletion with no income at all."""
    series_by_user = defaultdict(list)
    for user_id, evs in events_by_user.items():
        groups = defaultdict(list)
        for e in evs:
            if e.status in INACTIVE_STATUSES:
                continue
            is_recurring_expense = e.event_type in RECURRING_EVENT_TYPES and e.direction == "debit"
            is_recurring_income = e.event_type == "income" and e.category == "salary" and e.direction == "credit"
            if not (is_recurring_expense or is_recurring_income):
                continue
            # Salary descriptions embed the month/occasion (e.g. "August 2019
            # net salary", "Prorated first salary", "Next confirmed salary")
            # and never repeat verbatim, so group all of a user's salary rows
            # under one key; recurring expense descriptions repeat verbatim
            # every cycle, so those group correctly by their own text.
            key = "__salary__" if is_recurring_income else e.description
            groups[key].append(e)
        for desc, group in groups.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda e: e.event_date)
            first_d = parse_date(group[0].event_date)
            last_d = parse_date(group[-1].event_date)
            span = (last_d - first_d).days
            if span <= 0:
                continue
            # Average gap over the full observed history, not the median of
            # individual gaps: real vendor-level spending is irregular, and a
            # median can lock onto a coincidentally-repeated short gap and
            # over-project frequency (e.g. two same-week grocery trips
            # followed by a two-month gap). The average cadence over the
            # whole history is the conservative, representative estimate.
            cycle = int(round(span / (len(group) - 1)))
            cycle = max(cycle, 1)
            last = group[-1]
            s = RecurringSeries()
            s.user_id = user_id
            s.description = desc
            s.category = last.category
            s.cycle_days = cycle
            s.last_date = parse_date(last.event_date)
            s.last_amount = last.amount_home
            s.representative = last.event_id
            s.flexibility = last.flexibility
            s.minimum_allowed_amount = last.minimum_allowed_amount
            s.direction = last.direction
            series_by_user[user_id].append(s)
    return series_by_user


# --------------------------------------------------------------------------
# Forecast engine
# --------------------------------------------------------------------------
class Flow:
    __slots__ = ("date", "amount", "event_id", "is_projected", "description", "category")


def build_explicit_flows(events, start, end):
    """Explicit financial_events rows that move cash in (start, end]."""
    flows = []
    for e in events:
        if e.status in INACTIVE_STATUSES:
            continue
        if e.event_type == "investment_valuation":
            continue  # unrealized, never cash
        try:
            d = parse_date(e.settlement_date)
        except ValueError:
            continue
        if d <= start or d > end:
            continue
        if e.direction == "debit":
            if e.status in ("settled", "pending", "scheduled"):
                amt = -abs(e.amount_home)
            else:
                continue
        else:  # credit
            # Only a confirmed (scheduled) salary counts before it settles;
            # everything else (pending bonuses, refunds, investment gains,
            # windfalls, etc.) is excluded until settled -- and a settled
            # future-dated credit shouldn't occur in well-formed data.
            if e.status == "scheduled" and e.event_type == "income":
                amt = abs(e.amount_home)
            elif e.status == "settled":
                amt = abs(e.amount_home)
            else:
                continue
        f = Flow()
        f.date, f.amount, f.event_id = d, amt, e.event_id
        f.is_projected, f.description, f.category = False, e.description, e.category
        flows.append(f)
    return flows


def build_projected_flows(series_list, start, end, explicit_flows, overrides=None):
    """Project recurring debit series forward, skipping dates already
    covered by an explicit row for the same description. `overrides` maps a
    series description to "stop" (drop every future occurrence) or
    ("reduce", new_amount) (use new_amount instead of the historical
    amount), used when simulating a candidate spending change."""
    overrides = overrides or {}
    covered = defaultdict(list)
    covered_salary = []
    for f in explicit_flows:
        covered[f.description].append(f.date)
        if f.category == "salary":
            covered_salary.append(f.date)

    flows = []
    for s in series_list:
        override = overrides.get(s.description)
        if override == "stop":
            continue
        amount = override[1] if isinstance(override, tuple) else s.last_amount
        is_salary = s.direction == "credit"

        next_date = s.last_date + timedelta(days=s.cycle_days)
        guard = 0
        while next_date <= end and guard < 400:
            guard += 1
            if next_date > start:
                near_pool = covered_salary if is_salary else covered.get(s.description, [])
                near = any(abs((next_date - cd).days) <= 10 for cd in near_pool)
                if not near:
                    signed = abs(amount) if s.direction == "credit" else -abs(amount)
                    f = Flow()
                    f.date, f.amount = next_date, signed
                    f.event_id, f.is_projected = None, True
                    f.description, f.category = s.description, s.category
                    flows.append(f)
            next_date = next_date + timedelta(days=s.cycle_days)
    return flows


def apply_overrides_to_explicit(explicit_flows, overrides):
    """Drop/shrink explicit (already-scheduled) flows matching a stopped or
    reduced recurring series, so a change also affects the very next
    occurrence when that occurrence is already an explicit row."""
    if not overrides:
        return explicit_flows
    out = []
    for f in explicit_flows:
        override = overrides.get(f.description)
        if override == "stop" and f.amount < 0:
            continue
        if isinstance(override, tuple) and f.amount < 0 and abs(f.amount) > override[1]:
            nf = Flow()
            nf.date, nf.amount, nf.event_id = f.date, -abs(override[1]), f.event_id
            nf.is_projected, nf.description, nf.category = f.is_projected, f.description, f.category
            out.append(nf)
            continue
        out.append(f)
    return out


def min_running_balance(balance_now, flows, start, end):
    """Return the minimum balance reached over (start, end] given flows,
    and the full chronological timeline of (date, balance) checkpoints
    (including the starting point at `start`)."""
    ordered = sorted(flows, key=lambda f: f.date)
    running = balance_now
    timeline = [(start, running)]
    for f in ordered:
        running += f.amount
        timeline.append((f.date, running))
    worst = min(b for _, b in timeline)
    return worst, timeline


def earliest_safe_full_payment_date(balance_now, flows, min_balance, requested_amount,
                                     start, end):
    """First date d such that paying requested_amount in full on d, given the
    natural (unpaid) forecast, never breaks minimum_balance from d onward."""
    ordered = sorted(flows, key=lambda f: f.date)
    dates = sorted({start} | {f.date for f in ordered})

    balance_after = {start: balance_now}
    running = balance_now
    for f in ordered:
        running += f.amount
        balance_after[f.date] = running

    for d in dates:
        suffix_min = min(b for dt, b in balance_after.items() if dt >= d)
        if suffix_min - requested_amount >= min_balance - 1e-6:
            return d
    return None


def amount_safe_today(balance_now, flows, min_balance, requested_amount, start, end):
    worst, _ = min_running_balance(balance_now, flows, start, end)
    slack = worst - min_balance
    return max(0.0, min(requested_amount, slack))


# --------------------------------------------------------------------------
# Spending-change candidates
# --------------------------------------------------------------------------
def spending_change_candidates(profile, series_list, window_end):
    """Recurring, flexible, non-protected expenses the user permits changing,
    that still have an upcoming occurrence within the forecast window."""
    candidates = []
    for s in series_list:
        if s.direction != "debit":
            continue
        if s.category in profile.expense_categories_to_protect:
            continue

        can_stop = s.flexibility in ("stoppable", "reducible_or_stoppable") and \
            s.category in profile.expense_categories_willing_to_stop
        can_reduce = s.flexibility in ("reducible", "reducible_or_stoppable") and \
            s.category in profile.expense_categories_willing_to_reduce and \
            s.minimum_allowed_amount is not None and s.minimum_allowed_amount < s.last_amount

        if can_stop:
            candidates.append({
                "kind": "stop", "event_id": s.representative, "amount_freed": s.last_amount,
                "series": s,
            })
        if can_reduce:
            freed = s.last_amount - s.minimum_allowed_amount
            if freed > 0:
                candidates.append({
                    "kind": "reduce", "event_id": s.representative, "amount_freed": freed,
                    "new_amount": s.minimum_allowed_amount, "series": s,
                })
    candidates.sort(key=lambda c: -c["amount_freed"])
    return candidates


# --------------------------------------------------------------------------
# Decision engine per request
# --------------------------------------------------------------------------
def simulate_installment_safety(balance_now, base_flows, min_balance, option, start, end):
    """Simulate an installment option's own payments as additional debit
    flows on top of the background forecast; extend horizon as needed to
    cover the full plan length."""
    pay_dates = []
    d0 = parse_date(option.first_payment_date)
    d = d0
    for i in range(option.number_of_payments):
        pay_dates.append(d)
        d = d + timedelta(days=int(option.payment_frequency_days) if option.payment_frequency_days else 0)
    plan_end = max(pay_dates) if pay_dates else start
    horizon_end = max(end, plan_end)

    flows = list(base_flows)
    for pd in pay_dates:
        f = Flow()
        f.date, f.amount, f.event_id, f.is_projected = pd, -abs(option.payment_amount), None, False
        f.description, f.category = "installment_payment", "installment"
        flows.append(f)

    worst, _ = min_running_balance(balance_now, flows, start, horizon_end)
    safe = worst >= min_balance - 1e-6
    return safe, pay_dates, plan_end


def build_payment_plan_str(pairs):
    return "|".join(f"{fmt_date(d)}:{fmt_amount(a)}" for d, a in pairs)


def decide(request, profile, events, series_list, options, window_end):
    request_id = request["request_id"]
    request_date = parse_date(request["request_date"])
    desired_completion = parse_date(request["desired_completion_date"])
    requested_amount = float(request["requested_amount"])
    allows_partial = request["allows_partial_payment"].strip().lower() == "true"

    explicit_flows = build_explicit_flows(events, request_date, window_end)
    projected_flows = build_projected_flows(series_list, request_date, window_end, explicit_flows)
    base_flows = explicit_flows + projected_flows

    balance_now = profile.current_available_balance
    min_balance = profile.minimum_balance_to_keep

    amt_safe0 = amount_safe_today(balance_now, base_flows, min_balance, requested_amount,
                                   request_date, window_end)
    earliest_full = earliest_safe_full_payment_date(balance_now, base_flows, min_balance,
                                                      requested_amount, request_date, window_end)

    accepted = profile.payment_methods_accepted
    candidates = []  # each: dict with method, plan pairs, completes_by_deadline, uses_changes,
                      # total_paid, first_date, n_payments, tiebreak_id, status

    # full_payment now
    if "full_payment" in accepted and amt_safe0 >= requested_amount - 1e-6 and earliest_full == request_date:
        candidates.append({
            "method": "full_payment", "plan": [(request_date, requested_amount)],
            "completes_by_deadline": request_date <= desired_completion,
            "uses_changes": False, "total_paid": requested_amount,
            "first_date": request_date, "n_payments": 1, "tiebreak": "",
            "status": "affordable_now",
        })

    # partial_payment
    if (allows_partial and "partial_payment" in accepted and 0 < amt_safe0 < requested_amount - 1e-9
            and earliest_full is not None and earliest_full <= desired_completion):
        remainder = requested_amount - amt_safe0
        candidates.append({
            "method": "partial_payment",
            "plan": [(request_date, amt_safe0), (earliest_full, remainder)],
            "completes_by_deadline": earliest_full <= desired_completion,
            "uses_changes": False, "total_paid": requested_amount,
            "first_date": request_date, "n_payments": 2, "tiebreak": "",
            "status": "affordable_with_plan",
        })

    # installments
    if "installments" in accepted and profile.max_installment_months:
        for opt in options:
            if opt.payment_method != "installments":
                continue
            if opt.number_of_payments > profile.max_installment_months:
                continue
            safe, pay_dates, plan_end = simulate_installment_safety(
                balance_now, base_flows, min_balance, opt, request_date, window_end)
            if not safe:
                continue
            total_paid = opt.total_payable_amount if opt.total_payable_amount is not None \
                else opt.payment_amount * opt.number_of_payments
            candidates.append({
                "method": "installments",
                "plan": list(zip(pay_dates, [opt.payment_amount] * len(pay_dates))),
                "completes_by_deadline": plan_end <= desired_completion,
                "uses_changes": False, "total_paid": total_paid,
                "first_date": pay_dates[0] if pay_dates else request_date,
                "n_payments": opt.number_of_payments, "tiebreak": opt.payment_option_id,
                "status": "affordable_with_plan",
            })

    # wait (full payment later)
    if "full_payment" in accepted and earliest_full is not None and earliest_full > request_date:
        candidates.append({
            "method": "wait", "plan": [(earliest_full, requested_amount)],
            "completes_by_deadline": earliest_full <= desired_completion,
            "uses_changes": False, "total_paid": requested_amount,
            "first_date": earliest_full, "n_payments": 1, "tiebreak": "",
            "status": "affordable_later",
        })

    if not any(c["completes_by_deadline"] for c in candidates):
        candidates = try_spending_changes(
            profile, series_list, request_date, desired_completion, window_end,
            balance_now, min_balance, requested_amount, explicit_flows, accepted,
            allows_partial, options, candidates)

    chosen = rank_candidates(candidates)

    # amount_safe_to_pay is always reported "before optional spending changes",
    # regardless of which plan is finally recommended.
    amt_safe_final = max(0.0, min(requested_amount, amt_safe0))

    if chosen is None:
        return {
            "amount_safe_to_pay": amt_safe_final,
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": earliest_full,
            "spending_changes": [],
            "explanation": explain_not_recommended(profile, requested_amount, amt_safe_final, request_date),
        }

    return {
        "amount_safe_to_pay": amt_safe_final,
        "affordability_status": chosen["status"],
        "recommended_payment_method": chosen["method"],
        "payment_plan": build_payment_plan_str(chosen["plan"]),
        "earliest_date_for_full_payment": earliest_full,
        "spending_changes": chosen.get("changes", []),
        "explanation": explain_choice(profile, request, chosen, requested_amount, amt_safe0, earliest_full),
    }


def try_spending_changes(profile, series_list, request_date, desired_completion,
                          window_end, balance_now, min_balance, requested_amount,
                          explicit_flows, accepted, allows_partial, options, existing_candidates):
    """Greedily apply up to 3 permitted stop/reduce changes (largest amount
    freed first) to recurring flexible expenses, stopping as soon as a safe
    plan that completes the request by its deadline is found."""
    cands = spending_change_candidates(profile, series_list, window_end)
    if not cands:
        return existing_candidates

    overrides = {}
    applied = []
    new_candidates = list(existing_candidates)

    for c in cands:
        if len(applied) >= 3:
            break
        s = c["series"]
        if s.description in overrides:
            continue
        overrides[s.description] = "stop" if c["kind"] == "stop" else ("reduce", c["new_amount"])
        applied.append(c)

        trial_explicit = apply_overrides_to_explicit(explicit_flows, overrides)
        trial_projected = build_projected_flows(series_list, request_date, window_end,
                                                 trial_explicit, overrides)
        trial_flows = trial_explicit + trial_projected

        amt_safe = amount_safe_today(balance_now, trial_flows, min_balance, requested_amount,
                                      request_date, window_end)
        earliest = earliest_safe_full_payment_date(balance_now, trial_flows, min_balance,
                                                     requested_amount, request_date, window_end)

        change_tags = []
        for a in applied:
            if a["kind"] == "stop":
                change_tags.append(f"stop:{a['event_id']}")
            else:
                change_tags.append(f"reduce_to:{a['event_id']}:{fmt_amount(a['new_amount'])}")

        if "full_payment" in accepted and amt_safe >= requested_amount - 1e-6 and earliest == request_date:
            new_candidates.append({
                "method": "full_payment", "plan": [(request_date, requested_amount)],
                "completes_by_deadline": True, "uses_changes": True,
                "total_paid": requested_amount, "first_date": request_date,
                "n_payments": 1, "tiebreak": "", "status": "affordable_with_plan",
                "changes": change_tags,
            })
            return new_candidates

        if (allows_partial and "partial_payment" in accepted and 0 < amt_safe < requested_amount - 1e-9
                and earliest is not None and earliest <= desired_completion):
            remainder = requested_amount - amt_safe
            new_candidates.append({
                "method": "partial_payment",
                "plan": [(request_date, amt_safe), (earliest, remainder)],
                "completes_by_deadline": True, "uses_changes": True,
                "total_paid": requested_amount, "first_date": request_date,
                "n_payments": 2, "tiebreak": "", "status": "affordable_with_plan",
                "changes": change_tags,
            })
            return new_candidates

        if "installments" in accepted and profile.max_installment_months:
            for opt in options:
                if opt.payment_method != "installments":
                    continue
                if opt.number_of_payments > profile.max_installment_months:
                    continue
                safe, pay_dates, plan_end = simulate_installment_safety(
                    balance_now, trial_flows, min_balance, opt, request_date, window_end)
                if safe and plan_end <= desired_completion:
                    total_paid = opt.total_payable_amount if opt.total_payable_amount is not None \
                        else opt.payment_amount * opt.number_of_payments
                    new_candidates.append({
                        "method": "installments",
                        "plan": list(zip(pay_dates, [opt.payment_amount] * len(pay_dates))),
                        "completes_by_deadline": True, "uses_changes": True,
                        "total_paid": total_paid, "first_date": pay_dates[0],
                        "n_payments": opt.number_of_payments, "tiebreak": opt.payment_option_id,
                        "status": "affordable_with_plan", "changes": change_tags,
                    })
                    return new_candidates

    return new_candidates


def rank_candidates(candidates):
    if not candidates:
        return None
    def key(c):
        return (
            0 if c["completes_by_deadline"] else 1,
            0 if not c["uses_changes"] else 1,
            c["total_paid"],
            c["first_date"],
            c["n_payments"],
            c["tiebreak"] or "",
        )
    return sorted(candidates, key=key)[0]


# --------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------
def explain_choice(profile, request, chosen, requested_amount, amt_safe0, earliest_full):
    cur = profile.home_currency
    method = chosen["method"]
    changes = chosen.get("changes", [])
    change_txt = ""
    if changes:
        readable = []
        for c in changes:
            if c.startswith("stop:"):
                readable.append(f"stopping {c.split(':')[1]}")
            else:
                _, eid, amt = c.split(":")
                readable.append(f"reducing {eid} to {cur} {amt}")
        change_txt = " after " + " and ".join(readable)

    if method == "full_payment":
        req_date = request["request_date"]
        return (f"Pay {cur} {fmt_amount(requested_amount)} in full on {req_date}"
                f"{change_txt}. This keeps the {cur} {fmt_amount(profile.minimum_balance_to_keep)} "
                f"minimum protected over the next {FORECAST_DAYS} days.")
    if method == "partial_payment":
        remainder = requested_amount - amt_safe0
        return (f"Pay {cur} {fmt_amount(amt_safe0)} today and the remaining {cur} "
                f"{fmt_amount(remainder)} on {fmt_date(earliest_full)}{change_txt}. This completes "
                f"the full request while keeping the {cur} {fmt_amount(profile.minimum_balance_to_keep)} minimum protected.")
    if method == "installments":
        n = chosen["n_payments"]
        first = fmt_date(chosen["plan"][0][0])
        amt = fmt_amount(chosen["plan"][0][1])
        return (f"Use {n} installments of {cur} {amt}, starting {first}{change_txt}. This leaves at "
                f"least {cur} {fmt_amount(profile.minimum_balance_to_keep)} available.")
    if method == "wait":
        return (f"Wait until {fmt_date(earliest_full)}, then pay {cur} {fmt_amount(requested_amount)} "
                f"in full. Paying sooner would put the {cur} {fmt_amount(profile.minimum_balance_to_keep)} "
                f"minimum at risk.")
    return "No safe recommendation could be generated."


def explain_not_recommended(profile, requested_amount, amt_safe_final, request_date):
    cur = profile.home_currency
    return (f"Do not make this payment by the requested date. None of the available options keeps "
            f"the {cur} {fmt_amount(profile.minimum_balance_to_keep)} minimum protected, although "
            f"{cur} {fmt_amount(amt_safe_final)} is available today.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    profiles = load_profiles()
    home_currency_by_user = {uid: p.home_currency for uid, p in profiles.items()}

    rate_rows = read_csv("exchange_rates.csv")
    rates = RateTable(rate_rows)

    all_events = load_events(rates, home_currency_by_user)
    events_by_id = {e.event_id: e for e in all_events}
    messages_by_event = load_messages()
    apply_message_overrides(events_by_id, messages_by_event)

    events_by_user = defaultdict(list)
    for e in all_events:
        events_by_user[e.user_id].append(e)

    series_by_user = detect_recurring_series(events_by_user)
    options_by_request = load_payment_options()
    requests = read_csv("requests.csv")

    out_rows = []
    for req in requests:
        user_id = req["user_id"]
        profile = profiles.get(user_id)
        request_date = parse_date(req["request_date"])
        window_end = request_date + timedelta(days=FORECAST_DAYS)

        if profile is None:
            out_rows.append({
                "request_id": req["request_id"], "amount_safe_to_pay": "0",
                "affordability_status": "not_affordable",
                "recommended_payment_method": "not_recommended",
                "payment_plan": "none", "earliest_date_for_full_payment": "",
                "spending_changes_needed": "none",
                "decision_explanation": "No financial profile found for this user.",
            })
            continue

        result = decide(
            req, profile, events_by_user.get(user_id, []),
            series_by_user.get(user_id, []), options_by_request.get(req["request_id"], []),
            window_end,
        )

        spending = result["spending_changes"]
        spending_str = "|".join(spending) if spending else "none"
        earliest = result["earliest_date_for_full_payment"]
        earliest_str = fmt_date(earliest) if earliest else ""

        out_rows.append({
            "request_id": req["request_id"],
            "amount_safe_to_pay": fmt_amount(result["amount_safe_to_pay"]),
            "affordability_status": result["affordability_status"],
            "recommended_payment_method": result["recommended_payment_method"],
            "payment_plan": result["payment_plan"],
            "earliest_date_for_full_payment": earliest_str,
            "spending_changes_needed": spending_str,
            "decision_explanation": result["explanation"],
        })

    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in out_rows:
            writer.writerow(row)

    print(f"Wrote {len(out_rows)} rows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
