"""Minimal glob matcher replicating the siblings' fnmatch semantics.

``*`` matches across path separators — which is the behaviour most people expect
from ``**``-style globs ("everything under ``.venv``"). ``?`` matches one
character; ``[...]`` character classes pass through (``[!...]`` negates).
"""

from __future__ import annotations

import re
from typing import Dict, List, Pattern

_glob_cache: Dict[str, Pattern[str]] = {}


def glob_to_regex(glob: str) -> Pattern[str]:
    """Compile an fnmatch-style glob into an anchored regular expression."""
    out: List[str] = ["^"]
    i = 0
    n = len(glob)
    while i < n:
        ch = glob[i]
        if ch == "*":
            out.append(".*")
            i += 1
        elif ch == "?":
            out.append(".")
            i += 1
        elif ch == "[":
            close = glob.find("]", i + 2)
            if close < 0:
                out.append(re.escape("["))
                i += 1
                continue
            body = glob[i + 1 : close]
            if body.startswith("!"):
                body = "^" + body[1:]
            body = body.replace("\\", "\\\\")
            out.append("[" + body + "]")
            i = close + 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _compiled(glob: str) -> Pattern[str]:
    cached = _glob_cache.get(glob)
    if cached is None:
        cached = glob_to_regex(glob)
        _glob_cache[glob] = cached
    return cached


def matches_any(globs: List[str], path: str) -> bool:
    """Match ``path`` against each glob, also trying a leading-slash variant so
    that ``**/foo/**`` patterns match a top-level ``foo/bar.py`` (mirroring
    gitignore semantics where a leading ``**/`` is effectively implicit). Paths
    must be forward-slash normalised."""
    with_slash = "/" + path
    for g in globs:
        rx = _compiled(g)
        if rx.match(path) or rx.match(with_slash):
            return True
    return False
