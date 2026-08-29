import importlib.util, pathlib, sys
p=pathlib.Path(sys.argv[1]); s=importlib.util.spec_from_file_location("m",p/"audit.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
assert m.format_entry("info","login","ok") == "INFO login: ok"
