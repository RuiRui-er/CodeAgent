import importlib.util, pathlib, sys
p=pathlib.Path(sys.argv[1]); s=importlib.util.spec_from_file_location("m",p/"config.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
assert m.port({}) == 8080
assert m.port({"port": 9000}) == 9000
try: m.port({"port": "bad"})
except ValueError: pass
else: raise AssertionError("invalid port must fail")
