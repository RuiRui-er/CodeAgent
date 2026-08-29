import importlib.util
import pathlib
import sys


workspace = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject_parser", workspace / "parser.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.parse_line("") is None
assert module.parse_line("   ") is None
assert module.parse_line("name=alice") == ("name", "alice")
try:
    module.parse_line("name=")
except ValueError:
    pass
else:
    raise AssertionError("malformed non-blank line must still raise ValueError")
print("hidden checks passed")
