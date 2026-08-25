"""`python -m astrodoro.cli` — same entry point as the `astrodoro` script."""
import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
