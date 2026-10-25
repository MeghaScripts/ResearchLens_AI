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


## Measuring each level separately (`python -m eval.run_levels`)
Run from `backend/`, reusing the same cache (no re-fetching):

    python -m eval.run_levels --strict                      # level 1 + level 2, 5-fold pooled out-of-fold
    python -m eval.run_levels --make-quality-template 100   # blank sheet for human quality ratings
    python -m eval.run_levels --strict --ratings eval/quality_ratings.csv   # + level 3

| Level | Ground truth | What is reported |
|---|---|---|
| 1 Authenticity | the labelled fakes (off-topic papers excluded, relevance cutoff off) | coverage, decisive accuracy with Wilson 95% CI, confusion matrix, per fake type; thresholds tuned inside each training fold only |
| 2 Relevance | every real paper paired with every topic (paper vs its own topic = 1, vs another topic = 0; about 900 pairs for 150 papers) | AUC, balanced accuracy, precision, recall for the shipped weights/cutoff and for weights fitted by non-negative logistic regression (held-out folds); prints a recommended `DEEP_RELEVANCE_MIN` and `DEEP_RELEVANCE_WEIGHTS` |
| 3 Quality | **human ratings you collect** (3 raters, 1 to 5) | inter-rater kappa, Spearman correlation and high-vs-low AUC for the shipped score and for ridge-fitted weights on held-out folds; prints a recommended `DEEP_QUALITY_WEIGHTS` |

`python -m eval.run_eval --cv 5` gives the overall accept/reject decision the same pooled treatment (every record gets one
held-out prediction, so n is the whole set instead of a 30% split).

Quality is reported as agreement with raters, never as an "accuracy": there is no objective ground truth for it, and if the
raters disagree among themselves (low kappa) the target itself is noisy. Collect ratings with the guide in `eval/quality_eval.py`.

### Relevance model options
`DEEP_SEMANTIC_MODEL=allenai-specter` swaps the sentence embedding for SPECTER (scientific-paper embeddings);
`DEEP_CROSS_ENCODER=cross-encoder/ms-marco-MiniLM-L-6-v2` adds an optional cross-encoder rerank as an extra relevance component
(default weight 0.20 once it loads). Both need `sentence-transformers` and a model download, so they are off in the Docker/Render
builds. They are options to compare with `run_levels`, not claimed improvements: keep one only if its held-out numbers beat the shipped ones.
Both are exercised in tests with stubs only; the real models were not run in the development sandbox.

### Larger or cleaner runs
- `S2_API_KEY` (free from semanticscholar.org) is now sent with Semantic Scholar batch calls, and HTTP 429 is retried with
  back-off (`S2_RETRIES`, `S2_MAX_WAIT_S`). A rate-limited batch is no longer written to the cache. To redo old cached
  Semantic Scholar answers: `python -m eval.run_eval --drop-cache semantic_scholar_lookup_batch`.
- For a bigger set: `python -m eval.build_eval_set --n-real 500 --n-bad 500` (slow: one Crossref call per paper, cached).
