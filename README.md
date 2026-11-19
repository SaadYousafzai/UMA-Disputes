# UMA Dispute Forecasting

Can a language model predict how a **disputed** prediction-market question will finally resolve, and does it know when it is likely to be wrong?

This repo holds the data and tooling for a benchmark built from Polymarket markets whose resolution was disputed on UMA's Optimistic Oracle (Polygon). Each question comes with its rules, the final outcome, and web evidence collected only from before the first dispute. Local LLMs are then run with and without that evidence, and their answers and stated confidence are compared with the final outcome.

**Status: work in progress.** Data collection and evidence collection are done. Human labeling and the final model runs are not.

## How the benchmark is built

1. **Disputes.** Collect every Polymarket-related UMA request that was disputed and later settled: `data/raw/uma_disputed_settled_raw.jsonl` (5,571 rows, 4,919 distinct question IDs, requests from July 2022 to Nov 2026). Each row carries the request, proposal, dispute and settlement times and tx hashes, the rules text, and the oracle type (3,721 `managed`, 1,850 `permissionless`). `scripts/uma_disputes.py` is the on-chain way to rebuild the dispute list.
2. **Market metadata.** Match each UMA question to its Polymarket market through the Gamma API: `data/raw/gamma_lookup.jsonl` (660 lookups, 380 with exactly one match, 280 with none).
3. **Benchmark sample.** 300 disputed questions, with a hidden answer key (`final_outcome`) kept apart from the text shown to annotators and models.

   | Category | n | | Category | n |
   |---|---|---|---|---|
   | Geopolitics | 57 | | Esports | 37 |
   | Sports | 56 | | Culture | 21 |
   | Crypto | 56 | | Other | 19 |
   | Politics | 54 | | | |

   Outcomes are mostly Yes (97) and No (101), then Over/Under (22 each), Up/Down (9/7), 50-50 ties (6), and named players or teams. First disputes run from 2023-03 to 2026-11.
4. **Evidence.** For each question, search the web for pages published before the first-dispute date and keep a few on-topic ones (`data/evidence/evidence.jsonl`). 249 of 300 questions kept at least one page (584 pages in total). The collector applies these filters:
   - date window: only pages published before the cutoff
   - no prediction-market domains
   - a title-word relevance score
   - no duplicates
   - no pages containing text addressed to AI agents

   Rejected and dropped pages are logged in the `dropped` and `rejected` fields.
5. **Models.** Qwen3 14B and Gemma3 12B via Ollama on one 16 GB GPU. Two conditions, `none` (question and rules only) and `articles` (plus the kept evidence). The model returns JSON with an answer and a 0-100 confidence that the answer is correct.

## Files

| Path | What it is |
|---|---|
| `scripts/uma_disputes.py` | Queries UMA oracle contracts on Polygon for `DisputePrice` events (below) |
| `data/raw/uma_disputed_settled_raw.jsonl` | Disputed-and-settled UMA requests with Polymarket metadata |
| `data/raw/gamma_lookup.jsonl` | Gamma API match for each UMA question ID |
| `data/raw/disputed.jsonl` | 86 Gamma-format market records (resolved markets; only 1 is among the 300 benchmark questions) |
| `data/benchmark/benchmark_300_key.csv` | The 300 questions with final outcome, number of requests and dispute rounds, dispute and close times, volume |
| `data/benchmark/benchmark_300_annotate.csv` | Same questions with rules and outcomes, and blank annotator columns for human labels |
| `data/evidence/evidence.jsonl` | Collected evidence per question (query, window, cutoff, kept pages, dropped pages) |
| `results/results_qwen3_14b_articles.jsonl` | Qwen3 14B, `articles` condition, 249 questions (see caveats) |

## Querying UMA for disputes

`scripts/uma_disputes.py` reads `DisputePrice` logs from the UMA Optimistic Oracle contracts on Polygon and writes one JSON line per dispute: oracle, block, tx hash, requester, proposer, disputer, identifier, request timestamp, proposed price, decoded ancillary data, block time, and `question_id`. For Polymarket markets `question_id` is `keccak256(ancillaryData)`. That matches the dataset's `question_id` for 291 of the first 300 rows I checked.

```bash
pip install -r requirements.txt
export POLYGON_RPC_URL=https://<your-polygon-rpc>   # Alchemy, Infura, QuickNode, etc.

# 1. confirm which contracts emitted the disputes in your dataset
python scripts/uma_disputes.py discover --raw data/raw/uma_disputed_settled_raw.jsonl

# 2. scan a date or block range (resumable; chunk size shrinks automatically if the RPC limits ranges)
python scripts/uma_disputes.py scan --from-date 2025-08-01 --to-date 2025-09-01 --out out/disputes_onchain.jsonl
python scripts/uma_disputes.py scan --resume --out out/disputes_onchain.jsonl

# 3. check coverage against the dataset
python scripts/uma_disputes.py compare --out out/disputes_onchain.jsonl
```

**Read before running a full scan:**
- `MOOV2` (`0x2C0367…58B1`, Managed Optimistic Oracle V2) is taken from UMA's docs. The `OOv2` address in the script comes from memory and I could not check it from the sandbox, so run `discover` first. It prints the contract that emitted each sampled dispute, and tells you to pass `--oracle NAME=0xADDR` for any address that is not in the defaults. Requests from 2022 may also have gone through an older oracle contract.
- I tested the decoding, chunk shrinking, resume and `compare` against a mock RPC server, not against Polygon, because the sandbox could not reach any RPC. Treat the first real run as a test, and use `compare` to check coverage.
- A multi-year scan needs many `eth_getLogs` calls. Public RPCs often cap the block range, so a provider with larger ranges is much faster. `--chunk` sets the maximum range.
- The script reads disputes only. It does not fetch proposals, settlements or the Polymarket market metadata that the raw dataset has.

## Caveats

- **Evidence can contain the answer.** The cutoff is the first-dispute date, which comes after the event for most questions. Pre-cutoff pages therefore sometimes report the outcome (for example match results or live coverage). Results should be reported both with and without questions flagged as leaky.
- **Evidence is noisy.** Relevance is title-word overlap, so generic words can pass off-topic pages. Questions with generic titles ("Games Total: O/U 2.5") cannot be searched meaningfully. Evidence quality differs a lot by question type, so report results by category.
- **`results_qwen3_14b_articles.jsonl` is from the first prompt version, which allowed `CANNOT_DETERMINE`.** 162 of its 249 rows are abstentions, so it is not comparable with a forced-guess run. It is kept for reference and is superseded by the planned reruns.
- **Labels are not done.** `annotator_A_label` and `annotator_B_label` are empty.
- **Evidence text is third-party content** (up to 6,000 characters per page), included for reproducibility. Check licensing before redistributing it.
- **`benchmark_300_key.csv` contains the answers.** If the benchmark is meant to be used for testing other models, consider keeping the key private.

## Planned

- Add `collect_evidence.py` and `run_models.py` to this repo.
- Rerun both models under one forced-guess prompt for `none` and `articles`, on frozen evidence.
- Human labels, a hand audit of about 30 evidence sets, and calibration plots (confidence versus accuracy, accuracy on the most confident answers).
