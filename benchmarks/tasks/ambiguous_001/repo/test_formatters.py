import unittest

from formatters import format_code, format_title


class FormatterTests(unittest.TestCase):
    def test_both_trim(self):
        self.assertEqual(format_title(" hello world "), "hello world")
        self.assertEqual(format_code(" xYz "), "xYz")


if __name__ == "__main__":
    unittest.main()
