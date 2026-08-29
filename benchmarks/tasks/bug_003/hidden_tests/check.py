import importlib.util, json, pathlib, sys
p=pathlib.Path(sys.argv[1]); s=importlib.util.spec_from_file_location("m",p/"records.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
assert m.decode_json("  ") == {}
assert m.decode_json('{"a": 1}') == {"a": 1}
try: m.decode_json("{")
except json.JSONDecodeError: pass
else: raise AssertionError("invalid JSON must fail")
