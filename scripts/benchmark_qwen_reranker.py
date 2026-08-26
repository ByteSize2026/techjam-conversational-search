"""Offline benchmark helpers for the Qwen3 shopping reranker experiment.

This module deliberately does not import a model runtime or download anything.
It provides three small pieces of experiment infrastructure:

* a target-free, scenario/difficulty-stratified manifest builder;
* process and latency measurement helpers using the Python standard library;
* an evaluator-result comparison report for feature-only and reranked runs.

The ``baseline`` CLI subcommand can run the repository's deterministic Agent
with an explicitly supplied catalog and public set.  The ``compare`` command
only reads local JSON files, so it is also useful before a reranker exists.
All default output is placed under the external-disk benchmark directory.
No function in this file performs network access or model installation.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any


# Keep the default artifact location explicit and outside the repository.  A
# caller may provide another artifact root explicitly, but an omitted root
# must never silently become ~/.cache, /tmp, or the checkout.
EXTERNAL_ROOT = Path("/Volumes/PeeB/ai-models/techjam")
DEFAULT_ARTIFACT_ROOT = EXTERNAL_ROOT / "benchmarks"
DEFAULT_ASSET_LIMIT_BYTES = 8 * 1024**3
DEFAULT_SPLIT_RATIOS = {
    "dev": 0.60,
    "validation": 0.20,
    "locked": 0.20,
}
SPLIT_NAMES = tuple(DEFAULT_SPLIT_RATIOS)
METRIC_NAMES = (
    "hit_rate_at_10",
    "mrr",
    "mttc",
    "efficiency",
    "technical_score",
    "top1_hit_rate",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _clean_text(value: object, *, default: str = "") -> str:
    return " ".join(str(value or "").split()).strip() or default


def _read_jsonl(path: str | Path) -> list[dict[str, object]]:
    """Read non-empty UTF-8 JSONL rows and reject non-object rows."""

    rows: list[dict[str, object]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row at {path}:{line_number} is not an object")
            rows.append(value)
    return rows


def _sample_metadata(sample: Mapping[str, object]) -> dict[str, str]:
    sample_id = _clean_text(sample.get("sample_id"))
    scenario = _clean_text(sample.get("scenario_type") or sample.get("scenario"))
    difficulty = _clean_text(
        sample.get("difficulty_bucket") or sample.get("difficulty"),
        default="unknown",
    )
    if not sample_id:
        raise ValueError("public sample is missing a non-empty sample_id")
    if not scenario:
        raise ValueError(f"public sample {sample_id} is missing scenario_type")
    return {
        "sample_id": sample_id,
        "scenario": scenario,
        "difficulty": difficulty,
    }


def _validate_ratios(
    dev_ratio: float,
    validation_ratio: float,
    locked_ratio: float,
) -> dict[str, float]:
    values = {
        "dev": float(dev_ratio),
        "validation": float(validation_ratio),
        "locked": float(locked_ratio),
    }
    if any(not math.isfinite(value) or value < 0 for value in values.values()):
        raise ValueError("split ratios must be finite non-negative numbers")
    total = sum(values.values())
    if total <= 0:
        raise ValueError("split ratios must have a positive sum")
    return {name: value / total for name, value in values.items()}


def _allocate_counts(total: int, ratios: Mapping[str, float]) -> dict[str, int]:
    """Allocate a stratum with largest-remainder rounding.

    The split order is stable and therefore ties are deterministic.  Small
    strata can legitimately have an empty locked split; the public set has
    enough rows per scenario for the normal experiment to populate all three.
    """

    names = tuple(ratios)
    raw = [total * float(ratios[name]) for name in names]
    counts = [math.floor(value) for value in raw]
    remainder = total - sum(counts)
    order = sorted(
        range(len(names)),
        key=lambda index: (-(raw[index] - counts[index]), index),
    )
    for index in order[:remainder]:
        counts[index] += 1
    return dict(zip(names, counts))


def _stable_order(
    entries: Sequence[dict[str, str]],
    *,
    seed: int,
    scenario: str,
    difficulty: str,
) -> list[dict[str, str]]:
    def key(entry: dict[str, str]) -> tuple[str, str]:
        payload = f"{seed}\0{scenario}\0{difficulty}\0{entry['sample_id']}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest(), entry["sample_id"]

    return sorted(entries, key=key)


def build_manifest(
    samples: Iterable[Mapping[str, object]],
    *,
    seed: int = 0,
    dev_ratio: float = DEFAULT_SPLIT_RATIOS["dev"],
    validation_ratio: float = DEFAULT_SPLIT_RATIOS["validation"],
    locked_ratio: float = DEFAULT_SPLIT_RATIOS["locked"],
) -> dict[str, object]:
    """Build a deterministic target-free experiment manifest.

    Every split entry contains exactly ``sample_id``, ``scenario`` and
    ``difficulty``.  In particular, ``ground_truth``, ``parent_asin`` and
    user profile fields are intentionally never copied into the manifest.
    Stratification is performed by ``(scenario, difficulty)`` so the locked
    split remains representative of both the protocol scenario and its
    difficulty buckets.
    """

    if isinstance(seed, bool):
        raise ValueError("seed must be an integer")
    try:
        normalized_seed = int(seed)
    except (TypeError, ValueError) as exc:
        raise ValueError("seed must be an integer") from exc
    ratios = _validate_ratios(dev_ratio, validation_ratio, locked_ratio)

    metadata: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    strata: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError("public samples must be objects")
        entry = _sample_metadata(sample)
        if entry["sample_id"] in seen_ids:
            raise ValueError(f"duplicate public sample_id: {entry['sample_id']}")
        seen_ids.add(entry["sample_id"])
        metadata.append(entry)
        strata[(entry["scenario"], entry["difficulty"])].append(entry)

    splits: dict[str, list[dict[str, str]]] = {name: [] for name in SPLIT_NAMES}
    for (scenario, difficulty), entries in sorted(strata.items()):
        ordered = _stable_order(
            entries,
            seed=normalized_seed,
            scenario=scenario,
            difficulty=difficulty,
        )
        counts = _allocate_counts(len(ordered), ratios)
        offset = 0
        for split in SPLIT_NAMES:
            count = counts[split]
            splits[split].extend(ordered[offset : offset + count])
            offset += count

    # Keep each split's order reproducible without exposing any data beyond
    # its three metadata fields.  The IDs are repeated in a convenience map
    # because consumers often need only the frozen ID list.
    for split in SPLIT_NAMES:
        splits[split].sort(key=lambda entry: entry["sample_id"])
    split_ids = {
        split: [entry["sample_id"] for entry in splits[split]]
        for split in SPLIT_NAMES
    }
    scenario_counts = Counter(entry["scenario"] for entry in metadata)
    difficulty_counts = Counter(entry["difficulty"] for entry in metadata)
    split_counts = {
        split: dict(sorted(Counter(entry["scenario"] for entry in splits[split]).items()))
        for split in SPLIT_NAMES
    }
    return {
        "schema_version": "qwen3-reranker-manifest-v1",
        "seed": normalized_seed,
        "stratified_by": ["scenario", "difficulty"],
        "ratios": {name: round(value, 12) for name, value in ratios.items()},
        "source": {
            "sample_count": len(metadata),
            "scenario_counts": dict(sorted(scenario_counts.items())),
            "difficulty_counts": dict(sorted(difficulty_counts.items())),
        },
        "splits": splits,
        "split_ids": split_ids,
        "split_scenario_counts": split_counts,
    }


def generate_manifest(
    public_set_path: str | Path,
    *,
    seed: int = 0,
    dev_ratio: float = DEFAULT_SPLIT_RATIOS["dev"],
    validation_ratio: float = DEFAULT_SPLIT_RATIOS["validation"],
    locked_ratio: float = DEFAULT_SPLIT_RATIOS["locked"],
) -> dict[str, object]:
    """Read a public JSONL set and build its deterministic manifest."""

    return build_manifest(
        _read_jsonl(public_set_path),
        seed=seed,
        dev_ratio=dev_ratio,
        validation_ratio=validation_ratio,
        locked_ratio=locked_ratio,
    )


def write_json(value: Mapping[str, object], path: str | Path) -> Path:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def load_manifest(path: str | Path) -> dict[str, object]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("splits"), dict):
        raise ValueError(f"invalid benchmark manifest: {source}")
    return value


def manifest_sample_ids(manifest: Mapping[str, object], split: str = "all") -> list[str]:
    """Return IDs for one split, accepting the canonical and legacy shapes."""

    if split not in {"all", *SPLIT_NAMES}:
        raise ValueError(f"unknown split: {split}")
    raw_ids = manifest.get("split_ids")
    raw_splits = manifest.get("splits")
    if not isinstance(raw_splits, Mapping):
        raise ValueError("manifest has no splits")

    def values_for(name: str) -> list[str]:
        source: object
        if isinstance(raw_ids, Mapping) and name in raw_ids:
            source = raw_ids[name]
        else:
            source = raw_splits.get(name, [])
        if not isinstance(source, list):
            raise ValueError(f"manifest split {name} is not a list")
        values: list[str] = []
        for item in source:
            if isinstance(item, Mapping):
                item = item.get("sample_id")
            value = _clean_text(item)
            if value:
                values.append(value)
        if len(values) != len(set(values)):
            raise ValueError(f"manifest split {name} contains duplicate IDs")
        return values

    if split != "all":
        return values_for(split)
    output: list[str] = []
    seen: set[str] = set()
    for name in SPLIT_NAMES:
        for value in values_for(name):
            if value in seen:
                raise ValueError(f"sample ID appears in multiple manifest splits: {value}")
            seen.add(value)
            output.append(value)
    return output


def percentile(values: Iterable[float], percentile_value: float) -> float:
    """Return a linearly interpolated percentile in milliseconds or units.

    The implementation follows the common ``(n - 1) * p`` interpolation
    convention.  Empty input and percentages outside [0, 100] are errors;
    callers that need an empty report should use :func:`summarize_latencies`.
    """

    try:
        p = float(percentile_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("percentile must be numeric") from exc
    if not math.isfinite(p) or p < 0 or p > 100:
        raise ValueError("percentile must be between 0 and 100")
    cleaned_values: list[float] = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            cleaned_values.append(parsed)
    cleaned = sorted(cleaned_values)
    if not cleaned:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    if len(cleaned) == 1:
        return cleaned[0]
    rank = (len(cleaned) - 1) * p / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return cleaned[lower]
    fraction = rank - lower
    return cleaned[lower] + (cleaned[upper] - cleaned[lower]) * fraction


def summarize_latencies(
    latencies_ms: Iterable[float],
    *,
    wall_time_ms: float | None = None,
) -> dict[str, object]:
    cleaned: list[float] = []
    for value in latencies_ms:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            cleaned.append(parsed)
    if not cleaned:
        summary: dict[str, object] = {
            "sample_count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    else:
        summary = {
            "sample_count": len(cleaned),
            "p50_ms": round(percentile(cleaned, 50), 6),
            "p95_ms": round(percentile(cleaned, 95), 6),
            "max_ms": round(max(cleaned), 6),
        }
    summary["wall_time_ms"] = (
        None
        if wall_time_ms is None
        else round(max(float(wall_time_ms), 0.0), 6)
    )
    return summary


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError, TypeError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    # Darwin reports bytes; Linux and the other common Unix platforms report
    # KiB.  Keep the conversion local so the report remains portable.
    if sys.platform == "darwin":
        return int(value)
    return int(value * 1024)


def _current_rss_bytes() -> int | None:
    """Read current RSS where the OS exposes it through a stdlib file."""

    statm = Path("/proc/self/statm")
    if not statm.exists():
        return None
    try:
        resident_pages = int(statm.read_text(encoding="ascii").split()[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        return max(resident_pages, 0) * max(page_size, 1)
    except (OSError, IndexError, ValueError, TypeError):
        return None


def process_memory_usage() -> dict[str, int | None]:
    """Return current and peak process RSS using only stdlib facilities."""

    return {
        "current_rss_bytes": _current_rss_bytes(),
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def directory_size_bytes(path: str | Path) -> int:
    """Sum regular-file bytes without following symlinks."""

    root = Path(path).expanduser()
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_file():
        return int(root.stat().st_size)
    if not root.is_dir():
        raise ValueError(f"asset path is not a file or directory: {root}")
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            try:
                if candidate.is_file():
                    total += int(candidate.stat().st_size)
            except OSError:
                continue
    return total


def validate_external_path(
    path: str | Path,
    *,
    root: str | Path = EXTERNAL_ROOT,
) -> Path:
    """Validate a model/cache/asset path stays under the external root."""

    candidate = Path(path).expanduser().resolve()
    external_root = Path(root).expanduser().resolve()
    try:
        candidate.relative_to(external_root)
    except ValueError as exc:
        raise ValueError(
            f"path must stay under external root {external_root}: {candidate}"
        ) from exc
    return candidate


def resolve_artifact_root(value: str | Path | None) -> Path:
    """Resolve the default external artifact root or an explicitly supplied one."""

    if value is None:
        return DEFAULT_ARTIFACT_ROOT
    candidate = Path(value).expanduser().resolve()
    if candidate in {Path("/"), Path.home().resolve()}:
        raise ValueError("artifact root is too broad; choose a task directory")
    return candidate


def measure_callable(
    function: Callable[[], object],
    *,
    iterations: int = 1,
    warmups: int = 0,
) -> dict[str, object]:
    """Measure repeated calls without retaining their potentially large outputs."""

    if iterations <= 0 or warmups < 0:
        raise ValueError("iterations must be positive and warmups non-negative")
    for _ in range(warmups):
        function()
    latencies: list[float] = []
    started = time.perf_counter()
    for _ in range(iterations):
        call_started = time.perf_counter()
        function()
        latencies.append((time.perf_counter() - call_started) * 1000.0)
    wall_time_ms = (time.perf_counter() - started) * 1000.0
    return {
        "wall_time_ms": round(wall_time_ms, 6),
        "latency_ms": summarize_latencies(latencies, wall_time_ms=wall_time_ms),
        "process_memory": process_memory_usage(),
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _metric_from_sessions(sessions: Sequence[Mapping[str, object]]) -> dict[str, object]:
    count = len(sessions)
    if count == 0:
        return {
            "sample_count": 0,
            "hit_rate_at_10": 0.0,
            "mrr": 0.0,
            "mttc": None,
            "efficiency": None,
            "technical_score": None,
            "top1_hit_rate": 0.0,
        }
    hits = [bool(session.get("hit")) for session in sessions]
    reciprocal_ranks: list[float] = []
    turns: list[float] = []
    top1_hits: list[bool] = []
    for session, hit in zip(sessions, hits):
        reciprocal = _number(session.get("reciprocal_rank"))
        best_rank = _number(session.get("best_rank"))
        if reciprocal is None:
            reciprocal = 1.0 / best_rank if hit and best_rank and best_rank > 0 else 0.0
        reciprocal_ranks.append(reciprocal)
        first_turn = _number(session.get("first_hit_turn"))
        turns.append(first_turn if hit and first_turn is not None else 11.0)
        top1_hits.append(bool(hit and best_rank == 1))
    hit_rate = statistics.fmean(int(value) for value in hits)
    mrr = statistics.fmean(reciprocal_ranks)
    mttc = statistics.fmean(turns)
    efficiency = max(0.0, min(1.0, (11.0 - mttc) / 10.0))
    technical = 0.50 * hit_rate + 0.30 * mrr + 0.20 * efficiency
    return {
        "sample_count": count,
        "hit_rate_at_10": round(hit_rate, 6),
        "mrr": round(mrr, 6),
        "mttc": round(mttc, 6),
        "efficiency": round(efficiency, 6),
        "technical_score": round(technical, 6),
        "top1_hit_rate": round(statistics.fmean(int(value) for value in top1_hits), 6),
    }


def _aggregate_metric(data: Mapping[str, object]) -> dict[str, object]:
    hit = _number(data.get("hit_rate_at_10"))
    mrr = _number(data.get("mrr"))
    mttc = _number(data.get("mttc"))
    efficiency = _number(data.get("efficiency"))
    if efficiency is None and mttc is not None:
        efficiency = max(0.0, min(1.0, (11.0 - mttc) / 10.0))
    technical = _number(data.get("technical_score"))
    if technical is None:
        technical = _number(data.get("recommended_technical_score"))
    if technical is None and hit is not None and mrr is not None and efficiency is not None:
        technical = 0.50 * hit + 0.30 * mrr + 0.20 * efficiency
    sample_count = _number(data.get("sample_count"))
    top1 = _number(data.get("top1_hit_rate"))
    return {
        "sample_count": int(sample_count) if sample_count is not None else None,
        "hit_rate_at_10": None if hit is None else round(hit, 6),
        "mrr": None if mrr is None else round(mrr, 6),
        "mttc": None if mttc is None else round(mttc, 6),
        "efficiency": None if efficiency is None else round(efficiency, 6),
        "technical_score": None if technical is None else round(technical, 6),
        "top1_hit_rate": None if top1 is None else round(top1, 6),
    }


def _session_list(data: Mapping[str, object]) -> list[Mapping[str, object]] | None:
    raw = data.get("sessions")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError("result sessions must be a list of objects")
    return list(raw)


def _metrics_for_result(
    data: Mapping[str, object],
    *,
    manifest: Mapping[str, object] | None,
    split: str,
) -> tuple[dict[str, object], dict[str, dict[str, object]], set[str] | None]:
    sessions = _session_list(data)
    expected_ids: set[str] | None = None
    if manifest is not None:
        expected_ids = set(manifest_sample_ids(manifest, split))
    if sessions is not None:
        available_ids = {
            _clean_text(session.get("sample_id"))
            for session in sessions
            if _clean_text(session.get("sample_id"))
        }
        if expected_ids is not None:
            missing = expected_ids - available_ids
            if missing:
                raise ValueError(
                    f"result is missing {len(missing)} IDs from manifest split {split}"
                )
            sessions = [
                session
                for session in sessions
                if _clean_text(session.get("sample_id")) in expected_ids
            ]
        overall = _metric_from_sessions(sessions)
        grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for session in sessions:
            scenario = _clean_text(session.get("scenario_type") or session.get("scenario"), default="unknown")
            grouped[scenario].append(session)
        scenario_metrics = {
            name: _metric_from_sessions(grouped[name]) for name in sorted(grouped)
        }
        return overall, scenario_metrics, available_ids if expected_ids is None else expected_ids

    if split != "all":
        raise ValueError("a manifest split requires result JSON with sessions")
    overall = _aggregate_metric(data)
    raw_scenarios = data.get("scenario_metrics")
    scenario_metrics: dict[str, dict[str, object]] = {}
    if isinstance(raw_scenarios, Mapping):
        for name, value in raw_scenarios.items():
            if isinstance(value, Mapping):
                scenario_metrics[str(name)] = _aggregate_metric(value)
    return overall, scenario_metrics, None


def _delta(candidate: object, baseline: object) -> float | None:
    candidate_value = _number(candidate)
    baseline_value = _number(baseline)
    if candidate_value is None or baseline_value is None:
        return None
    return round(candidate_value - baseline_value, 6)


def _metric_delta(
    candidate: Mapping[str, object],
    baseline: Mapping[str, object],
) -> dict[str, float | None]:
    return {
        name: _delta(candidate.get(name), baseline.get(name))
        for name in METRIC_NAMES
    }


def _resource_from_result(data: Mapping[str, object]) -> object | None:
    benchmark = data.get("benchmark")
    if isinstance(benchmark, Mapping) and benchmark.get("resource") is not None:
        return benchmark.get("resource")
    if data.get("resource") is not None:
        return data.get("resource")
    return None


def compare_result_data(
    baseline: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    baseline_label: str = "feature_only",
    candidate_label: str = "reranked",
    manifest: Mapping[str, object] | None = None,
    split: str = "all",
    max_hit_rate_regression: float = 0.01,
    max_mttc_regression: float = 0.50,
    asset_size_bytes: int | None = None,
    asset_limit_bytes: int = DEFAULT_ASSET_LIMIT_BYTES,
) -> dict[str, object]:
    """Compare two evaluator result objects without exposing target IDs."""

    if max_hit_rate_regression < 0 or max_mttc_regression < 0:
        raise ValueError("guardrail tolerances must be non-negative")
    baseline_metrics, baseline_scenarios, baseline_ids = _metrics_for_result(
        baseline,
        manifest=manifest,
        split=split,
    )
    candidate_metrics, candidate_scenarios, candidate_ids = _metrics_for_result(
        candidate,
        manifest=manifest,
        split=split,
    )
    baseline_count = baseline_metrics.get("sample_count")
    candidate_count = candidate_metrics.get("sample_count")
    same_sample_count = (
        baseline_count is not None
        and candidate_count is not None
        and baseline_count == candidate_count
    )
    if baseline_ids is None or candidate_ids is None:
        same_sample_ids: bool | None = None
    else:
        same_sample_ids = baseline_ids == candidate_ids

    overall_delta = _metric_delta(candidate_metrics, baseline_metrics)
    hit_delta = overall_delta["hit_rate_at_10"]
    mttc_delta = overall_delta["mttc"]
    hit_ok = hit_delta is not None and hit_delta >= -float(max_hit_rate_regression)
    mttc_ok = mttc_delta is not None and mttc_delta <= float(max_mttc_regression)
    asset_available = asset_size_bytes is not None
    asset_ok = (
        True
        if not asset_available
        else int(asset_size_bytes) <= int(asset_limit_bytes)
    )
    same_ids_ok = same_sample_ids is not False
    guardrails = {
        "same_sample_count": same_sample_count,
        "same_sample_ids": {
            "available": same_sample_ids is not None,
            "passed": same_ids_ok,
        },
        "hit_rate_at_10": {
            "delta": hit_delta,
            "max_allowed_regression": round(float(max_hit_rate_regression), 6),
            "passed": hit_ok,
        },
        "mttc": {
            "delta": mttc_delta,
            "max_allowed_regression": round(float(max_mttc_regression), 6),
            "passed": mttc_ok,
        },
        "technical_score": {
            "delta": overall_delta["technical_score"],
            "improved_or_equal": (
                overall_delta["technical_score"] is not None
                and overall_delta["technical_score"] >= 0
            ),
        },
        "external_assets": {
            "available": asset_available,
            "size_bytes": asset_size_bytes,
            "limit_bytes": asset_limit_bytes,
            "passed": asset_ok,
        },
    }
    guardrails["passed"] = bool(
        same_sample_count and same_ids_ok and hit_ok and mttc_ok and asset_ok
    )

    scenario_names = sorted(set(baseline_scenarios) | set(candidate_scenarios))
    scenario_report: dict[str, object] = {}
    for name in scenario_names:
        before = baseline_scenarios.get(name, {})
        after = candidate_scenarios.get(name, {})
        scenario_report[name] = {
            "baseline": before,
            "candidate": after,
            "delta": _metric_delta(after, before),
        }
    return {
        "schema_version": "qwen3-reranker-comparison-v1",
        "split": split,
        "baseline": {
            "label": baseline_label,
            "metrics": baseline_metrics,
            "scenario_metrics": baseline_scenarios,
            "resource": _resource_from_result(baseline),
        },
        "candidate": {
            "label": candidate_label,
            "metrics": candidate_metrics,
            "scenario_metrics": candidate_scenarios,
            "resource": _resource_from_result(candidate),
        },
        "delta": overall_delta,
        "scenario_metrics": scenario_report,
        "guardrails": guardrails,
    }


def compare_result_files(
    baseline_path: str | Path,
    candidate_paths: Sequence[str | Path],
    *,
    output_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    split: str = "all",
    asset_dir: str | Path | None = None,
    max_hit_rate_regression: float = 0.01,
    max_mttc_regression: float = 0.50,
    asset_limit_bytes: int = DEFAULT_ASSET_LIMIT_BYTES,
) -> dict[str, object]:
    """Compare one baseline result against one or more local candidates."""

    if not candidate_paths:
        raise ValueError("at least one candidate result is required")
    baseline_file = Path(baseline_path)
    baseline_data = json.loads(baseline_file.read_text(encoding="utf-8"))
    if not isinstance(baseline_data, dict):
        raise ValueError(f"result is not a JSON object: {baseline_file}")
    manifest = load_manifest(manifest_path) if manifest_path is not None else None
    asset_bytes: int | None = None
    if asset_dir is not None:
        asset_path = validate_external_path(asset_dir)
        asset_bytes = directory_size_bytes(asset_path)

    comparisons: list[dict[str, object]] = []
    for path_value in candidate_paths:
        candidate_file = Path(path_value)
        candidate_data = json.loads(candidate_file.read_text(encoding="utf-8"))
        if not isinstance(candidate_data, dict):
            raise ValueError(f"result is not a JSON object: {candidate_file}")
        report = compare_result_data(
            baseline_data,
            candidate_data,
            baseline_label=baseline_file.stem,
            candidate_label=candidate_file.stem,
            manifest=manifest,
            split=split,
            max_hit_rate_regression=max_hit_rate_regression,
            max_mttc_regression=max_mttc_regression,
            asset_size_bytes=asset_bytes,
            asset_limit_bytes=asset_limit_bytes,
        )
        report["candidate"]["path"] = str(candidate_file)
        comparisons.append(report)
    output: dict[str, object] = {
        "schema_version": "qwen3-reranker-comparison-batch-v1",
        "baseline_path": str(baseline_file),
        "manifest_path": None if manifest_path is None else str(manifest_path),
        "comparisons": comparisons,
    }
    if output_path is not None:
        write_json(output, output_path)
    return output


class _InstrumentedAgent:
    """Collect per-turn timings while preserving the public Agent contract."""

    def __init__(self, agent: object) -> None:
        self.agent = agent
        self.latencies_ms: list[float] = []
        self.diagnostics: list[dict[str, object]] = []

    def reset(self, session_id: str, user_profile: dict) -> object:
        return self.agent.reset(session_id, user_profile)  # type: ignore[attr-defined]

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> object:
        started = time.perf_counter()
        try:
            return self.agent.respond(session_id, user_message, turn, top_k)  # type: ignore[attr-defined]
        finally:
            self.latencies_ms.append((time.perf_counter() - started) * 1000.0)
            diagnostics = getattr(self.agent, "last_diagnostics", None)
            if isinstance(diagnostics, Mapping):
                self.diagnostics.append(dict(diagnostics))


@contextmanager
def _offline_environment() -> Iterable[None]:
    """Force local model loading and redirect any runtime caches to PeeB."""

    values = {
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HOME": str(EXTERNAL_ROOT / "models" / "huggingface"),
        "TRANSFORMERS_CACHE": str(EXTERNAL_ROOT / "models" / "huggingface"),
        "HUGGINGFACE_HUB_CACHE": str(EXTERNAL_ROOT / "models" / "huggingface"),
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _select_samples(
    samples: Sequence[Mapping[str, object]],
    *,
    manifest: Mapping[str, object] | None,
    split: str,
    sample_limit: int | None,
) -> tuple[list[dict[str, object]], list[str]]:
    """Select the same frozen sample order for baseline and rerank runs."""

    if split not in {"all", *SPLIT_NAMES}:
        raise ValueError(f"unknown split: {split}")
    if sample_limit is not None and sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    rows = [dict(sample) for sample in samples]
    if manifest is None:
        # A named split has no meaning without its frozen manifest.  Requiring
        # one prevents the default validation command from accidentally
        # evaluating the reserved locked samples.
        if split != "all":
            raise ValueError("--manifest is required when --split is not all")
        selected = rows
        sample_ids = [str(sample.get("sample_id", "")) for sample in selected]
    else:
        selected_ids = manifest_sample_ids(manifest, split)
        by_id = {str(sample.get("sample_id", "")): sample for sample in rows}
        missing = [sample_id for sample_id in selected_ids if sample_id not in by_id]
        if missing:
            raise ValueError(
                f"public set does not contain {len(missing)} manifest sample IDs"
            )
        selected = [dict(by_id[sample_id]) for sample_id in selected_ids]
        sample_ids = list(selected_ids)
    if sample_limit is not None:
        selected = selected[:sample_limit]
        sample_ids = sample_ids[:sample_limit]
    return selected, sample_ids


def _validate_rerank_options(
    *,
    model_path: str | Path,
    device: str,
    batch_size: int,
    candidate_limit: int,
    timeout_seconds: float,
    fusion_weight: float,
) -> Path:
    checked_model_path = validate_external_path(model_path)
    if str(device).lower() not in {"mps", "cpu"}:
        raise ValueError("device must be mps or cpu")
    if int(batch_size) <= 0:
        raise ValueError("batch_size must be positive")
    if int(candidate_limit) <= 0 or int(candidate_limit) > 30:
        raise ValueError("candidate_limit must be between 1 and 30")
    if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not math.isfinite(float(fusion_weight)) or not 0 <= float(fusion_weight) <= 1:
        raise ValueError("fusion_weight must be between 0 and 1")
    return checked_model_path


def _asset_size_if_present(path: Path) -> tuple[int | None, bool]:
    """Measure a local model directory without making a missing path fatal."""

    if not path.exists():
        return None, False
    return directory_size_bytes(path), True


def _rerank_resource_report(
    instrumented: _InstrumentedAgent,
    *,
    wall_time_ms: float,
    initialization_ms: float,
    model_path: Path,
    sample_ids: Sequence[str],
    asset_size_bytes: int | None,
    asset_path_exists: bool,
) -> dict[str, object]:
    fallback_count = 0
    gate_skip_count = 0
    for diagnostics in instrumented.diagnostics:
        failures = diagnostics.get("model_failures")
        semantic_inputs = diagnostics.get("semantic_input_count")
        backend = diagnostics.get("model_backend")
        if isinstance(failures, (list, tuple)) and failures:
            fallback_count += 1
        elif _number(semantic_inputs) and _number(semantic_inputs) > 0 and not backend:
            fallback_count += 1
        if str(diagnostics.get("gate", "")) == "over_general":
            gate_skip_count += 1
    return {
        "wall_time_ms": round(wall_time_ms, 6),
        "initialization_time_ms": round(initialization_ms, 6),
        "cold_start_time_ms": (
            None
            if not instrumented.latencies_ms
            else round(instrumented.latencies_ms[0], 6)
        ),
        "latency_ms": summarize_latencies(
            instrumented.latencies_ms,
            wall_time_ms=wall_time_ms,
        ),
        "process_memory": process_memory_usage(),
        "external_asset_bytes": asset_size_bytes,
        "external_asset_path": str(model_path),
        "external_asset_path_exists": asset_path_exists,
        "fallback_count": fallback_count,
        "candidate_gate_skip_count": gate_skip_count,
        "sample_count": len(sample_ids),
    }


def run_reranked(
    catalog_path: str | Path,
    public_set_path: str | Path,
    output_path: str | Path,
    *,
    model_path: str | Path,
    revision: str | None = None,
    device: str = "mps",
    batch_size: int = 8,
    candidate_limit: int = 30,
    timeout_seconds: float = 15.0,
    fusion_weight: float = 1.0,
    manifest_path: str | Path | None = None,
    split: str = "validation",
    sample_limit: int | None = None,
    semantic_ranker: object | None = None,
) -> dict[str, object]:
    """Run the Qwen-configured Agent against one frozen experiment split.

    ``semantic_ranker`` is an explicit dependency-injection hook used only by
    tests and local adapter validation.  Production callers omit it, causing
    ``Agent`` to construct its Qwen adapter from the supplied ``AgentConfig``.
    The model path is always validated under PeeB and the whole evaluation is
    wrapped in offline environment variables.
    """

    checked_model_path = _validate_rerank_options(
        model_path=model_path,
        device=device,
        batch_size=batch_size,
        candidate_limit=candidate_limit,
        timeout_seconds=timeout_seconds,
        fusion_weight=fusion_weight,
    )
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evaluator.local_evaluator import catalog_index, evaluate, load_jsonl
    from starter.agent import Agent
    from starter.shopping_agent.config import AgentConfig

    manifest = load_manifest(manifest_path) if manifest_path is not None else None
    samples, sample_ids = _select_samples(
        load_jsonl(public_set_path),
        manifest=manifest,
        split=split,
        sample_limit=sample_limit,
    )
    asset_size_bytes, asset_path_exists = _asset_size_if_present(checked_model_path)
    config = AgentConfig(
        qwen_reranker_model_path=str(checked_model_path),
        qwen_reranker_revision=revision,
        qwen_reranker_device=str(device).lower(),
        qwen_reranker_batch_size=int(batch_size),
        qwen_reranker_candidate_limit=int(candidate_limit),
        qwen_reranker_timeout_seconds=float(timeout_seconds),
        qwen_reranker_fusion_weight=float(fusion_weight),
    )

    with _offline_environment():
        init_started = time.perf_counter()
        if semantic_ranker is None:
            agent = Agent(catalog_path, config=config)
        else:
            agent = Agent(catalog_path, config=config, semantic_ranker=semantic_ranker)
        initialization_ms = (time.perf_counter() - init_started) * 1000.0
        instrumented = _InstrumentedAgent(agent)
        run_started = time.perf_counter()
        catalog_ids, categories, products = catalog_index(catalog_path)
        result = evaluate(instrumented, samples, catalog_ids, categories, products)
        wall_time_ms = (time.perf_counter() - run_started) * 1000.0

    evaluator_metric_names = (
        "sample_count",
        "hit_rate_at_10",
        "mrr",
        "mttc",
        "efficiency",
        "recommended_technical_score",
        "scenario_metrics",
    )
    result["benchmark"] = {
        "mode": "qwen3_reranked",
        "network": "disabled",
        "model": "Qwen/Qwen3-Reranker-0.6B",
        "model_path": str(checked_model_path),
        "revision": revision,
        "device": str(device).lower(),
        "batch_size": int(batch_size),
        "candidate_limit": int(candidate_limit),
        "timeout_seconds": float(timeout_seconds),
        "fusion_weight": float(fusion_weight),
        "split": split,
        "sample_limit": sample_limit,
        "sample_ids": list(sample_ids),
        "evaluator_metrics": {name: result.get(name) for name in evaluator_metric_names},
        "resource": _rerank_resource_report(
            instrumented,
            wall_time_ms=wall_time_ms,
            initialization_ms=initialization_ms,
            model_path=checked_model_path,
            sample_ids=sample_ids,
            asset_size_bytes=asset_size_bytes,
            asset_path_exists=asset_path_exists,
        ),
    }
    write_json(result, output_path)
    return result


def run_deterministic_baseline(
    catalog_path: str | Path,
    public_set_path: str | Path,
    output_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    split: str = "all",
    asset_dir: str | Path | None = None,
    sample_limit: int | None = None,
) -> dict[str, object]:
    """Run the feature-only Agent with every optional model backend disabled."""

    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from evaluator.local_evaluator import (
        Agent,
        catalog_index,
        evaluate,
        load_jsonl,
    )
    from starter.shopping_agent.config import AgentConfig

    manifest = load_manifest(manifest_path) if manifest_path is not None else None
    samples, sample_ids = _select_samples(
        load_jsonl(public_set_path),
        manifest=manifest,
        split=split,
        sample_limit=sample_limit,
    )

    if asset_dir is not None:
        asset_path = validate_external_path(asset_dir)
        asset_bytes = directory_size_bytes(asset_path)
    else:
        asset_bytes = None

    init_started = time.perf_counter()
    # AgentConfig() is intentional: it prevents a user's API-key environment
    # from changing this command into a network/model benchmark.
    agent = Agent(catalog_path, config=AgentConfig())
    initialization_ms = (time.perf_counter() - init_started) * 1000.0
    instrumented = _InstrumentedAgent(agent)
    run_started = time.perf_counter()
    catalog_ids, categories, products = catalog_index(catalog_path)
    result = evaluate(instrumented, samples, catalog_ids, categories, products)
    wall_time_ms = (time.perf_counter() - run_started) * 1000.0
    result["benchmark"] = {
        "mode": "deterministic_feature_only",
        "network": "disabled",
        "model": None,
        "split": split,
        "sample_limit": sample_limit,
        "sample_ids": list(sample_ids),
        "initialization_time_ms": round(initialization_ms, 6),
        "resource": {
            "wall_time_ms": round(wall_time_ms, 6),
            "latency_ms": summarize_latencies(
                instrumented.latencies_ms,
                wall_time_ms=wall_time_ms,
            ),
            "process_memory": process_memory_usage(),
            "external_asset_bytes": asset_bytes,
        },
    }
    write_json(result, output_path)
    return result


def _default_output(root: Path, kind: str) -> Path:
    if kind == "manifest":
        return root / "manifests" / "qwen3-reranker-manifest.json"
    if kind == "baseline":
        return root / "results" / "feature-only-baseline.json"
    if kind == "rerank":
        return root / "results" / "qwen3-reranked.json"
    return root / "results" / "qwen3-reranker-comparison.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Qwen3 reranker experiment manifest, baseline, and comparison harness"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="create a frozen target-free split manifest")
    manifest.add_argument("--public-set", required=True, help="local public_set.jsonl path")
    manifest.add_argument("--output", help="manifest JSON output; defaults to the external artifact root")
    manifest.add_argument("--artifact-root", help="explicit artifact root when --output is omitted")
    manifest.add_argument("--seed", type=int, default=0)
    manifest.add_argument("--dev-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["dev"])
    manifest.add_argument("--validation-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["validation"])
    manifest.add_argument("--locked-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["locked"])

    baseline = subparsers.add_parser("baseline", help="run the offline deterministic feature-only evaluator")
    baseline.add_argument("--catalog", required=True, help="local catalog.jsonl path")
    baseline.add_argument("--public-set", required=True, help="local public_set.jsonl path")
    baseline.add_argument("--output", help="result JSON output; defaults to the external artifact root")
    baseline.add_argument("--artifact-root", help="explicit artifact root when --output is omitted")
    baseline.add_argument("--manifest", help="optional frozen manifest JSON")
    baseline.add_argument("--split", choices=("all", *SPLIT_NAMES), default="all")
    baseline.add_argument("--asset-dir", help="optional model/runtime asset directory under PeeB")
    baseline.add_argument("--sample-limit", type=int, help="optional deterministic subset size for smoke tests")

    rerank = subparsers.add_parser("rerank", help="run the Qwen3 reranker on a frozen non-locked split")
    rerank.add_argument("--catalog", required=True, help="local catalog.jsonl path")
    rerank.add_argument("--public-set", required=True, help="local public_set.jsonl path")
    rerank.add_argument("--manifest", help="frozen target-free manifest JSON")
    rerank.add_argument(
        "--split",
        choices=("all", *SPLIT_NAMES),
        default="validation",
        help="experiment split; locked must be explicitly selected",
    )
    rerank.add_argument("--sample-limit", type=int, help="optional deterministic subset size for smoke tests")
    rerank.add_argument("--output", help="result JSON output; defaults to the external artifact root")
    rerank.add_argument("--artifact-root", help="explicit artifact root when --output is omitted")
    rerank.add_argument("--model-path", required=True, help="local Qwen checkpoint directory under PeeB")
    rerank.add_argument("--revision", help="pinned local model revision")
    rerank.add_argument("--device", choices=("mps", "cpu"), default="mps")
    rerank.add_argument("--batch-size", type=int, default=8)
    rerank.add_argument("--candidate-limit", type=int, default=30)
    rerank.add_argument("--timeout-seconds", type=float, default=15.0)
    rerank.add_argument("--fusion-weight", type=float, default=1.0)

    compare = subparsers.add_parser("compare", help="compare one baseline to one or more result JSON files")
    compare.add_argument("--baseline", required=True, help="feature-only result JSON")
    compare.add_argument("--reranked", required=True, action="append", help="reranked result JSON; repeatable")
    compare.add_argument("--output", help="comparison JSON output; defaults to the external artifact root")
    compare.add_argument("--artifact-root", help="explicit artifact root when --output is omitted")
    compare.add_argument("--manifest", help="optional frozen manifest JSON")
    compare.add_argument("--split", choices=("all", *SPLIT_NAMES), default="all")
    compare.add_argument("--asset-dir", help="optional model/runtime asset directory under PeeB")
    compare.add_argument("--max-hit-rate-regression", type=float, default=0.01)
    compare.add_argument("--max-mttc-regression", type=float, default=0.50)
    compare.add_argument("--asset-limit-bytes", type=int, default=DEFAULT_ASSET_LIMIT_BYTES)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact_root = resolve_artifact_root(getattr(args, "artifact_root", None))
    output_value = getattr(args, "output", None)
    output = Path(output_value).expanduser() if output_value else _default_output(artifact_root, args.command)

    if args.command == "manifest":
        value = generate_manifest(
            args.public_set,
            seed=args.seed,
            dev_ratio=args.dev_ratio,
            validation_ratio=args.validation_ratio,
            locked_ratio=args.locked_ratio,
        )
        write_json(value, output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "seed": value["seed"],
                    "split_counts": value["split_scenario_counts"],
                },
                indent=2,
            )
        )
        return 0
    if args.command == "baseline":
        result = run_deterministic_baseline(
            args.catalog,
            args.public_set,
            output,
            manifest_path=args.manifest,
            split=args.split,
            asset_dir=args.asset_dir,
            sample_limit=args.sample_limit,
        )
        summary = {
            key: result.get(key)
            for key in (
                "sample_count",
                "hit_rate_at_10",
                "mrr",
                "mttc",
                "recommended_technical_score",
            )
        }
        summary["output"] = str(output)
        print(json.dumps(summary, indent=2))
        return 0
    if args.command == "rerank":
        result = run_reranked(
            args.catalog,
            args.public_set,
            output,
            model_path=args.model_path,
            revision=args.revision,
            device=args.device,
            batch_size=args.batch_size,
            candidate_limit=args.candidate_limit,
            timeout_seconds=args.timeout_seconds,
            fusion_weight=args.fusion_weight,
            manifest_path=args.manifest,
            split=args.split,
            sample_limit=args.sample_limit,
        )
        summary = {
            key: result.get(key)
            for key in (
                "sample_count",
                "hit_rate_at_10",
                "mrr",
                "mttc",
                "recommended_technical_score",
            )
        }
        summary["output"] = str(output)
        summary["benchmark"] = result.get("benchmark")
        print(json.dumps(summary, indent=2))
        return 0
    report = compare_result_files(
        args.baseline,
        args.reranked,
        output_path=output,
        manifest_path=args.manifest,
        split=args.split,
        asset_dir=args.asset_dir,
        max_hit_rate_regression=args.max_hit_rate_regression,
        max_mttc_regression=args.max_mttc_regression,
        asset_limit_bytes=args.asset_limit_bytes,
    )
    print(json.dumps({"output": str(output), "comparisons": len(report["comparisons"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
