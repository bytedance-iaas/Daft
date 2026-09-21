"""v2 tests (the v1 suite stays inside the package, backend/curation/tests)."""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BACKEND)
for path in (BACKEND, os.path.join(REPO, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)
