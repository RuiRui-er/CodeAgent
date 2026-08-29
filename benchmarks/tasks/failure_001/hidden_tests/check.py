import importlib.util, pathlib, sys
p=pathlib.Path(sys.argv[1]); s=importlib.util.spec_from_file_location("m",p/"retry.py"); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
for code in (408,429,500,503,599): assert m.should_retry(code)
for code in (200,400,401,404,422,600): assert not m.should_retry(code)
