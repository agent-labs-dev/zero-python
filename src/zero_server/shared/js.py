"""JavaScript's `String(value)`, `JSON.stringify` and the other places upstream's output follows JavaScript."""

import json
import math
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from functools import cmp_to_key


def js_string(value: object) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int() | float():
            return js_number(value)
        case list() | tuple():
            return ",".join("" if item is None else js_string(item) for item in value)
        case dict():
            return "[object Object]"
        case _:
            return str(value)


# Integers below 2**53 are exactly representable as a double, so JavaScript prints their digits as they are.
_MAX_EXACT = 2**53


def js_number(value: float) -> str:
    """`Number.prototype.toString()`: the shortest round-trip digits, laid out by ECMA-262's rules."""
    if type(value) is int and -_MAX_EXACT < value < _MAX_EXACT:
        return str(value)
    if type(value) is float and 1e-4 <= abs(value) < 1e16:
        # Python's repr writes the same shortest digits, and in this range lays them out as JavaScript does.
        text = repr(value)
        return text[:-2] if text.endswith(".0") else text
    if not math.isfinite(value):
        return "NaN" if value != value else "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    if value < 0:
        return "-" + js_number(-value)
    _sign, digit_tuple, exponent = Decimal(repr(float(value))).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    exponent += len(digit_tuple) - len(digits)
    k = len(digits)
    n = k + exponent
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    e = n - 1
    exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
    return digits + exp if k == 1 else digits[0] + "." + digits[1:] + exp


def js_sort[T](items: list[T], compare: Callable[[T, T], int]) -> list[T]:
    """`items.sort(compare)` as V8 runs it, returning a new list.

    Upstream's `cmpCondition` is not a consistent comparator (a compound condition and a subquery each
    sort before the other), so the result depends on the exact comparisons the sort makes. V8 sorts an
    array shorter than 64 by finding the leading run and binary-inserting the rest, which this repeats
    comparison for comparison. Longer arrays fall back to Python's sort, which is the same TimSort.
    """
    out = list(items)
    n = len(out)
    if n < 2:
        return out
    if n >= 64:
        return sorted(out, key=cmp_to_key(compare))
    run = 2
    descending = compare(out[1], out[0]) < 0
    while run < n:
        order = compare(out[run], out[run - 1])
        if (order < 0) != descending:
            break
        run += 1
    if descending:
        out[:run] = reversed(out[:run])
    for start in range(run, n):
        pivot = out[start]
        left, right = 0, start
        while left < right:
            mid = left + ((right - left) >> 1)
            if compare(pivot, out[mid]) < 0:
                right = mid
            else:
                left = mid + 1
        out[left + 1 : start + 1] = out[left:start]
        out[left] = pivot
    return out


def js_check_in(key: str, value: object) -> None:
    """Raise the `TypeError` JavaScript throws for `key in value` when `value` is not an object.

    `None` stands for `undefined`, which is what a function that returns nothing gives back.
    """
    if value is None or isinstance(value, bool | int | float | str):
        operand = "undefined" if value is None else js_string(value)
        raise TypeError(f"Cannot use 'in' operator to search for '{key}' in {operand}")


def js_truthy(value: object) -> bool:
    """JavaScript's truthiness for a JSON value: an empty array or object is truthy there."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value == value and value != 0  # NaN is falsy
    return True


# `JSON.stringify`'s escaping of a string, without `json.dumps` building an encoder for every call.
_encode_string = json.JSONEncoder(ensure_ascii=False).encode


def js_stringify(value: object) -> str:
    """`JSON.stringify(value)` for a JSON value: compact, numbers written as JavaScript writes them."""
    if type(value) is str:
        return _encode_string(value) if value.isascii() else _quote(value)
    kind = type(value)
    if kind is int and -_MAX_EXACT < value < _MAX_EXACT:  # ty: ignore[unsupported-operator]
        return str(value)
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return js_number(value)
        case float():
            # `JSON.parse("1e400")` is Infinity, which `JSON.stringify` writes as null.
            return js_number(value) if math.isfinite(value) else "null"
        case str():  # python-only: a str subclass, such as a StrEnum
            return _encode_string(value) if value.isascii() else _quote(value)
        case list() | tuple():
            return "[" + ",".join([js_stringify(item) for item in value]) + "]"
        case _:
            assert isinstance(value, dict), f"not a JSON value: {type(value).__name__}"
            members = [
                f"{_encode_string(k) if k.isascii() else _quote(k)}:{js_stringify(value[k])}" for k in js_keys(value)
            ]
            return "{" + ",".join(members) + "}"


_SURROGATE = re.compile("[\ud800-\udfff]")


def _quote(text: str) -> str:
    """A JSON string literal as `JSON.stringify` writes it, which escapes a lone surrogate as `\\udXXX`.

    Callers skip it for ASCII text, which needs nothing past the encoder.
    """
    quoted = _encode_string(text)
    if _SURROGATE.search(text) is None:
        return quoted
    # A high surrogate followed by a low one is one character in JavaScript, so it is joined first.
    joined = quoted.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return _SURROGATE.sub(lambda m: f"\\u{ord(m[0]):04x}", joined)


def has_surrogate(text: str) -> bool:
    """Whether `text` holds a surrogate. ASCII text, almost all of it, is answered without the regex."""
    return not text.isascii() and _SURROGATE.search(text) is not None


def to_well_formed(text: str) -> str:
    """`text.toWellFormed()`: lone surrogates replaced by U+FFFD, as encoding a string to UTF-8 does in JavaScript."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _is_array_index(key: str) -> bool:
    return key.isascii() and key.isdigit() and (key == "0" or key[0] != "0") and int(key) < 2**32 - 1


def js_keys(obj: Mapping[str, object]) -> list[str]:
    """`Object.keys(obj)`: array-index keys first in numeric order, then the rest as inserted."""
    for key in obj:
        if type(key) is not str or key[:1].isdigit():
            break
    else:
        return list(obj)
    indexes = sorted((k for k in obj if _is_array_index(k)), key=int)
    return indexes + [k for k in obj if not _is_array_index(k)]
