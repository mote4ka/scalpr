import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from scalpr_core.paths import DATA_DIR

STREAM_DIR = DATA_DIR / "stream"
HOUR = 3_600_000_000_000  # ns


# RECORDER: raw websocket messages with the local receive time, one gzip json-lines file per exchange and UTC hour:
# data/stream/<exchange>/<YYYY-MM-DD>/<HH>.jsonl.gz, a line is {"r": received ns, "m": message}
# replaying them through the same feed rebuilds the books exactly (see replay)
class Recorder:
    def __init__(self, root: Path = STREAM_DIR):
        self.root = root
        self.files: dict[str, tuple[int, IO[str]]] = {}

    def write(self, exchange: str, received: int, message: dict) -> None:
        hour = received // HOUR
        current = self.files.get(exchange)
        if current is None or current[0] != hour:
            if current is not None:
                current[1].close()
            started = datetime.fromtimestamp(hour * HOUR / 1e9, UTC)
            path = self.root / exchange / f"{started:%Y-%m-%d}" / f"{started:%H}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            # append: a restart within the hour adds a new gzip member, readers see one stream
            current = self.files[exchange] = (hour, gzip.open(path, "at", encoding="utf-8"))  # noqa: SIM115 - open for the whole hour
        current[1].write(json.dumps({"r": received, "m": message}, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        for _, file in self.files.values():
            file.flush()

    def close(self) -> None:
        for _, file in self.files.values():
            file.close()
        self.files.clear()


# messages of recorded files in order: (received ns, message)
def recorded(paths: list[Path]) -> Iterator[tuple[int, dict]]:
    for path in sorted(paths):
        with gzip.open(path, "rt", encoding="utf-8") as file:
            for line in file:
                # a line cut by a crash is the last one of its member
                if line.endswith("\n"):
                    record = json.loads(line)
                    yield record["r"], record["m"]
