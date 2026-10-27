import bisect
import json
import statistics
from collections import deque
from pathlib import Path

from scalpr_trader.journal import PAPER_DIR

# the deviation is sampled once a second, as on the backtest grid
SAMPLE = 1_000_000_000  # ns
# the live trader keeps the samples of every coin here, so a restart (a deploy) doesn't warm up again
BASIS_DIR = PAPER_DIR / "basis"


# BASIS: rolling median of the htx vs bybit deviation (%) over `window`, the same as spread.basis in the backtest;
# `value` stays None until `warmup` worth of samples is there
class Basis:
    def __init__(self, window: int, warmup: int):
        self.window, self.needed = window, max(1, warmup // SAMPLE)
        self.samples: deque[tuple[int, float]] = deque()
        self.value: float | None = None

    def add(self, now: int, deviation: float) -> None:
        self.samples.append((now, deviation))
        while self.samples[0][0] <= now - self.window:
            self.samples.popleft()
        self._update()

    # samples of the run before, oldest first: the ones still inside the window count as if this run had taken them
    def restore(self, samples: list[tuple[int, float]], now: int) -> None:
        self.samples.extend(sample for sample in samples if sample[0] > now - self.window)
        self._update()

    # ns until there is a value, if a sample comes every second from `now`: old samples leaving the window meanwhile
    # don't count
    def left(self, now: int) -> int:
        missing = self.needed - len(self.samples)
        if missing <= 0:
            return 0
        times = [t for t, _ in self.samples]
        wait = missing
        while (more := missing + bisect.bisect_right(times, now + wait * SAMPLE - self.window)) > wait:
            wait = more
        return wait * SAMPLE

    def _update(self) -> None:
        self.value = statistics.median(value for _, value in self.samples) if len(self.samples) >= self.needed else None


# the samples of one coin on disk: <folder>/<TICKER>.json, [[t ns, deviation %], ...], replaced whole
def save_samples(basis: Basis, ticker: str, folder: Path = BASIS_DIR) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f"{ticker}.part"
    part.write_text(json.dumps(list(basis.samples), separators=(",", ":")), encoding="utf-8")
    part.replace(folder / f"{ticker}.json")


def load_samples(ticker: str, since: int, folder: Path = BASIS_DIR) -> list[tuple[int, float]]:
    path = folder / f"{ticker}.json"
    if not path.exists():
        return []
    return [(t, deviation) for t, deviation in json.loads(path.read_text(encoding="utf-8")) if t > since]
