"""Test package for the source backend and native app."""
from pathlib import Path
import sys

# The app bundles these modules together; tests import the same flat backend.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
