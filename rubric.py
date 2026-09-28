"""Rubric helpers (RLHF Phase 5): dimension lists and score validation.

Preference projects may define named rubric dimensions (one per line,
configured on the project form). Annotators then rate BOTH responses
on every dimension with native range sliders — no client-side logic —
and the scores are stored inside the preference output document:

    {"kind": "preference", ..., "rubric":
        {"accuracy": {"a": 4, "b": 3}, ...}}

This module is PURE (no Flask, no database): the same validation runs
at project save, at submission build and at display time, and every
function degrades gracefully on malformed input.
"""

# Canonical response keys a rubric score pair covers.
_RUBRIC_SIDES = ("a", "b")


def parse_dimensions(
    raw: str,
    max_dimensions: int = 6,
    max_name: int = 40,
) -> tuple[list[str], list[str]]:
    """Parse a newline-separated dimension list from the project form.

    Args:
        raw: The textarea value (one dimension per line).
        max_dimensions: Upper bound on the number of dimensions.
        max_name: Upper bound on one dimension's character length.

    Returns:
        A (dimensions, problems) tuple. dimensions is the cleaned,
        order-preserved list; problems lists human-readable reasons
        the value must be rejected (empty list = value is fine; an
        empty raw simply yields no dimensions — the rubric is off).
    """
    problems: list[str] = []
    dims: list[str] = []
    seen: set[str] = set()
    for line in (raw or "").splitlines():
        name = line.strip()
        if not name:
            continue
        if len(name) > max_name:
            problems.append(
                f"Dimension \u201c{name[: max_name + 1]}\u2026\u201d is"
                f" longer than {max_name} characters."
            )
            continue
        key = name.casefold()
        if key in seen:
            problems.append(
                f"Dimension \u201c{name}\u201d is listed more than once."
            )
            continue
        seen.add(key)
        dims.append(name)
    if len(dims) > max_dimensions:
        problems.append(
            f"A rubric can have at most {max_dimensions} dimensions"
            f" (got {len(dims)})."
        )
    return dims, problems


def rubric_scores_valid(
    rubric: object,
    dims: list[str],
    min_score: int = 1,
    max_score: int = 5,
) -> bool:
    """Check a stored rubric document against a project's dimensions.

    Args:
        rubric: The parsed ``rubric`` value from an output document.
        dims: The project's rubric dimension names.
        min_score: Valid inclusive lower bound for one score.
        max_score: Valid inclusive upper bound for one score.

    Returns:
        True only when rubric is a dict with exactly the project's
        dimensions, each mapping to exactly {"a": int, "b": int} with
        both scores inside [min_score, max_score]. Booleans do not
        count as ints. An empty dims list is valid only with a
        missing/empty rubric.
    """
    if not dims:
        return rubric in (None, {},)
    if not isinstance(rubric, dict):
        return False
    if set(rubric.keys()) != set(dims):
        return False
    for pair in rubric.values():
        if not isinstance(pair, dict):
            return False
        if set(pair.keys()) != set(_RUBRIC_SIDES):
            return False
        for value in pair.values():
            if isinstance(value, bool) or not isinstance(value, int):
                return False
            if not min_score <= value <= max_score:
                return False
    return True
