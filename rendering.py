"""Trusted rendering of untrusted task content (RLHF Phase 3).

Annotators and admins display model prompts, responses and gold text
that may contain markdown, fenced code, or multi-turn conversation
JSON. That content is UNTRUSTED INPUT: it is converted to HTML
server-side and sanitized against a strict allowlist (nh3) before any
template marks it safe. There is deliberately NO client-side
rendering and NO third-party JS — syntax highlighting is Pygments
with CSS classes (static/css/pygments.css), keeping the offline-first
rule intact.

Failures degrade to escaped plain text; this module never raises.
"""

import html
import json
import logging

import nh3
import markdown as md

from config import MARKDOWN_RENDER_MAX_CHARS

logger = logging.getLogger(__name__)

# Sanitizer allowlist: everything a markdown document may legitimately
# produce — nothing more. span keeps its class (Pygments tokens);
# anchors keep only http(s)/mailto hrefs.
_ALLOWED_TAGS = frozenset({
    "p", "br", "strong", "em", "b", "i", "u", "s", "code", "pre",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td", "hr", "a", "span",
    "del", "sup", "sub",
})
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "span": {"class"},
    "pre": {"class"},  # Pygments' codehilite wrapper hook
    "th": {"align"},
    "td": {"align"},
}
_ALLOWED_SCHEMES = frozenset({"http", "https", "mailto"})

_MD = md.Markdown(
    extensions=["fenced_code", "tables", "codehilite", "nl2br",
                "sane_lists"],
    extension_configs={
        "codehilite": {"noclasses": False, "guess_lang": False},
    },
)

_ROLES = ("user", "assistant", "system")


def _sanitize(html_text: str) -> str:
    """Return ``html_text`` filtered to the rendering allowlist.

    Args:
        html_text: Generated HTML (e.g. markdown output).

    Returns:
        HTML containing only allowlisted tags, attributes and URL
        schemes.
    """
    return nh3.clean(
        html_text,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_ALLOWED_SCHEMES,
    )


def render_rich(text: str) -> str:
    """Convert untrusted text to sanitized rich HTML.

    Args:
        text: Raw stored text (markdown, code, or plain).

    Returns:
        Sanitized HTML fragment, or escaped plain text for oversize
        input.
    """
    text = text or ""
    if len(text) > MARKDOWN_RENDER_MAX_CHARS:
        return html.escape(text)
    try:
        _MD.reset()
        return _sanitize(_MD.convert(text))
    except Exception:
        logger.exception("Markdown rendering failed; using plain text.")
        return html.escape(text)


def conversation_turns(text: str) -> list[dict] | None:
    """Parse a multi-turn conversation document for bubble display.

    Args:
        text: Stored task input — expected JSON of the form
            {"kind": "conversation", "turns": [{"role": ..., "text":
            ...}, ...]} with roles user/assistant/system.

    Returns:
        A list of {"role", "html"} turns with sanitized rich HTML, or
        None when the input is not a valid conversation document.
    """
    try:
        doc = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("kind") != "conversation":
        return None
    turns = doc.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    parsed: list[dict] = []
    for turn in turns:
        if not isinstance(turn, dict):
            return None
        role = turn.get("role")
        body = turn.get("text")
        if role not in _ROLES or not isinstance(body, str) or not body:
            return None
        parsed.append({"role": role, "html": render_rich(body)})
    return parsed


def content_view(text: str) -> dict:
    """Build the display structure for a stored content string.

    Args:
        text: Raw stored text (may be a conversation JSON document).

    Returns:
        {"type": "conversation", "turns": [...]} for conversation
        documents, else {"type": "rich", "html": sanitized HTML}.
    """
    turns = conversation_turns(text)
    if turns is not None:
        return {"type": "conversation", "turns": turns}
    return {"type": "rich", "html": render_rich(text)}
