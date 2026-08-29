import importlib.util
import pathlib
import sys


workspace = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject_ranges", workspace / "ranges.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.clamp(-3, 0, 10) == 0
assert module.clamp(5, 0, 10) == 5
assert module.clamp(30, 0, 10) == 10
assert module.legacy_label() == "legacy", "unrelated baseline behavior changed"
print("hidden checks passed")
