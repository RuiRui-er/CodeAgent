def parse_line(line):
    key, value = line.split("=", 1)
    if not key or not value:
        raise ValueError("key and value are required")
    return key.strip(), value.strip()
