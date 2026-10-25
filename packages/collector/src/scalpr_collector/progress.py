from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Lock
from typing import IO

from rich.markup import escape
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressBar,
    Task,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from scalpr_core.logger import console

# row color per job kind, used for the label and the bar
COLORS = {"download": "cyan", "parse": "magenta"}


# bar in the task's own color instead of one style for all rows
class ColoredBarColumn(BarColumn):
    def render(self, task: Task) -> ProgressBar:
        bar = super().render(task)
        bar.complete_style = bar.finished_style = task.fields["color"]
        return bar


# one live display for the whole process: rich allows only one at a time, parallel jobs get a row each
# logger's console: log lines are printed above the bars instead of breaking them
progress = Progress(
    TextColumn("{task.description}"),
    ColoredBarColumn(),
    DownloadColumn(),
    TransferSpeedColumn(),
    TimeRemainingColumn(),
    console=console,
    transient=True,
)
_active = 0
_lock = Lock()


# TRACK: progress row for one job (kind is a COLORS key), display starts with the first job and stops after the last
# yields advance(n_bytes), thread safe (parsing runs in asyncio.to_thread)
@contextmanager
def track(kind: str, name: str, total: int | None) -> Iterator[Callable[[int], None]]:
    global _active
    with _lock:
        if _active == 0:
            progress.start()
        _active += 1
    color = COLORS[kind]
    task = progress.add_task(f"[{color}]{kind:<8}[/] {escape(name)}", total=total, color=color)
    try:
        yield lambda n: progress.advance(task, n)
    finally:
        progress.remove_task(task)
        with _lock:
            _active -= 1
            if _active == 0:
                progress.stop()


# TRACK LINES: lines of a binary file with a "parse" progress row
# advanced once per MB, updating on every line slows parsing down noticeably
def track_lines(file: IO[bytes], name: str, total: int) -> Iterator[bytes]:
    with track("parse", name, total) as advance:
        read = 0
        for line in file:
            yield line
            read += len(line)
            if read >= 1 << 20:
                advance(read)
                read = 0
