"""Tunable contest-agent behaviour. Ablations change this object, not the evaluator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContestConfig:
    """Khanna-style skeleton with an optional classmate-style precision gate."""

    use_category_lock: bool = True
    use_constraint_scoring: bool = True
    use_popularity_prior: bool = True
    use_profile_prior: bool = True
    profile_cold_start_only: bool = True
    pad_to_top_k: bool = True
    use_observed_fallback: bool = True
    ask_other: bool = True

    # 0 = always recommend (Khanna). >0 withhold recs while the working pool
    # is larger than this (classmate over-generality cutoff).
    gate_size: int = 0
    # Hard AND-filter before gating. If the conjunction empties the pool the
    # agent falls back to the soft pool and does not withhold.
    hard_filter: bool = False
    # Hits before the override message cannot score; withholding is optional.
    gate_before_override: bool = False
    # After override, ignore min_slots/dump shortcuts and wait for gate_size.
    # Recovers Override MRR (classmate 0.971 vs our 0.938) at some MTTC cost.
    strict_override_gate: bool = False
    # FlashRank/TinyBERT on the hard pool only. 0 disables; missing model
    # leaves lexical/popularity ranking unchanged.
    w_rerank: float = 0.0
    rerank_pool_limit: int = 80
    # If >0, still recommend when this many slots are known and the working
    # pool is at most evidence_pool_cap, even if it is larger than gate_size.
    # Cuts MTTC on sessions that already have a 3-slot conjunction but sit
    # at pool 6–20 waiting for "no additional preference".
    min_slots_to_recommend: int = 0
    evidence_pool_cap: int = 20
    # D2D-style TOI: if >0, undo an early recommend (pool still larger
    # than gate_size) when the popularity gap between the top two working
    # items is below this margin. 0 disables.
    overlap_margin: float = 0.0
    # Closer disclosed budget wins among hard-pool clones. 0 disables.
    w_price: float = 0.0
    # Once this many slots are known, another ``other`` almost always
    # returns no additional preference (intent cards hold 2 hard + 2 soft).
    # Recommend if the working pool is at most dump_pool_cap. 0 disables.
    dump_slots: int = 0
    dump_pool_cap: int = 40
    # If >0, recommend when the working pool is this small and at least one
    # disclosed slot has a non-generic token (not cotton/color/imported).
    # Generic-only conjunctions still wait for gate / min_slots.
    distinctive_early_cap: int = 0
    # MiniLM cosine on the hard pool only. 0 disables; missing weights fall
    # back to the lexical/popularity score. Keep <=0.1 — classmate w=0.45 hurt.
    w_dense: float = 0.0
    dense_pool_limit: int = 80
    # After dense/rerank, keep the popularity top-N as the head of the list
    # (reordered by blended score). 0 disables. Floor=10 swapped holdout
    # misses; keep off unless holdout+public both improve.
    dense_pop_floor: int = 0
    # Union the popularity top-N with the blended top-N, then RRF-sort.
    # Holdout Hit 0.975→0.97; keep off.
    dense_rrf_k: int = 0
    # Skip MiniLM when every disclosed slot is a catalog-generic token
    # (cotton/imported/color). Saves pop-rank-8 generic misses without
    # blocking distinctive promotions like "rubber sole".
    dense_skip_generic: bool = False
    # If >0, still run MiniLM on generic slots when the working pool is
    # this small. 0 keeps skip-generic absolute. Pool 20 / pop-8 misses
    # must stay above this cap.
    dense_generic_cap: int = 0
    # Skip MiniLM when every hard-pool item has the same exact-line score.
    # True clones share feature bullets; extra tiny-pool cosine then dethrones
    # the popularity leader. 0/False keeps MiniLM. Distinctive line variance
    # still runs MiniLM (rubber-sole / 0090).
    dense_skip_field_flat: bool = False
    # Extra MiniLM weight when 2..dense_tiny_cap. 0 disables.
    w_dense_tiny: float = 0.0
    dense_tiny_cap: int = 6
    # Extra MiniLM also on larger distinctive pools when the top-two
    # popularity gap is below this margin. 0 disables the near-tie path.
    dense_tie_margin: float = 0.0
    dense_tie_cap: int = 20
    # IDF of distinctive disclosed tokens inside the hard pool only.
    # 0 disables. Small weight: popularity stays the main sort key.
    w_idf: float = 0.0
    idf_pool_limit: int = 40
    # 1.0 if this product uniquely holds a distinctive disclosed token
    # (df==1 in the hard pool). Different from smoothed IDF. 0 disables.
    w_exclusive: float = 0.0
    # Okapi BM25 on distinctive slot tokens after AND. IDF is catalog-wide
    # (pool df is ~N after conjunction). 0 disables.
    w_bm25: float = 0.0
    bm25_pool_limit: int = 40
    # Hard-pool title uniqueness: share of non-chrome title tokens that appear
    # in only one pool title. Not constraint coverage and not query IDF.
    w_uniq: float = 0.0
    uniq_pool_limit: int = 40
    # Exact feature-bullet / details-line match of disclosed slots. 0 disables.
    w_field: float = 0.0
    field_pool_limit: int = 40
    # Distinctive disclosed slot as an exact title substring. Not token
    # coverage (rejected w_title). Skip cotton/color/imported slots. 0 disables.
    w_phrase: float = 0.0
    phrase_pool_limit: int = 40

    w_constraint: float = 2.6
    w_lexical: float = 1.0
    w_popularity: float = 0.55
    w_profile: float = 1.0
    # Title coverage of disclosed constraints. 0 keeps popularity-first
    # ranking; applied only when the working pool is at most title_pool_limit
    # so a large no-preference dump cannot drop a popular target from top-10.
    w_title: float = 0.0
    title_pool_limit: int = 24
    override_decay: float = 0.5
    observed_boost: float = 0.45
    min_candidates: int = 40
    global_fallback_limit: int = 400


KHANNA = ContestConfig(gate_size=0, hard_filter=False, gate_before_override=False)
CLASSMATE = ContestConfig(gate_size=5, hard_filter=True, gate_before_override=True)
HYBRID = ContestConfig(gate_size=8, hard_filter=True, gate_before_override=False)
# Public scoring default: classmate gate, verbatim conjunction, no global pad.
PUBLIC = ContestConfig(
    gate_size=5,
    hard_filter=True,
    gate_before_override=True,
    pad_to_top_k=False,
    w_popularity=1.0,
    w_constraint=0.35,
    w_lexical=0.55,
    w_profile=0.08,
    # Public-set ablation: token/phrase title bonus moved ~20 clone
    # sessions the wrong way (0.942 -> 0.930/0.940 MRR). Keep off.
    w_title=0.0,
    title_pool_limit=24,
    min_slots_to_recommend=3,
    evidence_pool_cap=20,
    dump_slots=4,
    dump_pool_cap=80,
    # Distinctive early-rank cap=10: public 0.953914 (MTTC 2.505) but
    # holdout 0.88453 / MRR 0.758 < 0.8888. Keep off.
    distinctive_early_cap=0,
    # MiniLM cosine on hard pools of size 2..80. Missing weights → 0.
    w_dense=0.1,
    dense_pool_limit=80,
    dense_pop_floor=0,
    dense_rrf_k=0,
    dense_skip_generic=True,
    dense_generic_cap=0,
    # Tiny-pool MiniLM extra weight. generic_cap=6 + tiny=0.25: holdout
    # 0.8920 but public MRR 0.9467→0.915. Distinctive-only tiny=0.12:
    # public 0.951914 / holdout 0.890778 Hit 0.980. Keep distinctive-only.
    w_dense_tiny=0.12,
    dense_tiny_cap=6,
    # Near-tie MiniLM extra (margin=0.04, cap=20): public 0.951677,
    # holdout 0.890599 not > 0.8908. Keep off.
    dense_tie_margin=0.0,
    dense_tie_cap=20,
    # Skip MiniLM on exact-line ties: public 0.955864 / holdout 0.893543
    # rank1 142→139. Keep off.
    dense_skip_field_flat=False,
    # Hard-pool IDF trial w=0.15: public Hit 1.0 / 0.953414 unchanged;
    # holdout Hit 0.980 / 0.888778 (not strictly > 0.8888). Keep off.
    w_idf=0.0,
    idf_pool_limit=40,
    # Exclusive df=1 trial w=0.25: public 0.953414 / holdout 0.888778, not > 0.8888.
    w_exclusive=0.0,
    # Hard-pool BM25 w=0.12: public 0.950489 Hit 1.0, holdout Hit 0.97 / 0.88624
    # (extra misses 0067/0106). w=0.20 dropped public Hit and 0090. Keep off.
    w_bm25=0.0,
    bm25_pool_limit=40,
    # Title uniqueness w=0.15: public 0.946664 Hit 1.0, holdout Hit 0.97 /
    # 0.886084 (dropped 0067/0090). Keep off.
    w_uniq=0.0,
    uniq_pool_limit=40,
    # Exact feature/details line. w=0.15: public 0.956025 / holdout 0.894705.
    # w=0.25: public 0.956275 / holdout 0.895843.
    # w=0.35: public 0.9549 Hit 1.0 (MRR 0.95625→0.9517) / holdout 0.897918
    # Hit 0.980 rank1 144. Promote on holdout gate; public Hit held.
    w_field=0.35,
    field_pool_limit=40,
    # Distinctive title phrase w=0.15: public 0.9549 Hit 1.0 (unchanged) /
    # holdout 0.898118 Hit 0.980. Promote; skip cotton/color/imported slots.
    w_phrase=0.15,
    phrase_pool_limit=40,
)
