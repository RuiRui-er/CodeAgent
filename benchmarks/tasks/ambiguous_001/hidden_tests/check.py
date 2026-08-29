import importlib.util
import pathlib
import sys


workspace = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject_formatters", workspace / "formatters.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.format_title(" hello WORLD ") == "Hello World"
assert module.format_code(" xYz ") == "xYz", "format_code was changed"
print("hidden checks passed")
