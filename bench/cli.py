"""Command-line entry point for the inference benchmarker."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    return argparse.ArgumentParser(
        prog="python -m bench",
        description="Concurrent inference benchmarker for Kube Inference Platform.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse command-line arguments and show the v0.1 foundation help."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
