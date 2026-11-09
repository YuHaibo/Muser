#!/usr/bin/env python3
"""Launch with the bundled SDK without changing global Python installations."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root / "vendor"), str(root / "vendor/python"), str(root / "backend")]
from muser.worker import main

if __name__ == "__main__":
    main()
