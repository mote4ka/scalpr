import json
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from scalpr_core.paths import DATA_DIR

PAPER_DIR = DATA_DIR / "paper"


# JOURNAL: what the trader did, json lines in data/paper/<start time>/: fills.jsonl (one line per hedged fill),
# markouts.jsonl (a fill marked again later) and snapshots.jsonl (every coin every 10 s), plus run.json
class Journal:
    def __init__(self, root: Path = PAPER_DIR):
        self.folder = root / f"{datetime.now(UTC):%Y-%m-%d_%H%M%S}"
        self.files: dict[str, IO[str]] = {}

    # run.json: how the run was started and its config (see config.from_dict)
    def write_run(self, run: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "run.json").write_text(json.dumps(run, indent=2, default=_plain), encoding="utf-8")

    def write(self, name: str, record: dict) -> None:
        if name not in self.files:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.files[name] = open(self.folder / f"{name}.jsonl", "a", encoding="utf-8")  # noqa: SIM115 - open for the whole run
        self.files[name].write(json.dumps(record, separators=(",", ":"), default=_plain) + "\n")

    def flush(self) -> None:
        for file in self.files.values():
            file.flush()

    def close(self) -> None:
        for file in self.files.values():
            file.close()
        self.files.clear()


# numpy numbers (prices come out of numpy) as plain python ones
def _plain(value: object) -> object:
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
