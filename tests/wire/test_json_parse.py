"""Python reads a request body as upstream's `await request.json()` does on Node, refusals worded as V8's."""

import base64
import json
import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import as_js, json_values
from zero_server.shared.json_parse import JSONSyntaxError, parse_body, parse_json

# Characters JSON's grammar turns on, and some it does not know.
_SIGNIFICANT = [*'{}[]:,"\\-+.0123456789eEtrufalsn \t\r\n', "\x00", "\x1f", "é", "€", "﻿", "😀", "\ud800"]


@st.composite
def near_json(draw: st.DrawFn) -> str:
    """A JSON text, often with an edit or three: a character dropped, inserted or replaced."""
    text = json.dumps(draw(json_values), ensure_ascii=draw(st.booleans()), indent=draw(st.sampled_from([None, 1])))
    if draw(st.booleans()):
        text = draw(st.sampled_from(["", " ", "\r\n"])) + text + draw(st.sampled_from(["", " ", "\n"]))
    for _ in range(draw(st.integers(0, 3))):
        at = draw(st.integers(0, len(text)))
        char = draw(st.sampled_from(_SIGNIFICANT) | st.characters(codec=None))
        match draw(st.sampled_from(["drop", "insert", "replace"])):
            case "drop":
                text = text[:at] + text[at + 1 :]
            case "insert":
                text = text[:at] + char + text[at:]
            case _:
                text = text[:at] + char + text[at + 1 :]
    return text


def _tag_non_finite(value: Any) -> Any:
    match value:
        case float() if math.isinf(value):
            return {"$nonFinite": "Infinity" if value > 0 else "-Infinity"}
        case list():
            return [_tag_non_finite(v) for v in value]
        case dict():
            return {k: _tag_non_finite(v) for k, v in value.items()}
        case _:
            return value


def _python(parse: Any, value: Any) -> dict[str, Any]:
    try:
        return {"value": as_js(_tag_non_finite(parse(value)))}
    except JSONSyntaxError as error:
        return {"error": str(error)}


@given(near_json() | st.sampled_from(["NaN", "Infinity", "-Infinity", "[object Object]", "undefined", "1e400"]))
def test_parses_as_json_parse(harness: Harness, text: str) -> None:
    expected = harness.call("jsonParse", text=text)
    if "value" in expected:
        expected["value"] = as_js(expected["value"])
    assert _python(parse_json, text) == expected


# A UTF-8 byte order mark, bytes that do not decode, and the UTF-16 Python's `json` would otherwise detect.
_PREFIXES = [b"", b"\xef\xbb\xbf", b"\xef\xbb\xbf\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"]
_JUNK = [b"\x80", b"\xc3", b"\xe2\x82", b"\xf0\x9f\x98", b"\xed\xa0\x80", b"\xc0\xaf", b"\xff"]


@given(
    st.sampled_from(_PREFIXES), near_json(), st.lists(st.tuples(st.integers(0, 50), st.sampled_from(_JUNK)), max_size=2)
)
def test_reads_bodies_as_request_json(
    harness: Harness, prefix: bytes, text: str, junk: list[tuple[int, bytes]]
) -> None:
    body = text.encode("utf-8", "surrogatepass")
    for at, chunk in junk:
        body = body[:at] + chunk + body[at:]
    body = prefix + body
    expected = harness.call("requestJSON", body=base64.b64encode(body).decode())
    if "value" in expected:
        expected["value"] = as_js(expected["value"])
    else:
        assert expected.pop("name") == "SyntaxError"
    assert _python(parse_body, body) == expected


# One text per way V8 refuses JSON, each at a different place in the parser.
PINNED = [
    "", "  ", "x", "{", "{x", "{1:2}", "{,}", "[1,", "[1 2]", "[1;2]", "[1,]", "[,]", '{"a" 1}', '{"a"', '{"a":1 "b"}',
    '{"a":}', '{"a":1,}', '{"a":1,', '{"a":1', '{"a":1}}', '{"a":1}x', '"abc', '"\\x"', '"\\', '"\\€"', '"\\u12"',
    '"\\u12', '"a\x01"', '"\t"', "-", "-x", "-01", "01", "0-1", "1.", "1.e", "1e", "1e+", "1 2", "tru", "trux", "tr1e",
    'tr"', "nul", "nulx", "NaN", "undefined", "Infinity", "-Infinity", "[object Object]", "\ufeff{}", "\r\n\r\n  x",
    "\r\r\nx", "[\n1,\n2,\nx]", "😀", "abcdefghijklmnopqrstuvwxyz", "[1,2,3,4,5,6,7,8,9,10,11,x,12,13,14,15]",
    "123456789012345678901234567890x", '"\\n\\u0041', "tr ue", "nul\tl",
    "aaaaaaaaaa😀bbbbbbbbbbbbbbbbbbbbbbbb", '"\\ud800"', "[1e400, -1e400]",
]  # fmt: skip


@pytest.mark.parametrize("text", PINNED)
def test_pinned_texts_parse_as_json_parse(harness: Harness, text: str) -> None:
    expected = harness.call("jsonParse", text=text)
    if "value" in expected:
        expected["value"] = as_js(expected["value"])
    assert _python(parse_json, text) == expected
