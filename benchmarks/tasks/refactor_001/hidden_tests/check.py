import pathlib, sys
p=pathlib.Path(sys.argv[1]); sys.path.insert(0,str(p))
import customer, supplier, names
assert names.normalize_name(" Acme Corp ") == "acme-corp"
assert customer.customer_key(" Acme Corp ") == "acme-corp"
assert supplier.supplier_key(" Blue Sky ") == "blue-sky"
assert "normalize_name" in pathlib.Path(p/"customer.py").read_text() and "normalize_name" in pathlib.Path(p/"supplier.py").read_text()
