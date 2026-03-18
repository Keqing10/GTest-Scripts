from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Mapping, Sequence


def write_csv_rows(csv_path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> Path:
    """Write a CSV using positional rows and return the target path."""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        writer.writerows(rows)
    return csv_path


def write_csv_dict_rows(
    csv_path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, object]],
) -> Path:
    """Write a CSV using dict rows and return the target path."""
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return csv_path
