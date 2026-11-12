"""Run the validation cases one at a time:  python validation/run_all.py [case ...]
Outputs go to validation/runs/<case>/ (git-ignored); overview images are copied to docs/img/."""
import json, shutil, subprocess, sys, time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
cases = json.loads((root / "validation" / "cases.json").read_text(encoding="utf-8"))
wanted = sys.argv[1:] or list(cases)
(root / "validation" / "results").mkdir(exist_ok=True)
(root / "docs" / "img").mkdir(parents=True, exist_ok=True)
for key in wanted:
    c = cases[key]
    out = root / "validation" / "runs" / key
    log = root / "validation" / "runs" / f"{key}.log"
    cmd = [sys.executable, "-m", "earthdiff", "run", "--name", c["name"], "--out", str(out), *c["args"]]
    t0 = time.time()
    print(f"== {key}: {' '.join(c['args'])}", flush=True)
    try:
        with open(log, "w", encoding="utf-8") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=1500, cwd=root)
        status = r.returncode
    except subprocess.TimeoutExpired:
        status = "timeout"
    print(f"   exit={status} in {time.time()-t0:.0f}s", flush=True)
    if status == 0:
        shutil.copy(out / "overview.png", root / "docs" / "img" / f"{key}_overview.png")
        shutil.copy(out / "summary.json", root / "validation" / "results" / f"{key}.summary.json")
