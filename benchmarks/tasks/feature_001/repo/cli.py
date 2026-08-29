import argparse


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    args = parser.parse_args(argv)
    print(args.text)


if __name__ == "__main__":
    main()
