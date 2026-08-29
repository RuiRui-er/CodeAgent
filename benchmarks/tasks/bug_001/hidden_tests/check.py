import importlib.util
import pathlib
import sys


workspace = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("subject_stats", workspace / "stats.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.average([]) == 0, "empty average must be 0"
assert module.average([3, 6, 9]) == 6, "non-empty behavior regressed"
print("hidden checks passed")
