import unittest

from parser import parse_line


class ParserTests(unittest.TestCase):
    def test_valid_line(self):
        self.assertEqual(parse_line("name=alice"), ("name", "alice"))

    def test_missing_value(self):
        with self.assertRaises(ValueError):
            parse_line("name=")


if __name__ == "__main__":
    unittest.main()
