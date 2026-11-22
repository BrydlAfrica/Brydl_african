"""Quality standards, scoring models, and defect taxonomy for Brydl Data Labeling.

Provides pure helpers for:
1. Standard multi-dimensional QA rubrics (Accuracy, Linguistic/Dialect Authenticity,
   Instruction Adherence, Formatting) and composite weighted score computation.
2. Defect taxonomy categories, severities, and validation.
3. Annotator Quality Tier categorization (Gold 95%+, Silver 90-94%, Needs Attention 80-89%, Probation <80%).
4. Standard QA feedback macros for African NLP & multimodal annotation.
5. In-app and CSV quality reports and metrics aggregation.
"""

from decimal import Decimal
import json
from typing import Any

# Standard 4-dimension evaluation rubric for all annotation tasks
STANDARD_RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "accuracy",
    "linguistic_quality",
    "instruction_adherence",
    "formatting",
)

RUBRIC_DIMENSION_META: dict[str, dict[str, Any]] = {
    "accuracy": {
        "label": "Accuracy & Factual Correctness",
        "description": "Factual precision, entity alignment, correct label identification.",
        "weight": 0.35,
    },
    "linguistic_quality": {
        "label": "Linguistic Quality & Dialect Authenticity",
        "description": "Diacritic marks (ẹ, ọ, ṣ), tone accuracy, natural Nigerian dialect fluency.",
        "weight": 0.30,
    },
    "instruction_adherence": {
        "label": "Instruction Following & Completeness",
        "description": "Adherence to project guidelines, boundary rules, length and detail constraints.",
        "weight": 0.20,
    },
    "formatting": {
        "label": "Formatting & Output Syntax",
        "description": "Clean markup, valid JSON/tags, zero stray artifacts.",
        "weight": 0.15,
    },
}

# Defect taxonomy for error tagging
DEFECT_CATEGORIES: dict[str, dict[str, str]] = {
    "diacritics_tone": {
        "label": "Diacritics & Tone Omission",
        "description": "Missing, improper, or inverted diacritic marks on African language terms.",
        "track": "Linguistic",
    },
    "translation_meaning": {
        "label": "Translation & Meaning Shift",
        "description": "Distorted semantic meaning, unnatural literalism, or mistranslation.",
        "track": "Accuracy",
    },
    "guideline_noncompliance": {
        "label": "Guideline Non-Compliance",
        "description": "Violated project-specific labeling rules or edge-case instructions.",
        "track": "Compliance",
    },
    "hallucination_factual": {
        "label": "Hallucination & Factual Inaccuracy",
        "description": "Fabricated facts, wrong named entities, or unsupported assertions.",
        "track": "Accuracy",
    },
    "formatting_syntax": {
        "label": "Formatting & Syntax Glitch",
        "description": "Malformed JSON structure, broken tags, or invalid schema formatting.",
        "track": "Syntax",
    },
    "incomplete_work": {
        "label": "Incomplete or Truncated Response",
        "description": "Skipped segments, superficial responses, or cut-off annotations.",
        "track": "Completeness",
    },
}

DEFECT_SEVERITIES: dict[str, dict[str, Any]] = {
    "minor": {"label": "Minor", "penalty": 5.0, "badge": "secondary"},
    "major": {"label": "Major", "penalty": 15.0, "badge": "warning text-dark"},
    "critical": {"label": "Critical", "penalty": 30.0, "badge": "danger"},
}

APPEAL_STATUSES: tuple[str, ...] = ("none", "pending", "granted", "denied")

QUALITY_TIERS: dict[str, dict[str, Any]] = {
    "gold": {
        "name": "Gold Standard (95%+)",
        "min_score": 95.0,
        "badge_class": "bg-warning text-dark",
        "icon": "bi-trophy-fill",
        "description": "Top-tier specialist accuracy. Eligible for Specialist Tracks and Pod Leadership.",
    },
    "silver": {
        "name": "Silver Standard (90%–94%)",
        "min_score": 90.0,
        "badge_class": "bg-success",
        "icon": "bi-award-fill",
        "description": "High-precision enterprise quality meeting standard production acceptance.",
    },
    "needs_attention": {
        "name": "Needs Attention (80%–89%)",
        "min_score": 80.0,
        "badge_class": "bg-info text-dark",
        "icon": "bi-exclamation-circle-fill",
        "description": "Below target 90% quality threshold. Coaching feedback required.",
    },
    "probation": {
        "name": "Probationary (<80%)",
        "min_score": 0.0,
        "badge_class": "bg-danger",
        "icon": "bi-shield-exclamation",
        "description": "Significant defect rate. Calibration review and remedial screening required.",
    },
}

# Standard QA macros for rapid high-quality feedback
FEEDBACK_MACROS: list[dict[str, str]] = [
    {
        "id": "yoruba_diacritics",
        "title": "Yoruba Diacritics & Tone",
        "category": "diacritics_tone",
        "text": "Please ensure all Yoruba subdots (ẹ, ọ, ṣ) and tone marks (á, à, ā) are correctly applied according to official standard Yoruba orthography.",
    },
    {
        "id": "hausa_hooked",
        "title": "Hausa Hooked Consonants",
        "category": "diacritics_tone",
        "text": "Remember to use correct Hausa hooked letters (ɓ, ɗ, ƙ, ƴ, 'R) instead of plain latin consonants where required.",
    },
    {
        "id": "igbo_vowel_harmony",
        "title": "Igbo Dot Below & Vowel Harmony",
        "category": "diacritics_tone",
        "text": "Review Igbo vowel harmony and ensure dots below (ị, ọ, ụ, ṅ) are properly preserved on all applicable stems.",
    },
    {
        "id": "literal_translation",
        "title": "Avoid Literal Translation",
        "category": "translation_meaning",
        "text": "The submitted translation was too literal. Please maintain idiomatic Nigerian natural phrasing while preserving precise source intent.",
    },
    {
        "id": "guideline_length",
        "title": "Instruction Compliance",
        "category": "guideline_noncompliance",
        "text": "The response did not fully comply with the project instructions. Please re-read the task guidelines before resubmitting.",
    },
    {
        "id": "json_format",
        "title": "Valid JSON / Schema Syntax",
        "category": "formatting_syntax",
        "text": "Your output contained syntax errors or invalid JSON tags. Ensure responses validate against the target schema.",
    },
]


def compute_weighted_qa_score(rubric_scores: dict[str, float | int]) -> float:
    """Calculate the overall weighted QA score (0.0 to 100.0) from rubric dimensions.

    Args:
        rubric_scores: Dictionary of dimension keys to score values (0-100).

    Returns:
        Weighted composite score rounded to 1 decimal place.
    """
    if not rubric_scores:
        return 100.0

    total_weight = 0.0
    weighted_sum = 0.0

    for dim, meta in RUBRIC_DIMENSION_META.items():
        if dim in rubric_scores:
            val = float(rubric_scores[dim])
            val = max(0.0, min(100.0, val))
            weight = meta["weight"]
            weighted_sum += val * weight
            total_weight += weight

    if total_weight <= 0.0:
        return 100.0

    score = weighted_sum / total_weight
    return round(score, 1)


def classify_quality_tier(avg_score: float | None, total_reviewed: int = 0) -> dict[str, Any]:
    """Classify an annotator into a quality tier based on their average QA score.

    Args:
        avg_score: Average QA score (0.0 - 100.0) or None if no reviews.
        total_reviewed: Total number of approved/rejected submissions.

    Returns:
        Dict with tier key, name, badge_class, icon, and description.
    """
    if avg_score is None or total_reviewed == 0:
        return {
            "tier_key": "unranked",
            "name": "Unranked (No Data)",
            "badge_class": "bg-secondary",
            "icon": "bi-dash-circle",
            "description": "No evaluated submissions on record yet.",
        }

    score = float(avg_score)
    if score >= 95.0:
        tier_key = "gold"
    elif score >= 90.0:
        tier_key = "silver"
    elif score >= 80.0:
        tier_key = "needs_attention"
    else:
        tier_key = "probation"

    data = QUALITY_TIERS[tier_key].copy()
    data["tier_key"] = tier_key
    return data


def parse_qa_rubric_data(raw_data: str | None) -> dict[str, float]:
    """Safely parse stored JSON rubric data into dimension float scores."""
    if not raw_data:
        return {}
    try:
        parsed = json.loads(raw_data)
        if isinstance(parsed, dict):
            res = {}
            for k, v in parsed.items():
                if k in RUBRIC_DIMENSION_META and isinstance(v, (int, float)):
                    res[k] = float(v)
            return res
    except (ValueError, TypeError):
        pass
    return {}


def parse_error_tags(raw_data: str | None) -> list[dict[str, str]]:
    """Safely parse stored JSON error tags."""
    if not raw_data:
        return []
    try:
        parsed = json.loads(raw_data)
        if isinstance(parsed, list):
            res = []
            for item in parsed:
                if isinstance(item, dict) and "category" in item:
                    cat = str(item["category"])
                    sev = str(item.get("severity", "minor"))
                    note = str(item.get("note", ""))
                    if cat in DEFECT_CATEGORIES and sev in DEFECT_SEVERITIES:
                        res.append({"category": cat, "severity": sev, "note": note})
            return res
    except (ValueError, TypeError):
        pass
    return []


def serialize_qa_rubric_data(scores: dict[str, float | int]) -> str:
    """Serialize dimension scores to clean JSON string."""
    clean = {}
    for k, v in scores.items():
        if k in RUBRIC_DIMENSION_META:
            clean[k] = round(float(v), 1)
    return json.dumps(clean)


def serialize_error_tags(tags: list[dict[str, Any]]) -> str:
    """Serialize defect tags to clean JSON string."""
    clean = []
    for t in tags:
        if isinstance(t, dict) and t.get("category") in DEFECT_CATEGORIES:
            clean.append({
                "category": str(t["category"]),
                "severity": str(t.get("severity", "minor")),
                "note": str(t.get("note", "")).strip()[:200],
            })
    return json.dumps(clean)
