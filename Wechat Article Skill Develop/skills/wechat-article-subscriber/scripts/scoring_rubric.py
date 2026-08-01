"""Validated five-dimension article scoring and ad heuristics."""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping


SCORING_DIMENSIONS = {
    "技术深度": {"weight": 0.30, "description": "技术细节、原创方案与推导"},
    "信息新颖度": {"weight": 0.20, "description": "独家程度、时效性与增量信息"},
    "分析深度与独立观点": {"weight": 0.25, "description": "独立判断、批判分析与趋势推演"},
    "实用参考价值": {"weight": 0.15, "description": "可复用方法、行动建议与决策价值"},
    "内容质量与可信度": {"weight": 0.10, "description": "引用、来源、事实观点区分"},
}

AD_TITLE_PATTERNS = [
    re.compile(r"^(推广|广告|赞助|特约)\s*[|｜：:]\s*"),
    re.compile(r"[|｜：:]\s*(推广|广告|赞助|特约)\s*$"),
    re.compile(r"^[【\[(](推广|广告|赞助|特约)[】\])]"),
]
AD_DISCLOSURES = (
    "本文为推广",
    "本文为广告",
    "商业合作",
    "赞助内容",
    "广告内容",
)


def validate_scores(scores: Mapping[str, float]) -> dict[str, float]:
    if set(scores) != set(SCORING_DIMENSIONS):
        missing = sorted(set(SCORING_DIMENSIONS) - set(scores))
        extra = sorted(set(scores) - set(SCORING_DIMENSIONS))
        raise ValueError(f"all five dimensions are required; missing={missing}, extra={extra}")
    validated: dict[str, float] = {}
    for name in SCORING_DIMENSIONS:
        value = scores[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        if not 1 <= float(value) <= 10:
            raise ValueError(f"{name} must be between 1 and 10")
        validated[name] = float(value)
    return validated


def calculate_score(scores: Mapping[str, float]) -> float:
    validated = validate_scores(scores)
    total = sum(
        Decimal(str(validated[name])) * Decimal(str(details["weight"]))
        for name, details in SCORING_DIMENSIONS.items()
    )
    return float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def validate_total_score(score: float) -> float:
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("score must be numeric")
    value = float(score)
    if not 1 <= value <= 10:
        raise ValueError("score must be between 1 and 10")
    return round(value, 1)


def format_rationale(scores: Mapping[str, float]) -> str:
    validated = validate_scores(scores)
    return " | ".join(f"{name} {validated[name]:g}/10" for name in SCORING_DIMENSIONS)


def is_advertisement(title: str = "", content: str = "") -> bool:
    normalized_title = title.strip()
    if any(pattern.search(normalized_title) for pattern in AD_TITLE_PATTERNS):
        return True
    prefix = content[:800]
    return any(disclosure in prefix for disclosure in AD_DISCLOSURES)


def should_sync(score: float, minimum: float) -> bool:
    return validate_total_score(score) >= validate_total_score(minimum)
