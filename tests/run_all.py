"""Run every offline test (python tests/run_all.py). Each test is a plain script that asserts and prints OK."""

import pathlib
import subprocess
import sys

here = pathlib.Path(__file__).parent
failed = []
for test in sorted(here.glob("test_*.py")):
    run = subprocess.run([sys.executable, "-X", "utf8", str(test)], capture_output=True, text=True, encoding="utf-8")
    last = (run.stdout.strip().splitlines() or [""])[-1]
    print(f"{'ok  ' if run.returncode == 0 else 'FAIL'} {test.name}: {last if run.returncode == 0 else run.stderr.strip().splitlines()[-1]}")
    if run.returncode:
        failed.append(test.name)
sys.exit(1 if failed else 0)
