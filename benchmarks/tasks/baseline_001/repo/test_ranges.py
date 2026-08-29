import unittest

from ranges import clamp, legacy_label


class RangeTests(unittest.TestCase):
    def test_middle_value(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_known_legacy_failure(self):
        self.assertEqual(legacy_label(), "modern")  # documented pre-existing failure


if __name__ == "__main__":
    unittest.main()
