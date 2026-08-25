# Research: official-kit-analysis

- **Query**: 以官方一手资料核对 TechJam2026 / TechJam Conversational Search participant repository、participant-kit release、README/docs/API contract/submission rules，以及 Amazon Reviews 2023 来源；重点提取提交截止日期、运行环境、Agent 协议、评分公式、baseline 分数、限制和 FAQ。
- **Scope**: mixed (local participant-kit artifacts + external official dataset sources)
- **Date**: 2026-08-25

## Verification status and source boundary

### Important finding: the local kit is the only available competition-primary artifact

The working directory is **not a Git repository** and has no configured remote, GitHub Release artifact, release tag, `SHA256SUMS`, or `docs/participant_release_checklist.md` file available locally. The README references a GitHub Release and several organizer/participant documents, but the referenced files are absent and ignored by `.gitignore`.

Accordingly:

- Statements below attributed to `README.md`, `docs/*`, `evaluator/*`, and `starter/*` are verified against the supplied local participant-kit files, not against a public organizer GitHub repository.
- GitHub repository searches on 2026-08-25 did **not** identify a clearly organizer-owned public repository/release for “TechJam 2026 / Conversational Shopping Search.” Search hits were unrelated personal repositories. This is not proof that an organizer repository does not exist (it might be private, unindexed, differently named, or delivered through another portal).
- No public final submission deadline was present in the supplied kit, and no official public deadline could be verified from the permitted primary sources examined.
- The only independently verifiable external first-party source is the McAuley Lab Amazon Reviews’23 site and its linked official code repository.

## Findings

### Files Found

| File Path | Description |
|---|---|
| `README.md` | Participant-kit overview; catalog/public/private split claims; starter command; score summary; high-level policy. |
| `docs/competition_specification.md` | Canonical local rules for session lifecycle, scenario mix, Agent behavior, metrics, and deliverables. |
| `docs/agent_api_contract.json` | Machine-readable request/response contract, types, allowed values, and limits. |
| `docs/evaluation_config.json` | Metric configuration and official composite weights. |
| `docs/baseline_results.json` | Public-set weak-BM25 reference scores. |
| `docs/submission_rules.md` | Required deliverables, package content restrictions, reproducibility rules, and final-run constraints. |
| `evaluator/local_evaluator.py` | Executable truth for local scoring normalization, simulator behavior, and metric calculation. |
| `starter/agent.py` | Actual baseline implementation: in-memory SQLite FTS5 / BM25, with no LLM and no clarification. |
| `data/README.md` | Public session count/scenario counts and catalog row-count requirement. |
| `DATA_ATTRIBUTION.md` | Local data source declaration and restricted data contents. |
| `.gitignore` | Confirms some referenced release/provenance/checklist artifacts are intentionally not included locally. |
| `.trellis/tasks/08-25-shopping-copilot-research/prd.md` | User-provided contest description and unverified event claim (webinar dated 2026-08-28). |

### Provenance / release inventory

| Artifact | Provenance evidence | Tag / release / publication date | Status |
|---|---|---|---|
| Supplied participant kit | Local files listed above; no `.git` directory/remote available. README lines 24–33 says the catalog must come from “the GitHub Release attached to this repository.” | **Not ascertainable**: no repository URL, release tag, asset URL, SHA256SUMS, or release publication date is in the checkout. | Not externally verifiable from the supplied kit. |
| Referenced participant checklist | `README.md:100–106` names `docs/participant_release_checklist.md`. `.gitignore:14` ignores that exact file. | Missing locally. | Cannot review. |
| Referenced organizer judging files | `README.md:104–106` names `organizer/JUDGING_RUNBOOK.md`, `organizer/private_release_checklist.md`, and `organizer/JUDGING_DAY_SOP.md`. | Missing locally. | Organizer-only / cannot review. |
| Amazon Reviews 2023 official project | McAuley Lab: `https://amazon-reviews-2023.github.io/`; linked official code: `https://github.com/hyp1231/AmazonReviews2023`. | Project page copyright says 2024. Official GitHub repository API: created `2024-01-25T22:58:41Z`; default branch `main`; no GitHub tags/releases returned on 2026-08-25. Last `main` commit: `b18fdf54bd46013d60799684f7a4eb80d8501d1a`, `2025-03-11T18:15:48Z`. | Independently verified primary source. |

## Verified participant-kit specification

### Challenge description and data boundaries

The local README describes a multi-turn shopping agent that should identify a hidden target product within at most ten turns (`README.md:1–3, 22`). It supplies / claims:

- A frozen catalog of 50,000 products from `Clothing_Shoes_and_Jewelry` (`README.md:5–12`; `docs/competition_specification.md:15–21`).
- 200 labeled public development sessions and 800 organizer-private final sessions (`README.md:7–12`; `docs/competition_specification.md:19`).
- Public scenario mix: 80 Buying, 80 Browsing, 30 Intent Override, and 10 Boundary (`data/README.md:3–7`), matching the stated proportions 40% / 40% / 15% / 5% (`docs/competition_specification.md:23–28`).
- Participant-visible catalog fields: `parent_asin`, `title`, `features`, `description`, `price`, `categories`, `details`, `average_rating`, `rating_number`, and `store`; only exact `parent_asin` is scored (`docs/competition_specification.md:15–18`; evaluator `local_evaluator.py:112–123`).
- The agent gets only aggregate profile fields, while direct user IDs, timestamps, raw review text, and purchase histories are removed (`docs/competition_specification.md:19–21`; `data/README.md:5–7`).

The local specification explicitly says that the hidden target comes from a real Amazon Reviews 2023 purchase record but the customer messages are **simulated from hidden product-metadata intent cards**, not real shopping conversations (`docs/competition_specification.md:5–8`).

### Runtime / execution environment

**Verified minimum participant-side setup:**

- `README.md:35–44` recommends Python **3.10+** and says the starter uses only the Python standard library.
- The documented local evaluation command is `python3 -m evaluator.local_evaluator` (`README.md:35–44`).
- The evaluator reads `data/catalog.jsonl` and `data/public_set.jsonl`, writes `results.json`, and imports `Agent` from `starter.agent` (`evaluator/local_evaluator.py:11–15, 298–307`).
- The starter uses Python `sqlite3` in-memory FTS5 (`starter/agent.py:1–7, 35–71`). Therefore a compatible Python build must have SQLite FTS5 enabled; this is an implementation implication, not an explicit competition requirement.
- The catalog download/decompression instructions expect `catalog.jsonl.gz` from a GitHub Release, decompressed to `data/catalog.jsonl`; `data/README.md:9–12` expects 50,000 rows. The actual release URL, checksum file, and artifact version are unavailable.

**Final-run environment:** exact CPU, RAM, timeout, platform/OS, Python patch version, package allowlist, and network state are **not published in the supplied artifacts**. The only published restriction is that final scoring *may* disable network access and that organizers may run under CPU, memory, timeout, and network restrictions (`docs/submission_rules.md:54–65, 98–103`). Treat external APIs as unavailable unless the organizer confirms otherwise.

### Required Agent protocol

Source: `docs/agent_api_contract.json:1–69`, corroborated by `docs/competition_specification.md:42–65` and `docs/submission_rules.md:16–32`.

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None: ...
    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict: ...
```

**Input contract:**

- `reset`: `session_id` non-empty string plus `user_profile`; its required, closed fields are `purchase_frequency`, `average_prior_rating` (number or null), `rating_style`, `preference_tags` (string list), and `summary` (`docs/agent_api_contract.json:4–23`).
- `respond`: `session_id`, `user_message`, `turn` integer 1–10, and `top_k` fixed at 10; no extra request fields (`docs/agent_api_contract.json:24–34`).

**Response contract:**

- Required: `message` string, `ask_attribute`, and `recommendations` (`docs/agent_api_contract.json:35–56`).
- `ask_attribute` must be `null` or exactly one of: `category`, `material`, `color`, `size`, `style`, `brand`, `budget`, `feature`, `use_case`, `other` (`docs/agent_api_contract.json:40–43`). The evaluator uses this structured field, not inferred meaning in prose (`docs/competition_specification.md:58–62`).
- `recommendations` may contain up to 100 objects, each requiring `parent_asin`; optional numeric `score` is accepted but ignored (`docs/agent_api_contract.json:44–55`; `docs/competition_specification.md:62–64`).
- `usage` is optional, but if emitted requires non-negative integer `prompt_tokens` and `completion_tokens` (`docs/agent_api_contract.json:57–65`).

**Actual evaluator normalization / lifecycle:**

- The executable evaluator drops invalid or duplicate product IDs and scores only the first **10** unique IDs found in the loaded catalog (`evaluator/local_evaluator.py:95–109`).
- It invokes `reset`, sends an initial customer message, calls `respond` once per turn, and ends on a valid target hit or after turn 10 (`evaluator/local_evaluator.py:224–268`; spec `docs/competition_specification.md:30–40`).
- Exceptions and malformed/non-dict responses are converted to an empty recommendation response locally (`evaluator/local_evaluator.py:239–245`); the specification says exceptions, invalid output, and timeouts may count as misses (`docs/competition_specification.md:64–65`).
- Intent Override cannot convert before the new intent is delivered (`docs/competition_specification.md:35–38`; executable guard `evaluator/local_evaluator.py:231, 251–265`).
- The simulator uses `ask_attribute` to decide the next disclosure. Missing it yields “Ask me about one specific attribute”; Boundary can explicitly respond with no preference (`evaluator/local_evaluator.py:166–185`).

### Official local scoring formula

Sources: `docs/evaluation_config.json:1–15`, `docs/competition_specification.md:67–77`, and executable implementation `evaluator/local_evaluator.py:188–201, 278–295`.

```text
HitRate@10 = successful sessions / N
MRR = sum(1 / target_rank; misses = 0) / N
MTTC = mean(first-hit turn; misses = 11)
Efficiency = clip((11 - MTTC) / 10, 0, 1)
TechnicalScore = 0.50 * HitRate@10 + 0.30 * MRR + 0.20 * Efficiency
```

Additional confirmed behavior:

- Exact `parent_asin` equality is required (`docs/evaluation_config.json:2–6`; `README.md:81–82`).
- Per-scenario metrics are reported for Buying, Browsing, Intent Override, and Boundary (`docs/evaluation_config.json:7–14`; evaluator `local_evaluator.py:291–294`).
- Reported token usage is aggregated but does not affect the core TechnicalScore (`docs/competition_specification.md:77`; evaluator `local_evaluator.py:245–250, 288–293`).

### Baseline and its verified implementation

The published local baseline reference is:

| Metric | Value | Source |
|---|---:|---|
| Baseline | `weak_bm25` | `docs/baseline_results.json:2` |
| Dataset / samples | `data/public_set.jsonl` / 200 | `docs/baseline_results.json:3–4` |
| Hit Rate@10 | 0.125 | `docs/baseline_results.json:5` |
| MRR | 0.068034 | `docs/baseline_results.json:6` |
| MTTC | 9.81 | `docs/baseline_results.json:7` |
| Efficiency | 0.119 | `docs/baseline_results.json:8` |
| TechnicalScore | 0.10671 | `docs/baseline_results.json:9` |

Implementation facts (`starter/agent.py:35–102`):

- It is explicitly a **stateless** BM25 baseline with no LLM dependency.
- At construction it reads the entire local catalog into in-memory SQLite FTS5.
- Indexed text fields are `title`, `categories`, `features`, `details`, `store`, and `description`.
- Query terms are taken only from the current `user_message`, with stopword removal and an OR expression; it does not accumulate dialog state or use the profile.
- FTS5 BM25 ranks results with weighted columns: parent ID 0.0, title 6.0, categories 4.0, features 2.5, details 2.5, store 1.5, description 1.0.
- It always returns `ask_attribute: None`, a generic response message, and zero token usage.

### Submission contents and restrictions

Source: `docs/submission_rules.md:6–103`; supplemental model policy in `README.md:84–86` and `docs/competition_specification.md:89–91`.

**Must submit:** an entry Python file exporting `Agent`, helper modules, setup instructions, a short report (method/model/limits), plus latency/token/cost disclosure (`docs/submission_rules.md:6–15`). Final deliverables also list source/reproduction instructions, working Agent, report, and one demonstrated multi-turn session (`docs/competition_specification.md:93–99`).

**May include:** Python, small local configuration/assets, dependency manifest, and install instructions (`docs/submission_rules.md:34–42`).

**Must not include:** private evaluation data, organizer-only files, API keys/secrets, privileged-host-access code, evaluator modification, or undeclared external-service dependencies used for official scoring (`docs/submission_rules.md:43–52`).

**Reproducibility:** specify non-default Python version, dependency installation, one official-harness run command, and non-obvious environment variables. A non-reproducible bundle may be invalidated (`docs/submission_rules.md:76–86`).

**Model policy:** any legally accessible LLM API or local model may be used during development, but teams bear credentials and costs. Keys must be environment variables and never committed (`README.md:84–86`; `docs/competition_specification.md:89–91`). For final judging, explicitly disclose network dependency and offline fallback because network may be disabled (`docs/submission_rules.md:54–65`).

### FAQ / ambiguity register

No standalone FAQ document was supplied or discovered in a verified organizer public source. The following are the locally answerable FAQ-equivalent points and open questions.

| Question | Answer / status | Evidence |
|---|---|---|
| What is the final submission deadline? | **Not found / not publicly verifiable.** No local file states a date/time/time zone; the local tree lacks the referenced release checklist and organizer artifacts. | Local full-text search; `README.md:100–106`; `.gitignore:14`. |
| Is there a webinar? | The task PRD says an organizer webinar is planned for **2026-08-28 16:00–16:45**, but no organizer URL, timezone, invitation, or public primary source is supplied. Treat as an unverified user/project claim. | `.trellis/tasks/08-25-shopping-copilot-research/prd.md:7–10, 27, 50`. |
| Can we use live LLM APIs in final scoring? | Not assured. They are allowed for development, but final network can be disabled; disclose a dependency/fallback. | `docs/submission_rules.md:54–65`. |
| Are exact host resource limits published? | No. Only a reservation of CPU/memory/timeout/network restrictions is published. | `docs/submission_rules.md:98–103`. |
| Can the Agent return more than 10 recommendations? | Schema allows up to 100, but only the first 10 valid unique catalog IDs are scored. | `docs/agent_api_contract.json:44–56`; evaluator `local_evaluator.py:95–109`. |
| Can it ask and recommend on the same turn? | Yes. Response contains both `ask_attribute`/`message` and recommendations. | `README.md:16–20`; contract. |
| Does natural-language wording determine the simulator reply? | No; structured `ask_attribute` controls simulator behavior. | `docs/competition_specification.md:60–62`. |
| Do token counts change TechnicalScore? | No. They are reported feasibility information only. | `docs/competition_specification.md:77`. |
| What counts as a miss? | No hit after 10 turns, error/invalid output/timeout; for MTTC, a miss contributes 11. | `docs/competition_specification.md:64–65, 69–75`; evaluator `local_evaluator.py:193–195`. |

## Amazon Reviews 2023 source verification

### Official source and category mapping

The source named by the kit is credible and independently verified:

- McAuley Lab official project page: `https://amazon-reviews-2023.github.io/`.
- Official code repository linked by that page: `https://github.com/hyp1231/AmazonReviews2023`.
- The project page identifies the corpus as a large-scale dataset collected in 2023 by McAuley Lab, describes reviews, item metadata, and graph links, and reports 571.54M reviews across 33 domains.
- The project page has a `Clothing_Shoes_and_Jewelry` category with official review and metadata download links; its displayed category statistics are 22.6M users, 7.2M items, and 66.0M ratings. This is the source category named by `DATA_ATTRIBUTION.md:3–8` and `README.md:7`.
- The source docs define `parent_asin` as the parent product ID and say it should be used to find item metadata. They also list metadata fields consistent with the kit’s allowed catalog subset: title, ratings, features, description, price, store, categories, details, and `parent_asin`.

### Kit-specific transformation claims

The competition package’s claims are local only and cannot be independently reconstructed without its release artifact and transformation code:

- It samples a 50,000-item frozen catalog from the source category.
- It says sessions are sampled deterministically from an official Clothing 5-core leave-last-out split (`README.md:108–112`).
- The local package removes direct IDs/reviews/timestamps/raw history and exposes safe aggregate profiles (`DATA_ATTRIBUTION.md:10–12`; `docs/competition_specification.md:19–21`).
- No catalog asset, `SHA256SUMS`, source row list, sampling seed, transformation script, or release tag is included, so the exact frozen catalog and deterministic split cannot be verified or reproduced from first-party public data alone.

## Caveats / Not Found

1. **Final deadline: not found.** Do not invent one. Obtain organizer confirmation with exact date, time, and time zone.
2. **Organizer participant GitHub repository/release: not externally verified.** The supplied README’s phrase “this repository” is insufficient because the local checkout has no Git remote. Request the canonical repository URL and release tag/asset hashes.
3. **Release integrity data: missing.** README requests SHA-256 verification, but neither `catalog.jsonl.gz` nor `SHA256SUMS` is present. The intended catalog cannot be authenticated or evaluated locally from this checkout.
4. **Referenced operational documents are absent.** `participant_release_checklist.md` and all named `organizer/` documents cannot be inspected; they may contain decisive final-environment or deadline details.
5. **Local vs final evaluator parity is unproven.** The local evaluator is clear and deterministic, but the organizer-only final harness, private set, timeouts, and environment are unavailable. Treat public metrics as development signals, not a guarantee of final behavior.
6. **Potential conflict in the project description:** the task PRD calls the challenge “TechJam 2026” and specifies an Aug. 28 webinar, but those claims are not present in the supplied participant-facing README/spec/rules. They require organizer confirmation.
7. **No standalone FAQ found.** The FAQ table above only synthesizes direct answers from the local supplied documents; it is not organizer-published FAQ text.

## External References

- [Amazon Reviews’23 — McAuley Lab official project](https://amazon-reviews-2023.github.io/) — primary dataset provenance, category statistics, field definitions, downloads, and citation.
- [hyp1231/AmazonReviews2023 — official linked code repository](https://github.com/hyp1231/AmazonReviews2023) — primary companion code; inspected default branch `main`, commit `b18fdf54bd46013d60799684f7a4eb80d8501d1a` dated 2025-03-11. No GitHub Releases/tags were returned by the repository API as of 2026-08-25.

## Related Specs

- `.trellis/tasks/08-25-shopping-copilot-research/prd.md` — user/project planning context; contains claims explicitly separated above from confirmed primary kit facts.
- `docs/competition_specification.md` — locally supplied rules/protocol source.
- `docs/agent_api_contract.json` — locally supplied protocol source.
- `docs/submission_rules.md` — locally supplied submission/reproducibility source.
