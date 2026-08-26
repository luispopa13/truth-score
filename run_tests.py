"""Manual test runner (no pytest installed)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "truthscore-backend"))
sys.path.insert(0, os.path.dirname(__file__))

from tests import test_core as t
from tests import test_integration as ti

fn = [n for n in dir(t) if n.startswith("test_")]
fn += [n for n in dir(ti) if n.startswith("test_")]

passed = 0
for name in fn:
    func = getattr(ti, name, None) or getattr(t, name)
    try:
        func()
        print(f"PASS {name}")
        passed += 1
    except Exception as e:
        print(f"FAIL {name}: {e!r}")

print(f"\n{passed}/{len(fn)} passed")
sys.exit(0 if passed == len(fn) else 1)