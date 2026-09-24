"""Turn Jarvis's Markdown replies into Telegram messages.

Telegram understands a small HTML subset (<b>, <i>, <s>, <code>, <pre>, <a>).
Everything else is escaped, so a reply can never inject markup. If a message
still fails to parse, the bot resends it as plain text.
"""

from __future__ import annotations

import html
import re

SAFE_CHUNK = 3_500  # Markdown per message; leaves room for tags within the 4,096 limit

_FENCE_RE = re.compile(r"^\s*```\s*([\w+#.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*|__(?!\s)(.+?)(?<!\s)__")
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*(?![\s*])([^*\n<>]+?)(?<![\s*])\*(?![\w*])"
    r"|(?<![\w_])_(?![\s_])([^_\n<>]+?)(?<![\s_])_(?![\w_])"
)
_STRIKE_RE = re.compile(r"~~(?!\s)([^~\n<>]+?)(?<!\s)~~")
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
_TAG_RE = re.compile(r"<(/?)(b|i|s|code|pre|a)(?:\s[^>]*)?>")


def _inline(text: str) -> str:
    kept: list[str] = []

    def keep(fragment: str) -> str:
        kept.append(fragment)
        return f"\x00{len(kept) - 1}\x00"

    text = _CODE_SPAN_RE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text
    )
    text = _LINK_RE.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{html.escape(m.group(1), quote=False)}</a>"
        ),
        text,
    )
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    return _PLACEHOLDER_RE.sub(lambda m: kept[int(m.group(1))], text)


def _line(line: str) -> str:
    if heading := _HEADING_RE.match(line):
        return f"<b>{_inline(heading.group(1))}</b>"
    if bullet := _BULLET_RE.match(line):
        return f"{bullet.group(1)}• {_inline(bullet.group(2))}"
    return _inline(line)


def balanced(markup: str) -> bool:
    """True if every tag the converter can produce is properly nested and closed."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(markup):
        closing, tag = match.group(1), match.group(2)
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return False
    return not stack


def to_html(markdown: str) -> str:
    """Telegram HTML for `markdown`, or its escaped text if the markup would be broken."""
    markdown = markdown.replace("\x00", "").replace("\r\n", "\n")
    out: list[str] = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        fence = _FENCE_RE.match(lines[i])
        if fence is None:
            out.append(_line(lines[i]))
            i += 1
            continue
        language = fence.group(1)
        body: list[str] = []
        i += 1
        while i < len(lines) and not _FENCE_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        i += 1  # the closing fence (or the end of the text)
        code = html.escape("\n".join(body), quote=False)
        attr = f' class="language-{html.escape(language, quote=True)}"' if language else ""
        out.append(f"<pre><code{attr}>{code}</code></pre>")
    result = "\n".join(out).strip()
    return result if balanced(result) else html.escape(markdown.strip(), quote=False)


def to_plain(markdown: str) -> str:
    """Readable plain text (for speech, and when Telegram rejects the HTML)."""
    text = markdown.replace("\x00", "")
    text = re.sub(r"^\s*```.*$", "", text, flags=re.MULTILINE)
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("~~", "").replace("`", "")
    text = _ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_speech(markdown: str) -> str:
    """What to say aloud: plain text without link addresses or bullet marks."""
    text = _LINK_RE.sub(lambda m: m.group(1), markdown)
    text = to_plain(text).replace("• ", "")
    return re.sub(r"https?://\S+", "", text).strip()


def split_markdown(text: str, limit: int = SAFE_CHUNK) -> list[str]:
    """Split a long reply into message-sized pieces at line breaks.

    A code block cut in two is closed at the end of one piece and reopened at
    the start of the next, so each piece renders on its own.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    fence_line: str | None = None  # the opening fence while inside a code block
    for line in text.replace("\r\n", "\n").split("\n"):
        pieces = [line[i : i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if current and size + len(piece) + 1 > limit:
                if fence_line is not None:
                    current.append("```")
                chunks.append("\n".join(current))
                current = [fence_line] if fence_line is not None else []
                size = sum(len(c) + 1 for c in current)
            current.append(piece)
            size += len(piece) + 1
        if _FENCE_RE.match(line):
            fence_line = None if fence_line is not None else line
    if current:
        chunks.append("\n".join(current))
    return [c.strip("\n") for c in chunks if c.strip()]
