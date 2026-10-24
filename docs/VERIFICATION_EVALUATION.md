# Verification: claim checking and evaluation

## What each layer proves
| Layer | Checks | Does NOT prove |
|---|---|---|
| Cite by ID (`claim_check.resolve_evidence_ids`) | the cited paper exists in the collected corpus | that it supports the claim |
| Claim support (`claim_check.check_claims`) | an LLM judge compared the claim with the cited abstracts (supported / partial / unsupported / contradicted) | ground truth: the judge can be wrong |
| Already addressed (`annotate_semantic_overlap`) | >= 2 recent corpus papers (not the gap's own evidence) are very similar to the gap text | that the gap is truly solved |
| Run agreement (`apply_consensus`, `GAP_CONSENSUS_RUNS>1`) | the gap recurs across independent generations | correctness |

Settings (env): `CLAIM_CHECK=0` disables the judge; `CLAIM_CHECK_MAX` (50), `CLAIM_CHECK_GROUP` (4 gaps per call),
`CLAIM_DROP_CONTRADICTED=1`, `ADDRESSED_THRESHOLD_EMBED` (0.72) / `ADDRESSED_THRESHOLD_TFIDF` (0.45),
`GAP_CONSENSUS_RUNS`, `GAP_CONSENSUS_MIN_AGREE`. The default thresholds are conservative guesses, not tuned values.

Groq cost: about 13 extra small calls per 50 gaps (2.5 s apart); `GAP_CONSENSUS_RUNS=2` doubles the gap-generation calls.

## Measuring paper verification
Run from `backend/`, on a machine with internet access:

    python -m eval.build_eval_set --n-real 150 --n-bad 150     # writes eval/labelled.jsonl
    python -m eval.run_eval                                    # baseline on the 30% test split
    python -m eval.run_eval --tune --target 0.95               # thresholds chosen on the 70% train split only

Report two numbers together: **coverage** (share decided accept/reject; the rest go to review) and
**decisive accuracy** with its 95% interval. Strict accuracy (abstentions counted as misses) is printed too.
Authenticity can plausibly reach the high 90s on decided papers; quality is partly subjective, so no 95% claim is made for it.
Offline CI: `tests/test_eval_smoke.py`.

Known limits: the quality weights are not fitted by regression (no quality labels exist); semantic similarity needs
`sentence-transformers` (skipped in the Docker/Render builds to save memory), otherwise TF-IDF is used and labelled `tfidf_fallback`.
