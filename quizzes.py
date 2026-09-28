"""Qualification quiz engine (pure, engine-agnostic).

A quiz gates a project: annotators must pass its multiple-choice
questions before claim/assignment hands them that project's work.
This module owns the three pieces that must be identical everywhere:
parsing the admin's paste-authored question text into a canonical
payload, grading a submitted answer sheet against that payload, and
the deterministic per-annotator display order (blake2b, same
contract as the RLHF A/B flip) so a browser refresh never reshuffles
a half-answered attempt.

Correct-answer flags live ONLY in the stored payload; nothing here
ever puts them into form choices or templates, so the key can never
leak through the render path.
"""

import hashlib
import json


QUIZ_MAX_QUESTIONS = 30
QUESTION_MAX_CHARS = 500
OPTION_MAX_CHARS = 200
QUIZ_ATTEMPT_STATUSES = ("passed", "failed", "exempt")
QUIZ_STATUSES = ("draft", "active", "retired")


def parse_quiz_text(raw: str) -> tuple[list[dict] | None, list[str]]:
    """Parse the paste-authored quiz text into canonical questions.

    Format (line-based, one question block after another)::

        Q: What does the "dog" label mean for this project?
        - Any four-legged animal
        * A dog is visible in the image
        - Only cartoon dogs

    ``Q:`` starts a question, ``- `` a wrong option, ``* `` the
    correct option (exactly one per question).

    Args:
        raw: The textarea contents.

    Returns:
        (questions, errors). questions is None when parsing failed;
        otherwise a list of {"text", "options": [{"text", "correct"}]}
        in canonical order. errors is a list of human-readable
        messages (empty on success).
    """
    errors: list[str] = []
    questions: list[dict] = []
    current: dict | None = None
    for lineno, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("q:"):
            if current is not None:
                questions.append(current)
            text = line[2:].strip()
            if not text:
                errors.append(f"Line {lineno}: question text is empty.")
                current = None
                continue
            if len(text) > QUESTION_MAX_CHARS:
                errors.append(
                    f"Line {lineno}: question text exceeds"
                    f" {QUESTION_MAX_CHARS} characters."
                )
                current = None
                continue
            current = {"text": text, "options": []}
            continue
        if lowered.startswith(("- ", "* ")):
            if current is None:
                errors.append(
                    f"Line {lineno}: option appears before its"
                    " question (start the block with 'Q:')."
                )
                continue
            correct = lowered.startswith("* ")
            text = line[2:].strip()
            if not text:
                errors.append(f"Line {lineno}: option text is empty.")
                continue
            if len(text) > OPTION_MAX_CHARS:
                errors.append(
                    f"Line {lineno}: option text exceeds"
                    f" {OPTION_MAX_CHARS} characters."
                )
                continue
            current["options"].append(
                {"text": text, "correct": correct}
            )
            continue
        errors.append(
            f"Line {lineno}: start questions with 'Q:' and options"
            " with '- ' or '* '."
        )
    if current is not None:
        questions.append(current)

    if not questions and not errors:
        errors.append("Add at least one question.")
    for number, question in enumerate(questions, start=1):
        if len(question["options"]) < 2:
            errors.append(
                f"Question {number}: give at least two options."
            )
        correct_count = sum(
            1 for option in question["options"]
            if option["correct"]
        )
        if correct_count != 1:
            errors.append(
                f"Question {number}: mark exactly one correct option"
                " with '* '."
            )
    if len(questions) > QUIZ_MAX_QUESTIONS:
        errors.append(
            f"A quiz carries at most {QUIZ_MAX_QUESTIONS} questions."
        )
    if errors:
        return None, errors
    return questions, []


def questions_payload(questions: list[dict]) -> str:
    """Serialize canonical questions to the stored JSON text.

    Args:
        questions: The parsed question list.

    Returns:
        Compact JSON (ensure_ascii=False so Hausa/Yoruba stay
        readable).
    """
    return json.dumps(questions, ensure_ascii=False)


def load_questions(raw: str | None) -> list[dict]:
    """Load and shape-check the stored question payload.

    Args:
        raw: The quiz's questions_json column.

    Returns:
        The question list, or [] when the payload is missing or
        malformed (a draft saved without questions, tampered row).
    """
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    shaped: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        raw_options = item.get("options")
        if not text or not isinstance(raw_options, list):
            continue
        options = []
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            option_text = str(option.get("text", ""))
            if not option_text:
                continue
            options.append({
                "text": option_text,
                "correct": bool(option.get("correct", False)),
            })
        if len(options) >= 2:
            shaped.append({"text": text, "options": options})
    return shaped


def display_order(quiz_id: int, annotator_id: int, count: int) -> list[int]:
    """Deterministic question order for one annotator and quiz.

    Args:
        quiz_id: Primary key of the quiz.
        annotator_id: Primary key of the annotator.
        count: Number of questions.

    Returns:
        A permutation of range(count), stable for this pair (a
        refresh never reshuffles a half-answered attempt) and
        different across annotators (no shoulder-surfing answer
        patterns).
    """
    if count <= 0:
        return []
    digest = hashlib.blake2b(
        f"quiz:{quiz_id}:{annotator_id}".encode("utf-8"),
        digest_size=16,
    ).hexdigest()
    order = list(range(count))
    # Fisher-Yates driven by successive digest slices — deterministic
    # and dependency-free.
    position = 0
    for index in range(count - 1, 0, -1):
        chunk = int(digest[position % len(digest)], 16)
        position += 1
        swap = chunk % (index + 1)
        order[index], order[swap] = order[swap], order[index]
    return order


def grade(
    questions: list[dict], answers: dict[str, int]
) -> tuple[int, int]:
    """Grade a submitted answer sheet against canonical questions.

    Args:
        questions: The canonical question list.
        answers: Mapping "q_<canonical_index>" -> chosen option index.

    Returns:
        (correct_count, total_count).
    """
    total = len(questions)
    correct = 0
    for index, question in enumerate(questions):
        chosen = answers.get(f"q_{index}")
        if chosen is None:
            continue
        if 0 <= chosen < len(question["options"]):
            if question["options"][chosen]["correct"]:
                correct += 1
    return correct, total


def score_percent(correct: int, total: int) -> int:
    """Integer percentage score, ROUND_HALF_UP-free (whole questions).

    Args:
        correct: Number of correct answers.
        total: Number of questions (> 0).

    Returns:
        The percent score as a whole number (2 of 3 questions is 67),
        integer arithmetic only — no float surprises.
    """
    if total <= 0:
        return 0
    return (correct * 100 + total // 2) // total
