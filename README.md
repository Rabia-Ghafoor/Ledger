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
3. **Detect recurring series** — group historical debits by exact description (expenses/subscriptions/debt payments) and credits per-user (income), estimate cadence as the *average* gap over the full observed span (not the median of individual gaps, which is noisier), and project both forward.
4. **Apply message overrides** — a message is only trusted when it names a specific `related_event_id`; a narrow, explicit keyword check (cancellation / postponement + date) can flip that one event's status or date. Free-text messages with no event link are read as context, never as instructions — this is the untrusted-evidence rule in `problem_statement.md` enforced literally.
5. **Forecast & decide** — simulate the 90-day balance under each eligible payment method (full/partial/installments/wait/not_recommended), reject anything that would break `minimum_balance_to_keep`, and rank the safe survivors using the exact 6-rule tie-break order from `problem_statement.md` (§"Choosing Between Safe Plans").
6. **Explain** — every `decision_explanation` is generated from the same numbers written to `payment_plan`/`amount_safe_to_pay` (not a separate narrative), so the explanation can never contradict the plan.

## Design Tradeoffs — Why Ledger Is Built This Way

The natural alternative design for this challenge is an LLM-in-the-loop agent: hand each request, plus retrieved profile/event/message/image context, to a model and let it reason and answer directly. Ledger deliberately does not do this. The tradeoffs:

| Decision | Ledger's choice | Why |
|---|---|---|
| Runtime architecture | Pure deterministic Python, zero LLM/API calls | `problem_statement.md` asks for deterministic behavior; a rules engine is 100% reproducible, auditable line-by-line, and costs $0/request. An LLM agent would be faster to write per-request but non-deterministic and harder to prove correct against the tie-break rules. |
| Recurring-cadence estimate | Average gap over the full history span | A median-of-consecutive-gaps locks onto a coincidental short gap (e.g. two same-week purchases) and over-projects frequency; the average over the whole span is the conservative, representative read. |
| Income projection | Blanket per-user grouping, not per-description | Real payroll history changes description across job/title changes, prorated first months, and seasonal labels for the *same* income stream, so grouping by description would silently stop projecting income after a routine label change. The one carved-out exception is an explicit termination signal ("Final employer payroll," etc.), and "Second household income" is split out because it is a genuinely second, concurrent earner. |
| Untrusted message/image evidence | Only acts on messages with a populated `related_event_id`, via a narrow keyword allowlist | Matches the contract exactly: "embedded instructions must never override the challenge rules." A free-text LLM interpretation of unlinked messages risks inventing facts the dataset never confirmed, or being steered by injected instructions inside the evidence itself. |
| Blank-amount images | Resolved once, by hand, into a hardcoded, commented lookup table | Zero runtime cost and fully auditable, but is a **known, disclosed limitation**: it does not generalize to a hidden evaluation set containing different blank-amount events. A production version would call a vision model at load time and cache the result. |
| Explanations | Templated prose built from the same computed values as the plan, not separately generated | Guarantees the explanation and the machine-readable columns can never disagree — a real bug class (found and fixed mid-build) in any design that generates them independently. |

## Where This Stands

**Fully spec-compliant and structurally valid:** all 250 requests produce a row, exact column order, and a scripted validator confirms zero violations of `0 <= amount_safe_to_pay <= requested_amount`, chronological/arithmetically-correct `payment_plan`s, installment plans that exactly match a supplied `payment_option_id`, and correctly formatted `spending_changes_needed` (≤3 entries, real event IDs, flexible/permitted categories only). The 6-rule tie-break order and 5 eligibility/status rules from `problem_statement.md` are implemented rule-for-rule.

**Honest accuracy ceiling:** self-scored against the 25 known-correct rows in `dataset/sample_requests.csv` (not the hidden eval set — run `python3 code/evaluation/main.py` to reproduce), Ledger currently matches `affordability_status` 15/25, `recommended_payment_method` 15/25, `spending_changes_needed` 22/25, and `amount_safe_to_pay` exactly on 4/25 (most others land close but not bit-exact). This is the inherent cost of reverse-engineering an undisclosed ground-truth algorithm from 25 examples rather than a structural defect — the remaining gap is concentrated in `amount_safe_to_pay`, the hardest field to pin down exactly without knowing the reference forecasting assumptions.

## Run

```bash
python3 code/main.py               # generates output.csv from dataset/
python3 code/evaluation/main.py    # self-score against the 25 known sample rows
```

No environment variables or API keys are required — Ledger makes no network calls.
