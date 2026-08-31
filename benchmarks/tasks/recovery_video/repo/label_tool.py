"""Small CLI used to demonstrate baseline-relative regression recovery."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    args = parser.parse_args()
    print(args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
