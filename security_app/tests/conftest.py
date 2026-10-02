"""pytest configuration: put app/ on the import path so tests can import the cdp_security package."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
