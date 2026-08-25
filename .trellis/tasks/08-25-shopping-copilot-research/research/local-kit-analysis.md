# Research: local participant-kit analysis

- **Query**: 以 trellis-research 身份对当前本地文件夹做只读深度检查。确认目录结构、README、Agent 接口、baseline、数据/会话样例、评测器、配置、测试、依赖、现有结果与明显缺失；判断可运行性、实验复现成本和工程风险。
- **Scope**: internal
- **Date**: 2026-08-25

## Findings

### Files Found

| File Path | Description |
|---|---|
| `README.md` | Participant-kit overview, catalog acquisition instruction, run command, metrics, and claimed baseline score. |
| `docs/competition_specification.md` | Detailed protocol, visible catalog fields, scenario mix, metric definitions, and model policy. |
| `docs/agent_api_contract.json` | Machine-readable reset/turn request and turn response contract. |
| `starter/agent.py` | Editable, standard-library SQLite FTS5/BM25 baseline Agent. |
| `evaluator/local_evaluator.py` | Deterministic public-set session simulator, recommendation normalization, scoring, and CLI. |
| `data/public_set.jsonl` | 200 labeled public development sessions. |
| `docs/evaluation_config.json` | Machine-readable evaluator limits and composite-score weights. |
| `docs/baseline_results.json` | Claimed weak-BM25 reference metrics. |
| `tests/test_evaluator.py` | Three evaluator-focused unit tests. |
| `docs/submission_rules.md` | Required deliverables, network caveat, output and reproducibility rules. |
| `.gitignore` | Excludes the downloaded catalog, result output, organizer material, and release checklist. |
| `.trellis/tasks/08-25-shopping-copilot-research/prd.md` | Task context and confirmed competition constraints; research-only scope. |

### Repository and Directory State

- The product-facing participant kit is intentionally small: root documentation plus four product directories: `data/`, `docs/`, `starter/`, `evaluator/`, and `tests/`. The remaining `.trellis/`, `.claude/`, and `.cursor/` content is workflow/agent tooling rather than challenge runtime.
- The working directory is **not a Git repository**: `git status`, `git log`, and `git remote -v` each fail with `fatal: not a git repository`. Consequently, there is no local commit, remote, release URL, or provenance record available for retrieving the absent release asset.
- No Python package/install manifest is present (`requirements.txt`, `pyproject.toml`, `setup.py`, `setup.cfg`, Pipfile, lockfiles, Makefile, Dockerfile, and `.python-version` all absent). This is consistent with README’s statement that the starter uses only the standard library (`README.md:34-40`), but it means future non-stdlib work must add its own reproducible dependency declaration.
- No product code modifications were made during this inspection. The only file written is this research record under the active task directory.

### README and Official-Artifact Consistency

- README describes a frozen 50,000-product Amazon Clothing/Shoes/Jewelry catalog, 200 public sessions, and 800 private sessions (`README.md:4-12`). The detailed specification independently gives the same catalog and split sizes (`docs/competition_specification.md:14-20`).
- Catalog acquisition is documented only as “download `catalog.jsonl.gz` from the GitHub Release attached to this repository,” decompress, and move it into `data/catalog.jsonl` (`README.md:23-32`; `data/README.md:8-11`). There is no release URL, catalog archive, checksum manifest, or uncompressed catalog in this checkout.
- README tells participants to verify `SHA256SUMS` (`README.md:32`), but no `SHA256SUMS` exists locally. Thus integrity verification cannot be performed from this folder alone.
- README’s “Judging and Submission Policy” lists four documents (`README.md:99-105`), but all are absent locally: `docs/participant_release_checklist.md`, `organizer/JUDGING_RUNBOOK.md`, `organizer/private_release_checklist.md`, and `organizer/JUDGING_DAY_SOP.md`. The first and organizer directory are explicitly gitignored (`.gitignore:7-15`), so their absence is likely intentional for this participant checkout; nonetheless the README should distinguish public from organizer-only/non-shipped artifacts.
- Data attribution identifies the Amazon Reviews 2023 source, category, and `parent_asin` join key (`DATA_ATTRIBUTION.md:1-11`).

### Agent API Contract

- The required entry point is an `Agent` class with `reset(session_id, user_profile)` followed by `respond(session_id, user_message, turn, top_k)` (`README.md:48-67`; `docs/competition_specification.md:41-64`; `docs/submission_rules.md:15-31`).
- `turn` is constrained to 1–10 and `top_k` is fixed at 10 (`docs/agent_api_contract.json:23-32`). Each response must contain a string `message`, allowed/null `ask_attribute`, and a recommendation array (`docs/agent_api_contract.json:34-55`). Allowed attributes are `category`, `material`, `color`, `size`, `style`, `brand`, `budget`, `feature`, `use_case`, `other`, or null (`docs/agent_api_contract.json:39-42`).
- Recommendations can contain at most 100 entries contractually, but evaluator scoring deduplicates and accepts only the first 10 **catalog-valid** IDs (`evaluator/local_evaluator.py:95-108`; `docs/competition_specification.md:59-64`). Optional numeric scores are accepted but ignored (`docs/competition_specification.md:61-63`).
- `usage` is optional in evaluator behavior, and only non-negative integer token values are accumulated (`evaluator/local_evaluator.py:244-249`), although when supplied its schema requires `prompt_tokens` and `completion_tokens` (`docs/agent_api_contract.json:56-64`). This is a workable compatibility detail for deterministic/offline agents.
- The evaluator catches any `respond` exception and converts it to an empty miss response (`evaluator/local_evaluator.py:237-243`). Invalid non-dict responses or missing/non-string `message` are similarly treated as empty responses. This means robustness failures silently degrade metric outcomes rather than aborting the run.

### Baseline Implementation

- The starter is explicitly a “stateless BM25 retrieval with no LLM dependency” (`starter/agent.py:34-36`). It builds an in-memory SQLite FTS5 virtual table (`starter/agent.py:38-49`) over `parent_asin`, title, categories, features, details, store, and description, ingesting catalog rows in 1,000-row batches (`starter/agent.py:51-70`).
- At every turn, it tokenizes only the **current** `user_message`, removes a small fixed stopword list, constructs an OR query, and retrieves `top_k` results using weighted SQLite `bm25` (`starter/agent.py:76-95`). It does not preserve dialogue messages, constraints, rankings, or user-profile content beyond asserting `reset` was called (`starter/agent.py:72-85`).
- Baseline emits a fixed customer-facing message, `ask_attribute: None`, and zero usage (`starter/agent.py:96-101`). Hence for non-buying sessions, the simulator responds with its “ask me about one specific attribute” fallback instead of revealing additional target constraints (`evaluator/local_evaluator.py:166-184`).
- Published reference results are Hit Rate@10 0.125, MRR 0.068034, MTTC 9.81, Efficiency 0.119, TechnicalScore 0.10671 (`docs/baseline_results.json:1-9`; summarized in `README.md:45-46`). These values cannot currently be independently reproduced because `data/catalog.jsonl` is absent.

### Dataset and Session Samples

- `data/public_set.jsonl` contains exactly 200 records, all with the documented fields: `sample_id`, `scenario_type`, `user_profile`, `ground_truth`, `category_bucket`, and `difficulty_bucket`. It has 200 distinct sample IDs and 200 distinct target `parent_asin` values.
- Scenario distribution is exactly 80 Buying, 80 Browsing, 30 Intent Override, and 10 Boundary. This matches `data/README.md:2-6` and the documented 40%/40%/15%/5% mix (`docs/competition_specification.md:22-27`).
- All records are bucketed `clothing`; difficulty counts are easy 80, medium 90, hard 30. `ground_truth.parent_asin` is present in every public record, so public-score tuning can overfit the released 200 target products unless experiments preserve a held-out validation split.
- All 200 profiles exactly conform to the five fields defined by the contract (`purchase_frequency`, `average_prior_rating`, `rating_style`, `preference_tags`, `summary`). However, profile diversity is limited: every record says `3-4 prior purchases`; rating styles are 134 usually-positive / 45 critical / 21 mixed; preference-tag vocabulary has nine terms. This evidence supports treating the profile as a low-cardinality auxiliary signal rather than assuming it is a high-information personalized history.
- The public session file deliberately lacks raw reviews, timestamps, purchase history, hidden intent cards, and simulator internals (`data/README.md:4-6`). For public evaluation, the evaluator deterministically rebuilds the hidden card from the **labeled target product metadata** whenever the file has no `intent_card`/`behavior` (`evaluator/local_evaluator.py:204-212`). The Agent never receives those rebuilt fields.

### Evaluator and Scoring Behavior

- The evaluator creates random UUID-based session IDs, calls `reset`, generates a first scenario-conditioned message, then runs at most 10 turns (`evaluator/local_evaluator.py:226-255`). It terminates immediately on a valid target hit.
- Initial Buying messages disclose the first hard constraint; Browsing starts vague; Intent Override begins with an old preference (`evaluator/local_evaluator.py:153-162`). For Intent Override, the evaluator blocks hits until the configured new intent is delivered around turn 3 (`evaluator/local_evaluator.py:251-264`). This makes pre-override recommendations unscorable even if they include the target.
- The simulator reads only the structured `ask_attribute` to choose what it will reveal. With no question it returns a generic correction; with an attribute, it provides up to two undisclosed constraints classified into the requested type (`evaluator/local_evaluator.py:166-184`). Natural-language question wording does not drive simulator correctness.
- Hidden intent construction uses product title and a maximum of two `hard_constraints` plus two `soft_preferences`, derived from features/details/material/color/price (`evaluator/local_evaluator.py:52-70`). `classify_constraint` supports limited material/color/size/style/use-case/budget heuristics; all other constraints become `feature` (`evaluator/local_evaluator.py:137-151`). An agent that asks those supported structured attributes can obtain deterministic signal, but there is a generalization risk if private catalog metadata is sparse or has different phrasing.
- Score implementation matches documentation: hit rate, MRR, miss-as-turn-11 MTTC, efficiency `clip((11 - MTTC)/10, 0, 1)`, then 0.50/0.30/0.20 composite weights (`evaluator/local_evaluator.py:187-200`, `277-293`; `docs/evaluation_config.json:1-14`). Earlier hits improve both Hit Rate and Efficiency; high rank improves MRR only.
- CLI defaults are `data/catalog.jsonl`, `data/public_set.jsonl`, and output `results.json` (`evaluator/local_evaluator.py:297-307`). Its invocation is straightforward but it writes an output file, so evaluation was run with a `/tmp/techjam-results.json` output override during this read-only inspection.

### Tests, Configuration, and Existing Results

- `python3 -m unittest -v` passes all three current tests on Python 3.11. Tests cover recommendation normalization order, miss MTTC assignment, and derivation of hidden fields from a temporary two-product catalog (`tests/test_evaluator.py:21-84`).
- There is no integration test that loads the 50,000-row released catalog, starts `starter.Agent`, runs all 200 samples, asserts published baseline metrics, checks catalog row count/identity checksum, or validates the full response schema. Consequently, the presently passing tests establish evaluator primitives but not end-to-end release reproducibility.
- `docs/evaluation_config.json` is the only product evaluation configuration; it provides constants/metric weights but no time, memory, CPU, or network limits (`docs/evaluation_config.json:1-14`). Submission rules warn that official runs may impose CPU, memory, timeout, and network restrictions (`docs/submission_rules.md:53-63`, `97-102`), but concrete resource limits are not included locally.
- `docs/baseline_results.json` is the only pre-existing result artifact. There is no local `results.json` (also gitignored at `.gitignore:4`), prior run log, or artifact proving the baseline scores were generated from the currently available files.

## Runability, Reproduction Cost, and Engineering Risk

### Direct Runability: Blocked

The kit is **not directly runnable end to end in its current checkout**. The documented command `python3 -m evaluator.local_evaluator` fails before Agent construction with:

```text
FileNotFoundError: [Errno 2] No such file or directory: 'data/catalog.jsonl'
```

The failure originates when `catalog_index` opens the default catalog (`evaluator/local_evaluator.py:111-122`, called by `main` at `evaluator/local_evaluator.py:303-305`). The missing file is a required, explicitly gitignored release asset (`.gitignore:5`). Unit tests are runnable because their catalog is temporary and synthetic.

### Reproduction Cost

- **Minimum cost after obtaining the exact release asset:** low. Python 3.10+ is recommended (`README.md:34-40`); the baseline and evaluator use standard library modules only. Decompressing the catalog, placing it at `data/catalog.jsonl`, checking the checksum, and invoking the evaluator are the documented steps.
- **Cost from this checkout alone:** indeterminate/blocking. No catalog archive, SHA-256 manifest, release URL, or Git remote exists, so the missing release asset cannot be located or verified solely from local information.
- **Cost of reliable experiment iteration:** moderate. Every fresh `Agent` initialization reindexes the complete catalog into in-memory SQLite (`starter/agent.py:38-70`), and `main` constructs an Agent once per run (`evaluator/local_evaluator.py:303-305`). Repeated full-set experiments therefore pay catalog parse/index cost each process run. Actual time and memory cannot be measured without the catalog.

### Primary Engineering Risks

1. **Release-asset availability and integrity — blocking.** Local evaluation and the claimed baseline cannot be verified until the exact 50,000-row catalog plus checksum information are supplied.
2. **Public-set overfitting — high.** All public target IDs are labeled and unique. Optimizing retrieval/features against all 200 sessions without a held-out split risks a misleading local score against 800 private sessions.
3. **Simulator-specific question policy — high.** The evaluator rewards correctly formatted `ask_attribute`, not semantic quality of prose. A clarification strategy can overfit the current deterministic classifier/reply policy rather than robust shopping conversation behavior.
4. **Intent-override state handling — high.** Hits before the override is delivered are discarded (`evaluator/local_evaluator.py:251-264`); an agent needs turn-aware state and must replace stale constraints.
5. **Silent failure degradation — medium/high.** Agent exceptions and malformed `message` fields produce misses without a raised run failure (`evaluator/local_evaluator.py:237-243`), requiring explicit local logging/contract tests in a future implementation.
6. **Undocumented official resource envelope — medium/high.** Submission rules allow network restrictions and refer to time/memory restrictions but provide no numbers locally (`docs/submission_rules.md:53-63`, `97-102`). Heavy dependencies or online-only retrieval could be invalid or operationally fragile.
7. **Dependency/reproducibility gap for future improvements — medium.** The starter’s zero-dependency posture is reproducible, but there is no manifest/template for any new embedding, reranking, or model client dependencies despite submission requirements requiring install instructions (`docs/submission_rules.md:75-85`).
8. **Documentation-package mismatch — medium.** README references missing public/organizer documents and a release checksum but lacks local retrieval information. This increases onboarding friction and makes it difficult to determine which missing items are intentional.

## Caveats / Not Found

- `data/catalog.jsonl`, `catalog.jsonl.gz`, `SHA256SUMS`, `results.json`, dependency manifests, resource-limit configuration, and the README-referenced release/judging checklists were not found in the local checkout.
- No external network lookup was performed: this report is limited to read-only local evidence. The actual GitHub Release URL, asset size, checksum, catalog schema details beyond documentation, and any official updates must be confirmed from organizer-controlled sources.
- The baseline score is documented but not independently validated in this environment due to the missing catalog.
- The absence of organizer documents is consistent with `.gitignore`; it should not be interpreted as evidence that such materials do not exist outside the participant kit.
