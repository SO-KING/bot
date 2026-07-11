"""Minimal ANSI escape parser → Telegram HTML.

Telegram supports a small subset of HTML in messages (parse_mode='HTML'):
<i>, <b>, <u>, <s>, <code>, <pre>.

We convert common SGR (Select Graphic Rendition) codes:
- 0      reset
- 1      bold
- 3      italic
- 4      underline
- 9      strikethrough
- 22     bold off
- 23     italic off
- 24     underline off
- 29     strikethrough off
- 30..37, 90..97  foreground colors (mapped to bold/normal colored via <span> not supported)
- 38;5;n, 38;2;r;g;b  256/true color → ignored (Telegram has no inline color)
- 40..47, 49         background → ignored
- 39                 default fg → ignored

We map colors to <b>/<i>/<u>/<s> only — Telegram cannot colorize, but bold + underline
roughly preserve visual emphasis. Each color code toggles <b> so different colors appear
distinctly as bold vs not bold — crude but readable.
"""

import re
from html import escape

_ANSI_RE = re.compile(
    # 1) Match a complete CSI sequence (incl. private '?' / '>' markers and any params)
    r"\x1b\[[?>]?[0-9;]*[ -/]*[@-~]"
    # 2) Match other C1 / single-char escapes that some agents emit
    r"|\x1b[=>]"
    # 3) OSC sequences (title bar / clipboard): ESC ] ... BEL or ST
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    # 4) Stray UTF-8 BOMs (in case an agent echoes one)
    r"|\ufeff"
)
# SGR codes (the only ones we actually convert to HTML tags)
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _open_tags(sgrs: list[int]) -> str:
    out = ""
    for c in sgrs:
        if c == 1:
            out += "<b>"
        elif c == 3:
            out += "<i>"
        elif c == 4:
            out += "<u>"
        elif c == 9:
            out += "<s>"
    return out


def _close_tags(sgrs: list[int]) -> str:
    out = ""
    for c in reversed(sgrs):
        if c == 1:
            out = "</b>" + out
        elif c == 3:
            out = "</i>" + out
        elif c == 4:
            out = "</u>" + out
        elif c == 9:
            out = "</s>" + out
    return out


def ansi_to_html(text: str) -> str:
    if not text:
        return ""
    # 1. Strip non-SGR ANSI sequences (cursor moves, erase, etc.)
    text = _ANSI_RE.sub("", text)
    # 2. Walk through SGR codes, maintaining a stack of active styles
    out: list[str] = []
    stack: list[int] = []
    pos = 0
    for m in _SGR_RE.finditer(text):
        # add escaped text before this control
        out.append(escape(text[pos:m.start()]))
        pos = m.end()
        codes = m.group(1)
        if not codes:
            # full reset
            out.append(_close_tags(stack))
            stack.clear()
            continue
        for c in codes.split(";"):
            if not c:
                continue
            n = int(c)
            if n == 0:
                out.append(_close_tags(stack))
                stack.clear()
            elif n in (22, 23, 24, 29):
                # turn specific style off
                target = {22: 1, 23: 3, 24: 4, 29: 9}[n]
                if target in stack:
                    out.append(_close_tags(stack))
                    stack = [s for s in stack if s != target]
                    out.append(_open_tags(stack))
            elif n in (1, 3, 4, 9):
                if n not in stack:
                    stack.append(n)
                    out.append(_open_tags([n]))
            # color codes ignored (telegram can't colorize inline)
    out.append(escape(text[pos:]))
    # close any dangling tags
    out.append(_close_tags(stack))
    return "".join(out)


def clean_for_telegram(text: str, max_chars: int) -> list[str]:
    """Truncate and chunk text for Telegram's 4096-char message limit,
    never splitting inside HTML tags or multi-byte sequences."""
    rendered = ansi_to_html(text)
    if len(rendered) <= max_chars:
        return [rendered]
    chunks: list[str] = []
    cur = ""
    i = 0
    while i < len(rendered):
        # detect tag start
        if rendered[i] == "<":
            end = rendered.find(">", i)
            if end == -1:
                break
            tag = rendered[i:end + 1]
            i = end + 1
        else:
            tag = rendered[i]
            i += 1
        if len(cur) + len(tag) > max_chars - 50:
            chunks.append(cur)
            cur = tag
        else:
            cur += tag
    if cur:
        chunks.append(cur)
    return chunks
