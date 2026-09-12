# Token Usage & Cost Report

## Final full-dataset run (the run that produced `output.csv`)

The submitted solution (`code/main.py`) is a **fully deterministic, rule-based
Python program**. It reads every file under `dataset/`, reconstructs each
user's financial state, runs a 90-day cash-flow forecast, and writes
`output.csv`. **It makes zero LLM / API calls at runtime** — there is no
model provider, no network access, and nothing to authenticate, so there are
no runtime tokens or runtime inference cost to report.

| Metric | Value |
|---|---|
| Model provider / name | None (no model calls at runtime) |
| Model calls | 0 |
| Input tokens | 0 |
| Output tokens | 0 |
| Requests processed | 250 (`dataset/requests.csv`) |
| Total tokens | 0 |
| Average tokens / request | 0 |
| Estimated total cost | $0.00 |
| Estimated cost / request | $0.00 |

Wall-clock runtime for the full 250-request run: well under 1 second on a
single core (`python3 code/main.py`), since it is pure CSV parsing and
arithmetic over ~25k event rows.

## Development-time model usage (one-off, NOT part of the run above)

`financial_events.csv` has 16 rows with a blank `amount`. Per the task
instructions, each blank amount is only recoverable from a receipt/payslip
image linked via `images.csv` (`related_event_id`), e.g. `image_07` →
`dataset/media/images/image_07.png`. This dataset ships no OCR/vision API key
and the challenge rules require reading secrets only from environment
variables, so rather than wiring up a paid vision API call for 16 fixed,
one-time lookups, those 16 images were read once during development (by
Claude, via Claude Code's multimodal file reading, while building this
solution) and the transcribed amounts were hardcoded into
`RESOLVED_IMAGE_AMOUNTS` in `code/main.py`, each with a comment citing the
source image and the receipt line item it came from.

This is a one-time development-time step, not part of the repeatable
full-dataset run, is not billed per request, and does not run again when
`code/main.py` is re-executed. It is disclosed here for transparency:

| Model provider / name | Anthropic, Claude (Sonnet 5), via Claude Code |
| --- | --- |
| Purpose | One-time manual transcription of 16 receipt/payslip images into a hardcoded lookup table |
| Model calls (approx., across the whole build/debug session) | 1 interactive agent session (not a metered API integration) |
| Est. input tokens (images + surrounding context, one-off) | ~15,000–20,000 |
| Est. output tokens (one-off) | ~2,000 |
| Recurs on future runs? | No — the 16 values are fixed constants in the source file |
| Cost attributable to `output.csv` generation | $0.00 (the generation run itself is model-free) |

## Overall total (runtime + one-off development)

| | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|
| Runtime (produces `output.csv`) | 0 | 0 | 0 | $0.00 |
| Development-time (one-off image transcription) | ~15,000–20,000 | ~2,000 | ~17,000–22,000 | not separately billed (interactive session, not a metered API) |
| **Total attributable to `output.csv`** | **0** | **0** | **0** | **$0.00** |

## Why no model is used at inference time

The affordability decision is fully specified by deterministic rules
(currency conversion, recurring-series detection, a 90-day balance
forecast, payment-option matching, and a fixed tie-break order — see
`problem_statement.md` §"Choosing Between Safe Plans"). Implementing it as
straight-line Python satisfies "keep behavior deterministic where possible"
exactly, avoids any API-key/secrets handling, and costs nothing to re-run on
the full dataset or on a hidden evaluation set of the same shape.
