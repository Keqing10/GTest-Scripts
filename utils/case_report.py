from __future__ import annotations

import csv
from pathlib import Path


def write_case_presence_csv(file_path: Path, cases: dict[str, tuple[bool, bool]]) -> Path:
    """导出 case_name/debug/release 三列的 Y/N 标记清单。"""

    with file_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_name", "debug", "release"])
        for case_name in sorted(cases):
            debug_flag, release_flag = cases[case_name]
            writer.writerow([case_name, "Y" if debug_flag else "N", "Y" if release_flag else "N"])
    return file_path