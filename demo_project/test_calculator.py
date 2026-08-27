import unittest

from calculator import divide


class CalculatorTests(unittest.TestCase):
    def test_divide(self):
        self.assertEqual(divide(10, 2), 5)


if __name__ == "__main__":
    unittest.main()
