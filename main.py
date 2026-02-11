"""
Thin entry point at the repo root – required by Buildozer which expects
main.py in source.dir.  Delegates to app.main.
"""

import os
import sys

# Ensure the repo root is on the path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
