import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        from astrodoro.cli import main
    else:
        from astrodoro.ui import main
    sys.exit(main())
