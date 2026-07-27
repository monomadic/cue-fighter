"""Render the design mockup: same real data, three layout directions."""
import json, sys
from pathlib import Path

data = Path(sys.argv[1]).read_text()
tpl = Path(__file__).with_name("mockup_tpl.html").read_text()
out = Path(sys.argv[2])
out.write_text(tpl.replace("/*__DATA__*/null", data))
print(f"wrote {out}  ({out.stat().st_size/1024:.0f} KB)")
