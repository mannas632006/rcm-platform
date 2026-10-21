"""`python -m tools.synthetic` -- generate the synthetic corpus.

Thin wrapper so `make seed` has a stable entry point that does not depend on
which file inside the package currently owns `main()`.
"""

from __future__ import annotations

import sys

from .emit import main

if __name__ == "__main__":
    sys.exit(main())
