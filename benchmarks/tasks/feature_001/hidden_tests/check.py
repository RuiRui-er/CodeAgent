import pathlib, subprocess, sys
p=pathlib.Path(sys.argv[1]); exe=sys.executable
a=subprocess.run([exe,str(p/"cli.py"),"hello"],capture_output=True,text=True)
b=subprocess.run([exe,str(p/"cli.py"),"hello","--uppercase"],capture_output=True,text=True)
assert a.returncode == 0 and a.stdout.strip() == "hello"
assert b.returncode == 0 and b.stdout.strip() == "HELLO"
