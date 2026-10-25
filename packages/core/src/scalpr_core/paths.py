import os
from pathlib import Path

def _find_root() -> Path:
    # uv.lock exists only in the workspace root
    for parent in Path(__file__).resolve().parents:
        if (parent / "uv.lock").exists():
            return parent
    raise RuntimeError("Workspace root not found, set SCALPR_ROOT")

PROJECT_ROOT = Path(os.environ["SCALPR_ROOT"]) if "SCALPR_ROOT" in os.environ else _find_root()

DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = DATA_DIR / "logs"


for path in (LOGS_DIR, DATA_DIR):
    path.mkdir(parents=True, exist_ok=True)