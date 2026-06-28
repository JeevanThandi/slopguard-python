"""Entry point: ``python -m slopguard`` and the ``slopguard-python`` console
script both call :func:`main`."""

from __future__ import annotations

import sys

from .cli import run


def main() -> int:
    code = run(sys.argv[1:], sys.stdout, sys.stderr)
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover
    main()
