import statistics
from collections import deque

# the deviation is sampled once a second, as on the backtest grid
SAMPLE = 1_000_000_000  # ns


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
        self.value = statistics.median(value for _, value in self.samples) if len(self.samples) >= self.needed else None
