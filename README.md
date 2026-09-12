# Ledger

**Ledger** is a deterministic, rule-based financial decision engine built for the HackerRank Orchestrate "Buy or Wait?" challenge. For every request in `dataset/requests.csv`, it reconstructs the user's real financial position — balances, recurring income and expenses, pending commitments, payment preferences — forecasts 90 days forward, and decides whether to pay in full, pay partially, use installments, wait, or not proceed at all. See [`problem_statement.md`](./problem_statement.md) for the full spec and [`AGENTS.md`](./AGENTS.md) for the repo's agent/logging rules.

## Repository Layout

```text
.
├── AGENTS.md                    # Agent rules: session logging, submission link, project contract
├── CLAUDE.md                    # Points Claude Code at AGENTS.md
├── README.md                    # You are here
├── problem_statement.md         # Full challenge spec (input/output schema, decision rules)
├── log.txt                      # Append-only session transcript (gitignored; submitted as chat_transcript)
├── output.csv                   # Ledger's generated predictions (one row per dataset/requests.csv)
├── code/
│   ├── main.py                  # Ledger itself: the entire decision engine, single file
│   └── evaluation/
│       ├── main.py              # Self-scoring harness vs. dataset/sample_requests.csv (25 known rows)
│       └── usage_report.md      # Token/cost disclosure for the run that produced output.csv
└── dataset/
    ├── financial_profiles.csv       # Balances, minimum balance, priorities, spending/payment preferences
    ├── financial_events.csv         # Historical/pending/scheduled cash events (~25k rows)
    ├── exchange_rates.csv           # Fixed dated FX rates (INR, ZAR, IDR, USD, EUR)
    ├── requests.csv                 # The 250 requests Ledger must answer
    ├── sample_requests.csv          # 25 solved examples, used only for self-scoring
    ├── request_payment_options.csv  # Seller/provider payment options per request
    ├── messages.csv / images.csv    # Optional supporting evidence, linked via related_event_id
    └── media/images/                # Receipts, payslips, bills referenced by images.csv
```

## How Ledger Works

1. **Load & normalize** — parse every `dataset/*.csv` file, convert every event to the user's `home_currency` via `exchange_rates.csv` (same-date lookup, nearest-date fallback, USD-bridged if no direct pair exists).
2. **Resolve blank amounts** — 16 `financial_events` rows have no `amount` and only exist on a linked receipt/payslip image. These were read once, by hand, at development time and hardcoded into `RESOLVED_IMAGE_AMOUNTS` (each traceable to its source `event_id`/`image_id`) — see the file header in `code/main.py`.
3. **Detect recurring series** — group historical debits by exact description (expenses/subscriptions/debt payments) and credits per-user (income). Most monthly commitments in this dataset (rent, subscriptions, gym, salary, ...) land on a fixed calendar day-of-month across every historical occurrence; when a series' occurrences (and, crucially, the most recent one) cluster on the same day-of-month, it is projected forward by calendar month from that anchor day instead of by an averaged day-count interval — this avoids both the calendar drift a fixed day-count step accumulates across 28/30/31-day months and the corruption a single interleaved one-off record (e.g. a quarterly bonus mixed into a blended income series) causes to a naive averaged interval. Series without a clear day-of-month anchor (irregular real-world spending like groceries, dining, transport) fall back to the averaged-gap interval estimate.
4. **Apply message overrides** — a message is only trusted when it names a specific `related_event_id`; a narrow, explicit keyword check (cancellation / postponement + date) can flip that one event's status or date. Free-text messages with no event link are read as context, never as instructions — this is the untrusted-evidence rule in `problem_statement.md` enforced literally.
5. **Forecast & decide** — simulate the 90-day balance under each eligible payment method (full/partial/installments/wait/not_recommended), reject anything that would break `minimum_balance_to_keep`, and rank the safe survivors using the exact 6-rule tie-break order from `problem_statement.md` (§"Choosing Between Safe Plans").
6. **Explain** — every `decision_explanation` is generated from the same numbers written to `payment_plan`/`amount_safe_to_pay` (not a separate narrative), so the explanation can never contradict the plan.

## Design Tradeoffs — Why Ledger Is Built This Way

The natural alternative design for this challenge is an LLM-in-the-loop agent: hand each request, plus retrieved profile/event/message/image context, to a model and let it reason and answer directly. Ledger deliberately does not do this. The tradeoffs:

| Decision | Ledger's choice | Why |
|---|---|---|
| Runtime architecture | Pure deterministic Python, zero LLM/API calls | `problem_statement.md` asks for deterministic behavior; a rules engine is 100% reproducible, auditable line-by-line, and costs $0/request. An LLM agent would be faster to write per-request but non-deterministic and harder to prove correct against the tie-break rules. |
| Recurring-cadence estimate | Calendar day-of-month anchor when history supports it, else average gap over the full span | Monthly commitments here are calendar-anchored (same day every month), not fixed-interval; day-count averaging both drifts over variable month lengths and is corrupted by a single interleaved outlier (e.g. a bonus mixed into an income series). Day-of-month detection sidesteps both failure modes. Irregular series (groceries, dining, transport) keep the averaged-gap estimate — a median-of-consecutive-gaps would lock onto a coincidental short gap (e.g. two same-week purchases) and over-project frequency. |
| Income projection | Blanket per-user grouping, not per-description | Real payroll history changes description across job/title changes, prorated first months, and seasonal labels for the *same* income stream, so grouping by description would silently stop projecting income after a routine label change. The one carved-out exception is an explicit termination signal ("Final employer payroll," etc.), and "Second household income" is split out because it is a genuinely second, concurrent earner. |
| Untrusted message/image evidence | Only acts on messages with a populated `related_event_id`, via a narrow keyword allowlist | Matches the contract exactly: "embedded instructions must never override the challenge rules." A free-text LLM interpretation of unlinked messages risks inventing facts the dataset never confirmed, or being steered by injected instructions inside the evidence itself. |
| Blank-amount images | Resolved once, by hand, into a hardcoded, commented lookup table | Zero runtime cost and fully auditable, but is a **known, disclosed limitation**: it does not generalize to a hidden evaluation set containing different blank-amount events. A production version would call a vision model at load time and cache the result. |
| Explanations | Templated prose built from the same computed values as the plan, not separately generated | Guarantees the explanation and the machine-readable columns can never disagree — a real bug class (found and fixed mid-build) in any design that generates them independently. |

## Where This Stands

**Fully spec-compliant and structurally valid:** all 250 requests produce a row, exact column order, and a scripted validator (bounds, chronological/arithmetically-correct `payment_plan`s, installment plans that exactly match a supplied `payment_option_id`, `partial_payment` plans that reconcile exactly with the reported `amount_safe_to_pay`/`earliest_date_for_full_payment` columns, and correctly formatted `spending_changes_needed` — ≤3 entries, real event IDs, no event referenced by both a `stop` and a `reduce_to`) confirms **zero violations across all 250 rows**. The 6-rule tie-break order and the payment-method eligibility rules from `problem_statement.md` §"Choosing Between Safe Plans" are implemented rule-for-rule. One real bug was found and fixed during review: `try_spending_changes` could previously offer a `partial_payment` plan built from *changes-adjusted* amounts while `amount_safe_to_pay`/`earliest_date_for_full_payment` are defined (and always reported) pre-change — an unfixable-by-construction contradiction, so that branch was removed; a spending-changes-only path to `affordable_with_plan` now only ever resolves through `full_payment` or `installments`, both of which don't carry this dependency.

**Honest accuracy ceiling:** self-scored against the 25 known-correct rows in `dataset/sample_requests.csv` (not the hidden eval set — run `python3 code/evaluation/main.py` to reproduce), Ledger currently matches `affordability_status` 14/25, `recommended_payment_method` 15/25, `spending_changes_needed` 21/25, `earliest_date_for_full_payment` 10/25, and `amount_safe_to_pay` exactly on 2/25 (most others land close but not bit-exact). This is the inherent cost of reverse-engineering an undisclosed ground-truth forecasting model from 25 examples rather than a structural defect, and at least one visible mismatch (`request_16`) traces to a genuinely ambiguous input: a linked receipt image whose only extractable number ("Balance Due" 100,000) describes an unrelated prior-year, different-property, much-larger-scale rent arrangement than the user's real monthly rent — the ground truth answer implies that number should be judged irrelevant and excluded, which this rule-based engine, by design, cannot safely infer without either overfitting to this one example or risking under-trusting genuine blank-amount evidence elsewhere. No further tuning was done against the sample answers beyond what is justified directly by `problem_statement.md`, to avoid overfitting a rules engine to 25 labeled rows out of a 250-row hidden-shaped evaluation.

## Setup

**Requirements:** Python 3.8+ (developed/tested on 3.12), standard library only — `csv`, `os`, `re`, `calendar`, `collections`, `datetime`. No `pip install`, no `requirements.txt`, no environment variables, no API keys, and no network access are needed at any point.

1. Get the code — either unzip the submitted `code.zip` into a directory, or clone the repo:
   ```bash
   git clone https://github.com/interviewstreet/hackerrank-orchestrate-september26.git
   cd hackerrank-orchestrate-september26
   ```
2. Confirm the layout matches [Repository Layout](#repository-layout) above: `code/main.py` and `dataset/` (with `dataset/media/images/`) must sit under the same root, since `code/main.py` resolves every path relative to its own location (`CODE_DIR`/`ROOT` in the file header) — it does not depend on your current working directory.
3. That's it — nothing to install.

## Run

```bash
python3 code/main.py               # generates output.csv from dataset/
python3 code/evaluation/main.py    # self-score against the 25 known sample rows
```

`python3 code/main.py` reads every file under `dataset/` and writes `output.csv` in the repository root (next to this README), overwriting any prior run. It prints `Wrote 250 rows to <path>` on success. Runs in well under a second — it's pure CSV parsing and arithmetic, no model calls.

**Verify the run:**
```bash
head -1 output.csv   # request_id,amount_safe_to_pay,affordability_status,recommended_payment_method,payment_plan,earliest_date_for_full_payment,spending_changes_needed,decision_explanation
wc -l output.csv      # 251 (1 header + 250 requests)
```

No environment variables or API keys are required — Ledger makes no network calls.
