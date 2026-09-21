"""``python -m curation.cli`` - same as the ``curation`` console script."""
from __future__ import annotations

if __name__ == "__main__":
    from . import main

    raise SystemExit(main())
