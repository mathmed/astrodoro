"""`python -m astrodoro` — the command line interface.

The console script installed by pyproject is `astrodoro`; this exists so the
package also runs from a checkout with no install step, which is what the
Makefile targets rely on.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
