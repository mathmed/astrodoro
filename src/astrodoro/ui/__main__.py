"""`python -m astrodoro.ui` — same entry point as the `astrodoro-gui` script."""
import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
