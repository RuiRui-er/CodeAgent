def divide(total, count):
    """Return the average amount for each item."""
    return count / total  # Bug: numerator and denominator are reversed.


if __name__ == "__main__":
    print(divide(10, 2))
