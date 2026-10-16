"""A request body read the way upstream reads it, through the Fetch API's `Request.json()` on Node.

That is undici's `parseJSONFromBytes`: drop a UTF-8 byte order mark, decode UTF-8 with a `TextDecoder`,
which replaces what does not decode and drops a second mark, then V8's `JSON.parse`. Python's `json`
accepts the same documents, less `NaN` and the infinities, but describes a refusal its own way, so a
refused body is scanned again here for the message V8's `JsonParser` (`src/json/json-parser.cc`) reports.
Positions, columns and the quoted context are in UTF-16 code units, as V8 counts them.

Upstream's oracle runs on Node 24. Other JavaScript runtimes word these messages differently.
"""

import json
from typing import NoReturn

import orjson

from zero_server.shared.json import JSONValue

_BOM = b"\xef\xbb\xbf"


class JSONSyntaxError(ValueError):
    """`JSON.parse`'s `SyntaxError`."""

    error_name = "SyntaxError"


class StackOverflowError(ValueError):
    """V8's `RangeError` once the stack runs out, which is how upstream refuses a body nested too deep."""

    error_name = "RangeError"

    def __init__(self) -> None:
        super().__init__("Maximum call stack size exceeded")


def decode_body(body: bytes) -> str:
    # `utf-8-sig` drops the second mark, as the `TextDecoder` does.
    return body.removeprefix(_BOM).decode("utf-8-sig", "replace")


def _refuse_constant(name: str) -> None:
    raise ValueError(name)


def parse_json(text: str) -> JSONValue:
    """`JSON.parse(text)`, raising `JSONSyntaxError` with V8's message."""
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except ValueError:
        raise JSONSyntaxError(_V8Scanner(text).error()) from None
    except RecursionError:
        # Python's `json` stops near 10,000 levels; upstream refuses well before that, its validators
        # running out of stack.
        raise StackOverflowError from None


def parse_body(body: bytes) -> JSONValue:
    """`await request.json()` for a request whose body is `body`.

    orjson parses what it can. It refuses invalid UTF-8, lone surrogates, numbers past the double range
    and deep nesting, all of which take the `json` path to keep upstream's results and messages. It also
    reads an integer past 64 bits as a float, where `json` keeps the int, so a run of 19 digits anywhere
    takes that path too.
    """
    if _LONG_INTEGER not in body.translate(_DIGIT_RUNS):
        try:
            return orjson.loads(body.removeprefix(_BOM).removeprefix(_BOM))
        except orjson.JSONDecodeError:
            pass
    return parse_json(decode_body(body))


_DIGIT_RUNS = bytes(ord("0") if chr(b).isdigit() and b < 128 else ord(" ") for b in range(256))
_LONG_INTEGER = b"0" * 19


_EOS = -1
_WHITESPACE = frozenset(map(ord, " \t\n\r"))
_HEX = frozenset(map(ord, "0123456789abcdefABCDEF"))
_ESCAPES = frozenset(map(ord, '"\\/bfnrt'))
_CONTEXT = 10
_SHORT = 2 * _CONTEXT
_SPECIAL = frozenset({"[object Object]", "undefined", "Infinity", "NaN"})

# Each of these continues " at position {pos} (line {line} column {column})".
_MESSAGES = {
    "prop_or_rbrace": "Expected property name or '}' in JSON",
    "comma_or_rbrack": "Expected ',' or ']' after array element in JSON",
    "comma_or_rbrace": "Expected ',' or '}' after property value in JSON",
    "colon": "Expected ':' after property name in JSON",
    "quoted_prop": "Expected double-quoted property name in JSON",
    "unterminated_string": "Unterminated string in JSON",
    "control": "Bad control character in string literal in JSON",
    "unicode": "Bad Unicode escape in JSON",
    "escape": "Bad escaped character in JSON",
    "minus": "No number after minus sign in JSON",
    "exponent": "Exponent part is missing a number in JSON",
    "fraction": "Unterminated fractional number in JSON",
    "trailing": "Unexpected non-whitespace character after JSON",
    "number": "Unexpected number in JSON",
    "string": "Unexpected string in JSON",
}


class _Error(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def _token(unit: int) -> str:
    """V8's `one_char_json_tokens` for one code unit."""
    if unit == _EOS:
        return "eos"
    if unit in _WHITESPACE:
        return "whitespace"
    char = chr(unit)
    if char == '"':
        return "string"
    if char == "-" or (char.isascii() and char.isdigit()):
        return "number"
    return {"t": "true", "f": "false", "n": "null", ":": ":", ",": ",", "[": "[", "]": "]", "{": "{", "}": "}"}.get(
        char, "illegal"
    )


class _V8Scanner:
    """Finds the first error in a JSON text exactly where V8's parser does, and words it as V8 does."""

    def __init__(self, text: str) -> None:
        raw = text.encode("utf-16-le", "surrogatepass")
        self.units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
        self.cursor = 0

    def error(self) -> str:
        try:
            self._parse()
        except _Error as error:
            return error.message

    # Reading.

    def _at(self, index: int) -> int:
        return self.units[index] if index < len(self.units) else _EOS

    def _peek(self) -> str:
        return _token(self._at(self.cursor))

    def _skip_whitespace(self) -> None:
        while self._at(self.cursor) in _WHITESPACE:
            self.cursor += 1

    def _next(self) -> int:
        self.cursor += 1
        return self._at(self.cursor)

    def _check(self, token: str) -> bool:
        self._skip_whitespace()
        if self._peek() != token:
            return False
        self.cursor += 1
        return True

    def _expect_next(self, token: str, message: str) -> None:
        self._skip_whitespace()
        if self._peek() != token:
            self._report(self._peek(), message)
        self.cursor += 1

    # Reporting.

    def _source(self, start: int = 0, end: int | None = None) -> str:
        units = self.units[start:end]
        return b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le", "surrogatepass")

    def _location(self) -> tuple[int, int]:
        line, line_start, i = 1, 0, 0
        while i < self.cursor:
            unit = self.units[i]
            if unit == 0x0D and i < self.cursor - 1 and self.units[i + 1] == 0x0A:
                i += 1
                unit = 0x0A
            if unit in (0x0D, 0x0A):
                line += 1
                line_start = i + 1
            i += 1
        return line, 1 + i - line_start

    def _report(self, token: str, message: str | None = None) -> NoReturn:
        pos = self.cursor
        if message is None and token in ("number", "string"):
            message = token
        if message is not None:
            line, column = self._location()
            raise _Error(f"{_MESSAGES[message]} at position {pos} (line {line} column {column})")
        if token == "eos":
            raise _Error("Unexpected end of JSON input")
        source = self._source()
        if source in _SPECIAL:
            raise _Error(f'"{source}" is not valid JSON')
        char = self._source(pos, pos + 1)
        length = len(self.units)
        if length <= _SHORT:
            raise _Error(f"Unexpected token '{char}', \"{source}\" is not valid JSON")
        if pos < _CONTEXT:
            raise _Error(f"Unexpected token '{char}', \"{self._source(0, pos + _CONTEXT)}\"... is not valid JSON")
        if pos < length - _CONTEXT:
            context = self._source(pos - _CONTEXT, pos + _CONTEXT)
            raise _Error(f"Unexpected token '{char}', ...\"{context}\"... is not valid JSON")
        raise _Error(f"Unexpected token '{char}', ...\"{self._source(pos - _CONTEXT)}\" is not valid JSON")

    def _report_char(self, unit: int) -> NoReturn:
        self._report(_token(unit) if unit == _EOS or unit <= 0xFF else "illegal")

    # Parsing, as `ParseJsonValue` does, with the containers on an explicit stack.

    def _parse(self) -> NoReturn:
        stack: list[str] = []
        while True:
            # Produce a value.
            while True:
                self._skip_whitespace()
                token = self._peek()
                if token == "string":
                    self.cursor += 1
                    self._scan_string()
                elif token == "number":
                    self._scan_number()
                elif token == "{":
                    self.cursor += 1
                    if self._check("}"):
                        break
                    stack.append("object")
                    self._expect_next("string", "prop_or_rbrace")
                    self._scan_string()
                    self._expect_next(":", "colon")
                    continue
                elif token == "[":
                    self.cursor += 1
                    if self._check("]"):
                        break
                    stack.append("array")
                    continue
                elif token in ("true", "false", "null"):
                    self._scan_literal(token)
                else:
                    self._report(token)
                break
            # Consume it into its container.
            while True:
                if not stack:
                    # Python's json refused the document, so V8 does too, and something follows the value.
                    assert not self._check("eos"), "V8 accepts a document Python's json refused"
                    self._report(self._peek(), "trailing")
                if stack[-1] == "object":
                    if self._check(","):
                        self._expect_next("string", "quoted_prop")
                        self._scan_string()
                        self._expect_next(":", "colon")
                        break
                    self._skip_whitespace()
                    if self._peek() != "}":
                        self._report(self._peek(), "comma_or_rbrace")
                    self.cursor += 1
                else:
                    if self._check(","):
                        break
                    self._skip_whitespace()
                    if self._peek() != "]":
                        self._report(self._peek(), "comma_or_rbrack")
                    self.cursor += 1
                stack.pop()

    def _scan_string(self) -> None:
        """From just past the opening quote to just past the closing one."""
        while True:
            unit = self._at(self.cursor)
            if unit == _EOS:
                self._report("illegal", "unterminated_string")
            if unit == 0x22:
                self.cursor += 1
                return
            if unit == 0x5C:
                escaped = self._next()
                if escaped == _EOS or escaped > 0xFF:
                    self._report_char(escaped)
                if escaped == ord("u"):
                    for _ in range(4):
                        if self._next() not in _HEX:
                            self._report("illegal", "unicode")
                elif escaped not in _ESCAPES:
                    self._report("illegal", "escape")
                self.cursor += 1
                continue
            if unit < 0x20:
                self._report("illegal", "control")
            self.cursor += 1

    def _scan_number(self) -> None:
        unit = self._at(self.cursor)
        if unit == ord("-"):
            unit = self._next()
        if unit == ord("0"):
            unit = self._next()
            if 0x30 <= unit <= 0x39:
                self._report("number")
        else:
            if not 0x30 <= unit <= 0x39:
                self._report("illegal", "minus")
            while 0x30 <= self._at(self.cursor) <= 0x39:
                self.cursor += 1
        if self._at(self.cursor) == ord("."):
            if not 0x30 <= self._next() <= 0x39:
                self._report("illegal", "fraction")
            while 0x30 <= self._at(self.cursor) <= 0x39:
                self.cursor += 1
        if self._at(self.cursor) in (ord("e"), ord("E")):
            unit = self._next()
            if unit in (ord("-"), ord("+")):
                unit = self._next()
            if not 0x30 <= unit <= 0x39:
                self._report("illegal", "exponent")
            while 0x30 <= self._at(self.cursor) <= 0x39:
                self.cursor += 1

    def _scan_literal(self, literal: str) -> None:
        expected = [ord(c) for c in literal]
        remaining = len(self.units) - self.cursor
        if remaining >= len(expected) and self.units[self.cursor : self.cursor + len(expected)] == expected:
            self.cursor += len(expected)
            return
        self.cursor += 1
        for i in range(min(len(expected) - 1, remaining - 1)):
            if self._at(self.cursor) != expected[1 + i]:
                self._report_char(self._at(self.cursor))
            self.cursor += 1
        self._report("eos")
