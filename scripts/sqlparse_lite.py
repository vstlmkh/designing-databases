#!/usr/bin/env python3
"""Tolerant SQL statement splitter shared by the bundled auditors.

Standard library only. Not a real parser: it strips comments, respects string
literals, dollar-quoting, backtick/double-quote identifiers and parentheses,
then yields top-level statements with the line number they start on.
"""
from __future__ import annotations

import re
from typing import Iterator, NamedTuple


class Statement(NamedTuple):
    text: str
    line: int


def _strip_comments(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "\\" and quote == "'":
                    if i + 1 < n:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                if sql[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "$":  # dollar-quoted body ($$ ... $$ or $tag$ ... $tag$)
            m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end == -1 else end + len(tag)
                out.append(sql[i:end])
                i = end
                continue
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))  # keep line numbering intact
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(re.sub(r"[^\n]", " ", sql[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> Iterator[Statement]:
    """Yield top-level `;`-terminated statements with their 1-based start line."""
    clean = _strip_comments(sql)
    depth = 0
    buf: list[str] = []
    line = 1
    start_line = 1
    i, n = 0, len(clean)
    while i < n:
        ch = clean[i]
        if ch == "\n":
            line += 1
        if not buf or buf == [""] or "".join(buf).strip() == "":
            start_line = line
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(clean[i])
                if clean[i] == "\n":
                    line += 1
                if clean[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            text = "".join(buf).strip()
            if text:
                yield Statement(text, start_line)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        yield Statement(tail, start_line)


def split_columns(body: str) -> list[str]:
    """Split the inside of a CREATE TABLE (...) on top-level commas."""
    parts, depth, buf = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def unquote(ident: str) -> str:
    return ident.strip().strip('`"[]').strip()


def paren_body(text: str, start: int) -> tuple[str, int]:
    """Return the contents of the parenthesis group opening at/after `start`."""
    open_at = text.find("(", start)
    if open_at == -1:
        return "", start
    depth, i = 0, open_at
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i], i + 1
        i += 1
    return text[open_at + 1 :], len(text)
