"""Markdown helpers so requester and reviewer text renders verbatim, never as structure."""
from __future__ import annotations


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def code(text: str) -> str:
    """An inline code span that no backtick or pipe in `text` can break out of."""
    text = cell(text)
    fence = "`" * (longest_backtick_run(text) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def fenced(text: str, indent: str = "") -> "list[str]":
    fence = "`" * max(3, longest_backtick_run(text) + 1)
    return [f"{indent}{fence}text", *[f"{indent}{line}" for line in text.strip("\n").splitlines()], f"{indent}{fence}"]


def longest_backtick_run(text: str) -> int:
    longest = current = 0
    for char in text:
        current = current + 1 if char == "`" else 0
        longest = max(longest, current)
    return longest
