import unittest

from stats import average


class AverageTests(unittest.TestCase):
    def test_non_empty_values(self):
        self.assertEqual(average([2, 4, 6]), 4)


if __name__ == "__main__":
    unittest.main()
