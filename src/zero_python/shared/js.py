"""JavaScript's `String(value)`, for the places upstream compares stringified values."""

import json
from collections.abc import Callable
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
        case _:
            return str(value)


def js_number(value: float) -> str:
    """`Number.prototype.toString()`: the shortest round-trip digits, laid out by ECMA-262's rules."""
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
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
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


def js_truthy(value: object) -> bool:
    """JavaScript's truthiness for a JSON value: an empty array or object is truthy there."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value == value and value != 0  # NaN is falsy
    return True


def js_stringify(value: object) -> str:
    """`JSON.stringify(value)` for a JSON value: compact, numbers written as JavaScript writes them."""
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int() | float():
            return js_number(value)
        case str():
            return json.dumps(value, ensure_ascii=False)
        case list() | tuple():
            return "[" + ",".join(js_stringify(item) for item in value) + "]"
        case dict():
            members = (f"{json.dumps(k, ensure_ascii=False)}:{js_stringify(v)}" for k, v in value.items())
            return "{" + ",".join(members) + "}"
        case _:
            raise TypeError(f"not a JSON value: {type(value).__name__}")
