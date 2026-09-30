"""A source file as ``mutate`` sees it: the original bytes, the decoded text,
and the line table that maps parser positions to text offsets.

Python sources may declare their encoding (PEP 263), so the bytes are decoded
with the declared codec and every mutant is encoded back with the same codec.
Only the mutated span changes on disk: the encoding, a UTF-8 BOM and the line
endings survive.
"""

from __future__ import annotations

import io
import re
import tokenize
from typing import Dict, List

from ..errors import unreadable_file

# The line terminators the Python tokenizer honours. ``str.splitlines`` would
# also split on form feeds and U+2028, which the parser treats as ordinary
# characters, and the line numbers would drift.
_NEWLINE = re.compile(r"\r\n|\r|\n")


class SourceFile:
    """The decoded text of one file plus position helpers."""

    def __init__(self, data: bytes, path: str) -> None:
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
            raise unreadable_file(path, exc)
        if text.encode(encoding) != data:
            raise unreadable_file(path, f"the {encoding} text does not encode back to the same bytes")
        self.path = path
        self.data = data
        self.encoding = encoding
        self.text = text
        self._starts = [0] + [m.end() for m in _NEWLINE.finditer(text)]
        self._utf8: Dict[int, bytes] = {}

    @property
    def line_count(self) -> int:
        return len(self._starts)

    def line(self, number: int) -> str:
        """Line ``number`` (1-based) without its terminator."""
        start = self._starts[number - 1]
        end = self._starts[number] if number < len(self._starts) else len(self.text)
        return self.text[start:end].rstrip("\r\n")

    def lines(self) -> List[str]:
        """Every line without its terminator (index 0 = line 1)."""
        return [self.line(n) for n in range(1, self.line_count + 1)]

    def offset(self, line: int, column: int) -> int:
        """Text offset of the 0-based code-point ``column`` on 1-based ``line``."""
        return self._starts[line - 1] + column

    def column_of_byte(self, line: int, byte_column: int) -> int:
        """Convert the parser's UTF-8 byte column (``col_offset``) on ``line``
        into a 0-based code-point column."""
        encoded = self._utf8.get(line)
        if encoded is None:
            encoded = self.line(line).encode("utf-8")
            self._utf8[line] = encoded
        return len(encoded[:byte_column].decode("utf-8", errors="ignore"))

    def tokens(self) -> List[tokenize.TokenInfo]:
        """The token stream. Line terminators are normalised to ``\\n`` first;
        that never moves a token's ``(row, column)``."""
        normalized = "\n".join(self.lines())
        return list(tokenize.generate_tokens(io.StringIO(normalized).readline))

    def splice(self, start: int, end: int, replacement: str) -> str:
        """The text with ``[start, end)`` replaced."""
        return self.text[:start] + replacement + self.text[end:]

    def encode(self, text: str) -> bytes:
        """Encode (mutated) text with the file's own codec."""
        return text.encode(self.encoding)
