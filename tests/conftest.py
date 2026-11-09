import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "vendor"), str(ROOT / "vendor/python")]
os.environ["PYTHONPATH"] = os.pathsep.join(sys.path[:3])
