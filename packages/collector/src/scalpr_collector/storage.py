from pathlib import Path

from pandas import DataFrame


# WRITE PARQUET: .part first, so an interrupted write never looks like a finished file
def write_parquet(frame: DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    frame.to_parquet(part, compression="zstd")
    part.replace(path)
    return path
