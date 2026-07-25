def fibonacci(n: int) -> int:
    """Return the nth Fibonacci number (0-indexed, so fibonacci(0) = 0, fibonacci(1) = 1)."""
    if n < 0:
        raise ValueError("n must be non-negative")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
