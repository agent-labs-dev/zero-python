import pytest

from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.error import get_error_details, get_error_message
from zero_server.shared.js import js_number, js_sort, js_string
from zero_server.shared.json import is_json_value


class Named(Exception):
    error_name = "Error"


class WithMessage(Exception):
    def __init__(self) -> None:
        super().__init__(1, 2)
        self.message = "from attribute"


class WithDetails(Exception):
    details = {"code": 7}  # noqa: RUF012


def test_check() -> None:
    assert check(True, "fine")
    with pytest.raises(AssertionFailedError, match="broken"):
        check(0, "broken")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (True, True),
        (3, True),
        (1.5, True),
        (float("nan"), True),
        (float("inf"), True),
        ("s", True),
        ([1, (2, "x")], True),
        ({"a": [None]}, True),
        ({1: 2}, False),
        ({"a": object()}, False),
        (object(), False),
    ],
)
def test_is_json_value(value: object, expected: bool) -> None:
    assert is_json_value(value) is expected


def test_error_message_from_exceptions() -> None:
    assert get_error_message(Exception("boom")) == "boom"
    assert get_error_message(WithMessage()) == "from attribute"
    assert get_error_message(Exception(1, 2)) == "(1, 2)"


def test_error_message_falls_back_to_the_cause() -> None:
    error = Exception()
    error.__cause__ = ValueError("root")
    assert get_error_message(error) == "root"


def test_error_message_of_an_empty_error_is_its_properties() -> None:
    error = Exception()
    error.__cause__ = Exception()
    assert get_error_message(error) == "Parsed message: {}"
    error = Exception()
    error.code = 5  # ty: ignore[unresolved-attribute]
    assert get_error_message(error) == 'Parsed message: {"code":5}'
    error.code = object()  # ty: ignore[unresolved-attribute]
    assert get_error_message(error) == (
        "Unknown error of type object was thrown and the message could not be determined. See cause for details."
    )


def test_error_message_detects_cycles() -> None:
    error = Exception()
    error.__cause__ = error
    assert get_error_message(error) == "Circular error reference detected while extracting the error message."


def test_error_details() -> None:
    assert get_error_details(Exception("x")) is None
    assert get_error_details(Named()) is None
    assert get_error_details(ValueError()) == {"name": "ValueError"}
    assert get_error_details(WithDetails()) == {"code": 7}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1e21, "1e+21"),
        (0.00125, "0.00125"),
        (1.5e300, "1.5e+300"),
        (2e-7, "2e-7"),
        (-1.25e-9, "-1.25e-9"),
        (0.0, "0"),
        (12.5, "12.5"),
        (100.0, "100"),
        (0.00001, "0.00001"),
        (-0.0001, "-0.0001"),
        (9999999999999998.0, "9999999999999998"),
        (1e16, "10000000000000000"),
        (-7, "-7"),
        (2**53 - 1, "9007199254740991"),
        # Past 2**53 an integer is a double first, as JavaScript would hold it.
        (2**60 + 1, "1152921504606847000"),
    ],
)
def test_js_number(value: float, expected: str) -> None:
    assert js_number(value) == expected


def test_js_string_falls_back_to_str() -> None:
    assert js_string(object) == str(object)
    assert js_string({"a": 1}) == "[object Object]"
    assert js_string([{"a": 1}, [1, None]]) == "[object Object],1,"


def test_js_sort_long_arrays_use_timsort() -> None:
    items = list(range(70, 0, -1))
    assert js_sort(items, lambda a, b: a - b) == sorted(items)
    assert js_sort([1], lambda a, b: 0) == [1]


def test_js_stringify() -> None:
    from zero_server.shared.js import js_stringify

    assert js_stringify({"a": [1.0, True, False, None, "é"], "b": 2.5}) == '{"a":[1,true,false,null,"é"],"b":2.5}'
    assert js_stringify({"b": 1, "10": 2, "2": 3, "01": 4, "": 5}) == '{"2":3,"10":2,"b":1,"01":4,"":5}'
    with pytest.raises(AssertionError, match="not a JSON value"):
        js_stringify(object())
