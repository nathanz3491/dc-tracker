"""Enables `python -m tracker`, which is the invocation the PRD specifies."""

import sys

from tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
