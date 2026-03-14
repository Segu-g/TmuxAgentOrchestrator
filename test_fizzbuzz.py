"""
Specification tests for fizzbuzz.

Rules:
  - For multiples of 3: return "Fizz"
  - For multiples of 5: return "Buzz"
  - For multiples of both 3 and 5: return "FizzBuzz"
  - For all other numbers: return the number as a string
"""
import pytest
from fizzbuzz import fizzbuzz


def test_returns_fizz_for_multiple_of_3():
    assert fizzbuzz(3) == "Fizz"
    assert fizzbuzz(6) == "Fizz"
    assert fizzbuzz(9) == "Fizz"


def test_returns_buzz_for_multiple_of_5():
    assert fizzbuzz(5) == "Buzz"
    assert fizzbuzz(10) == "Buzz"
    assert fizzbuzz(20) == "Buzz"


def test_returns_fizzbuzz_for_multiple_of_both_3_and_5():
    assert fizzbuzz(15) == "FizzBuzz"
    assert fizzbuzz(30) == "FizzBuzz"
    assert fizzbuzz(45) == "FizzBuzz"


def test_returns_number_as_string_for_non_multiples():
    assert fizzbuzz(1) == "1"
    assert fizzbuzz(2) == "2"
    assert fizzbuzz(4) == "4"
    assert fizzbuzz(7) == "7"
    assert fizzbuzz(11) == "11"


def test_fizzbuzz_at_boundary_1():
    assert fizzbuzz(1) == "1"


def test_fizzbuzz_at_boundary_100():
    assert fizzbuzz(100) == "Buzz"


def test_fizzbuzz_prioritises_fizzbuzz_over_fizz_and_buzz():
    # 15 is divisible by both 3 and 5 — must be "FizzBuzz", not "Fizz" or "Buzz"
    result = fizzbuzz(15)
    assert result == "FizzBuzz", f"Expected 'FizzBuzz' for 15, got '{result}'"


def test_returns_string_not_int():
    result = fizzbuzz(1)
    assert isinstance(result, str), f"Expected str, got {type(result)}"


def test_fizzbuzz_sequence_1_to_20():
    expected = [
        "1", "2", "Fizz", "4", "Buzz",
        "Fizz", "7", "8", "Fizz", "Buzz",
        "11", "Fizz", "13", "14", "FizzBuzz",
        "16", "17", "Fizz", "19", "Buzz",
    ]
    actual = [fizzbuzz(n) for n in range(1, 21)]
    assert actual == expected
