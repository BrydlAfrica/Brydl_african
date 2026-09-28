"""SFT gold-rewrite pipeline (RLHF Phase 4): harvest, model, export.

Turns finalized platform work — approved submissions, binding
consensus rulings and seed gold answers — into supervised-fine-tuning
training pairs. An admin reviews the machine-harvested pair on the
SFT builder surface, rewrites the assistant completion where needed
(the "gold rewrite", with a mandatory written note on every deviation
from the harvested source), marks it ready, and exports a JSONL
messages corpus.

This module is PURE: it never touches the database or the request.
Every function degrades to None / empty output on malformed input —
task content is untrusted by definition, so the export path can never
crash on a bad row.

Harvest priority for the completion (first match wins):
1. A RESOLVED consensus group: the binding ruling. Preference tasks
   resolve to the decided response's full text; other kinds resolve
   to the decided verdict value itself.
2. The latest APPROVED submission's output document.
3. The task's gold ``expected_output`` (seed answer key).

The prompt side is extracted from the task input document per kind:
preference → its prompt; conversation → the last user turn; anything
else → its text/prompt field or the raw text.
"""

import json

# Keys tried, in order, when unwrapping an arbitrary output document
# into completion text (after the kind-specific handling).
_GENERIC_OUTPUT_KEYS = ("text", "answer", "marked_entities", "label")

# Conversation roles allowed in an input conversation document.
_CONVERSATION_ROLES = ("user", "assistant", "system")


def _load_document(raw: str) -> dict | None:
    """Parse a stored JSON document, tolerating anything malformed.

    Args:
        raw: The stored column value (compact JSON document).

    Returns:
        The parsed dict, or None when the value is not valid JSON of
        a JSON object.
    """
    try:
        doc = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _conversation_prompt(doc: dict) -> str | None:
    """Extract the SFT prompt from a conversation input document.

    The training prompt is the LAST user turn: the assistant reply the
    annotators judged is the completion, everything before it is
    context that the current single-turn pair format does not carry.

    Args:
        doc: A parsed input document (kind == "conversation").

    Returns:
        The last user turn's text, or None when the document holds no
        usable user turn.
    """
    turns = doc.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in reversed(turns):
        if (
            isinstance(turn, dict)
            and turn.get("role") in _CONVERSATION_ROLES
            and turn.get("role") == "user"
        ):
            text = turn.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def extract_prompt(input_data: str) -> str | None:
    """Extract the user-side prompt text from a task input document.

    Args:
        input_data: The task's stored input_data column.

    Returns:
        The prompt text (stripped), or None when nothing usable is
        found. Raw non-JSON input passes through as the prompt.
    """
    text = (input_data or "").strip()
    if not text:
        return None
    doc = _load_document(text)
    if doc is None:
        return text
    kind = doc.get("kind")
    if kind == "conversation":
        return _conversation_prompt(doc)
    for key in ("prompt", "text"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return text


def preference_response_text(input_data: str, chosen: str) -> str | None:
    """Map a canonical preference choice to that response's full text.

    Args:
        input_data: The task's stored input_data column.
        chosen: The canonical choice key ("a" or "b").

    Returns:
        The response text of the chosen side, or None when the input
        is not a valid preference document or the key is unknown.
    """
    doc = _load_document(input_data)
    if doc is None or chosen not in ("a", "b"):
        return None
    value = doc.get(f"response_{chosen}")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def unwrap_output(
    output_data: str, input_data: str | None = None
) -> str | None:
    """Unwrap a stored submission/gold document into completion text.

    Args:
        output_data: The stored output document (JSON) or plain text.
        input_data: The task's input document, needed to resolve a
            preference choice back to the winning response's text.

    Returns:
        The completion text (stripped), or None when the document
        yields nothing usable.
    """
    text = (output_data or "").strip()
    if not text:
        return None
    doc = _load_document(text)
    if doc is None:
        return text
    kind = doc.get("kind")
    if kind == "preference":
        return preference_response_text(input_data or "", doc.get("chosen"))
    if kind == "classification":
        value = doc.get("label")

        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    if kind == "rlhf":
        rating = doc.get("rating")
        explanation = doc.get("explanation")
        if isinstance(explanation, str) and explanation.strip():
            prefix = f"Rated {rating}/5: " if isinstance(rating, int) else ""
            return (prefix + explanation.strip()).strip()
        return None
    for key in _GENERIC_OUTPUT_KEYS:
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def harvest_completion(
    input_data: str,
    expected_output: str | None,
    group_status: str | None,
    group_choice: str | None,
    approved_output: str | None,
) -> tuple[str | None, str]:
    """Resolve the machine-harvested completion and its provenance.

    Args:
        input_data: The task's input document.
        expected_output: The task's gold answer (may be None).
        group_status: The task's consensus group status (None when the
            task has no group).
        group_choice: The resolved group's binding decided_choice.
        approved_output: The latest approved submission's output
            document (None when the task has none).

    Returns:
        A (source_completion, source) tuple; source is one of
        "consensus", "approved", "gold". The completion is None when
        no source yields usable text (a manually typed pair is still
        possible on the builder form).
    """
    if group_status == "resolved" and group_choice:
        text = preference_response_text(input_data, group_choice)
        if text is None:
            decided = group_choice.strip()
            text = decided if decided else None
        if text is not None:
            return text, "consensus"
    if approved_output:
        text = unwrap_output(
            approved_output, input_data
        )
        if text is not None:
            return text, "approved"
    if expected_output:
        text = unwrap_output(expected_output, input_data)
        if text is not None:
            return text, "gold"
    return None, "manual"


def to_messages(
    prompt: str, completion: str, system_prompt: str | None = None
) -> dict:
    """Build one SFT record in chat-messages format.

    Args:
        prompt: The user-side prompt text.
        completion: The assistant-side gold completion text.
        system_prompt: Optional system message.

    Returns:
        A dict of the shape
        ``{"messages": [{role, content}, ...]}`` with an optional
        leading system message.
    """
    messages: list[dict[str, str]] = []
    system = (system_prompt or "").strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": (prompt or "").strip()})
    messages.append(
        {"role": "assistant", "content": (completion or "").strip()}
    )
    return {"messages": messages}


def export_jsonl(rows: list[dict]) -> str:
    """Serialize SFT examples to one JSON object per line.

    Args:
        rows: Prepared export dicts, each carrying ``id``, ``task_id``,
            ``project_id``, ``messages`` and ``updated_at`` (a UTC
            datetime or ISO string; anything else is stringified).

    Returns:
        The JSONL body (newline-terminated lines; an empty list gives
        an empty string).
    """
    lines: list[str] = []
    for row in rows:
        record = {
            "id": row.get("id"),
            "task_id": row.get("task_id"),
            "project_id": row.get("project_id"),
            **to_messages(
                row.get("prompt", ""),
                row.get("completion", ""),
                row.get("system_prompt"),
            ),
            "updated_at": str(row.get("updated_at", "")),
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")
