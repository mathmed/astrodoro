"""Qt interface. `main()` is the GUI entry point."""


def main() -> int:
    from .main import main as _main
    return _main()


__all__ = ["main"]
