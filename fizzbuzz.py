def fizzbuzz(n: int) -> str:
    """Return the FizzBuzz string for a positive integer n.

    Rules:
    - Multiple of 3 → "Fizz"
    - Multiple of 5 → "Buzz"
    - Multiple of both → "FizzBuzz"
    - Otherwise → the number as a string
    """
    result = ""
    if n % 3 == 0:
        result += "Fizz"
    if n % 5 == 0:
        result += "Buzz"
    return result or str(n)
