"""Small CSV CLI used by the two-minute CodeAgent demonstration."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_row(row: dict[str, str]) -> tuple[str, int]:
    return row["name"], int(row["age"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    args = parser.parse_args()

    with Path(args.csv_file).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name, age = parse_row(row)
            print(f"{name},{age}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
