# Pre-change baseline

Captured on branch `feat/ecom-agent-architecture` at base commit `dbae00c05698a078202cba7c6a59c79715bb4683`, before the Agent tool-loop integration was imported by the evaluator process. No model/tool-planning environment variables were configured; the run used `data/catalog.jsonl` and `data/public_set.jsonl`.

Commands:

```bash
python3 -m unittest discover -s tests -v
python3 -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output /tmp/ecom-agent-before.json
```

Results:

- Unit tests: 45 passed.
- Public samples: 200.
- HitRate@10: 0.900000.
- MRR: 0.545181.
- MTTC: 4.485000.
- Efficiency: 0.651500.
- Recommended Technical Score: 0.743854.
- Reported token usage: 0.

Scenario HitRate@10:

- buying: 0.912500
- browsing: 0.987500
- intent_override: 0.666667
- boundary: 0.800000

The generated evaluator JSON remains in `/tmp/ecom-agent-before.json` and is not a tracked repository artifact.

## Post-change deterministic regression

The same public evaluator command was rerun after the tool-loop integration with tool planning disabled (the default), writing `/tmp/ecom-agent-after-deterministic.json`.

All reported values were byte-for-value identical to the pre-change metric summary:

- HitRate@10: 0.900000.
- MRR: 0.545181.
- MTTC: 4.485000.
- Efficiency: 0.651500.
- Recommended Technical Score: 0.743854.
- All four scenario summaries and reported token usage were unchanged.
