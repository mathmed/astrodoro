#!/usr/bin/env python3
"""Ponto de entrada da interface gráfica.

  .venv/bin/python gui.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
