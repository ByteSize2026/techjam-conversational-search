"""Normalisation helpers aligned with the official evaluator's catalog fields."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

MATERIALS = (
    "cotton",
    "polyester",
    "nylon",
    "leather",
    "wool",
    "spandex",
    "silk",
    "rayon",
    "fabric",
)
COLORS = (
    "black",
    "white",
    "blue",
    "red",
    "pink",
    "green",
    "brown",
    "gray",
    "grey",
    "purple",
    "yellow",
    "orange",
)
SEARCH_FIELDS = ("title", "features", "details", "description", "categories", "store")
GENERIC_CATEGORY = {"clothing", "clothing shoes & jewelry", "clothing, shoes & jewelry"}
TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
PRICE_RE = re.compile(r"\$?\s*(\d+(?:\.\d+)?)")
MATERIAL_RE = re.compile(r"\b(" + "|".join(MATERIALS) + r")\b", re.I)
COLOR_RE = re.compile(r"\b(" + "|".join(COLORS) + r")\b", re.I)
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "the", "this", "to",
    "with", "you", "looking",
}
CHROME = {
    "hi", "hey", "hello", "please", "thanks", "thank", "ok", "okay", "just",
    "still", "really", "want", "need", "looking", "shopping", "browsing",
    "preference", "additional", "particular", "specific",
}
ALLOWED_ATTRIBUTES = {
    "category", "material", "color", "size", "style", "brand",
    "budget", "feature", "use_case", "other",
}


def normalise(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def terms(value: object, *, limit: int = 64) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in TOKEN_RE.findall(normalise(value)):
        if len(raw) < 2 or raw in STOPWORDS or raw in seen:
            continue
        seen.add(raw)
        found.append(raw)
        if len(found) >= limit:
            break
    return found


def parse_price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = PRICE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def coarse_category(values: Sequence[object] | None) -> str:
    """Same leaf-pair used by evaluator.initial_message / coarse_category."""

    cleaned: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part and part.lower() not in GENERIC_CATEGORY:
                cleaned.append(part)
    return " ".join(cleaned[-2:]) if cleaned else "clothing item"


def product_search_text(product: Mapping[str, object]) -> str:
    """Flatten a catalog row the same way the simulator mines constraints.

    Dict details become ``key: value`` so ``color: black`` and feature
    bullets with punctuation survive as substrings of the corpus.
    """

    parts: list[str] = []
    for field in SEARCH_FIELDS:
        value = product.get(field)
        if isinstance(value, Mapping):
            for key, item in value.items():
                if item not in (None, "", []):
                    parts.append(f"{key}: {item}")
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(item) for item in value if item not in (None, ""))
        elif value not in (None, ""):
            parts.append(str(value))
    return normalise(" ".join(parts))


def searchable_blob(product: Mapping[str, object]) -> str:
    return product_search_text(product)


def fold_punct(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[,;:/\"']", " ", str(text).lower())).strip()


def classify_constraint(value: str) -> str:
    lowered = value.lower()
    if "budget" in lowered or re.search(r"(?:\$|<=|under)\s*\d", lowered):
        return "budget"
    if any(word in lowered for word in MATERIALS):
        return "material"
    if any(word in lowered for word in ("color", "black", "white", "blue", "red", "pink", "green")):
        return "color"
    if any(word in lowered for word in ("size", "sizing", "width", "wide", "narrow")):
        return "size"
    if any(word in lowered for word in ("department", "style", "fit", "sleeve", "neck")):
        return "style"
    if any(word in lowered for word in ("hiking", "running", "gym", "winter", "outdoor", "work")):
        return "use_case"
    return "feature"


def match_needle(text: str) -> str:
    return fold_punct(text)


def constraint_matches(constraint: str, corpus: str, price: float | None = None) -> bool:
    """Hard-match a disclosed string against a product corpus.

    Constraints are copied from the target's own features/details, so the
    source product must survive conjunction.  Matching uses the same
    whitespace-collapsed corpus plus a punctuation-folded fallback.
    """

    raw = str(constraint or "").strip()
    norm = normalise(raw)
    if not norm:
        return True
    color_word = ""
    if norm.startswith("color:"):
        color_word = norm.split(":", 1)[1].strip()
    elif classify_constraint(raw) == "color":
        for word in COLORS:
            if re.search(rf"\b{re.escape(word)}\b", norm):
                color_word = word
                break
    if color_word:
        if color_word in {"gray", "grey"}:
            return bool(re.search(r"\bgray\b", corpus) or re.search(r"\bgrey\b", corpus))
        return bool(re.search(rf"\b{re.escape(color_word)}\b", corpus))
    if "budget" in norm or re.search(r"\$\s*\d", norm):
        target = parse_price(norm)
        if target is not None:
            if price is None:
                return False
            return abs(float(price) - target) <= max(2.0, 0.05 * target)
    if classify_constraint(raw) == "material" and len(norm) <= 12 and " " not in norm:
        return bool(re.search(rf"\b{re.escape(norm)}\b", corpus))
    if norm in corpus:
        return True
    pieces = [normalise(part) for part in re.split(r";", raw) if normalise(part)]
    if len(pieces) > 1 and all(piece in corpus for piece in pieces):
        return True
    folded = fold_punct(raw)
    if folded and len(folded) >= 8:
        return folded in fold_punct(corpus)
    return False
