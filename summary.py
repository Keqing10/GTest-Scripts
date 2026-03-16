from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from utils.case_report import write_case_presence_csv
from utils.gtest_parser import extract_named_cases_by_status
from utils.gtest_parser import parse_summary_from_log_text


"""解析 gtest 日志并生成 summary.csv。

常见修改方式：
1. 使用相对路径：把 DEFAULT_OUTPUT_DIR / DEFAULT_MODE_CSV 改成 Path("output")、Path("mode.csv") 这类写法即可。
2. 更换日志目录：修改 DEFAULT_OUTPUT_DIR，或运行时使用 --output-dir。

这个脚本只依赖 output 目录里的日志文件和 mode.csv，既可以单独执行，
也可以被 run_tests.py 在测试结束后自动调用。
"""


# ===== 可配置项：用户通常只需要修改这些默认值 =====
# SCRIPT_DIR: 当前脚本所在目录。相对路径会以它为基准进行解析，通常不需要修改。
SCRIPT_DIR = Path(__file__).resolve().parent
# DEFAULT_OUTPUT_DIR: 默认的日志目录，summary 会从这里读取 debug/release 日志并输出 summary.csv。
DEFAULT_OUTPUT_DIR = Path("output")
# DEFAULT_MODE_CSV: 模式配置文件，决定汇总哪些日志以及它们在表格中的顺序。
DEFAULT_MODE_CSV = Path("mode.csv")

# ===== 内部常量：输出文件名，不建议修改 =====
LIST_SKIPPED_FILE = "list-skipped.csv"
LIST_FAILED_FILE = "list-failed.csv"


# ===== 数据模型 =====
@dataclass(slots=True)
class SummaryRow:
    """汇总表中的一行，同时也是导出 CSV 的基础数据结构。"""

    test: str
    total: int | str
    passed: int | str
    failed: int | str
    skipped: int | str
    release_total: int | str
    release_passed: int | str
    release_failed: int | str
    release_skipped: int | str
    debug_rate: float | str
    release_rate: float | str
    debug_min: float | str
    release_min: float | str
    debug_ms: int | str
    release_ms: int | str
    count_status: str

    def to_csv_dict(self) -> dict[str, object]:
        return {
            "Test": self.test,
            "DebugTotal": self.total,
            "DebugPassed": self.passed,
            "DebugFailed": self.failed,
            "DebugSkipped": self.skipped,
            "ReleaseTotal": self.release_total,
            "ReleasePassed": self.release_passed,
            "ReleaseFailed": self.release_failed,
            "ReleaseSkipped": self.release_skipped,
            "Debug(min)": self.debug_min,
            "Release(min)": self.release_min,
            "DebugRate": self.debug_rate,
            "ReleaseRate": self.release_rate,
            "Debug(ms)": self.debug_ms,
            "Release(ms)": self.release_ms,
            "CountStatus": self.count_status,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse gtest logs and generate a summary table.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory containing gtest log files. Supports relative paths.")
    parser.add_argument("--mode-csv", default=str(DEFAULT_MODE_CSV), help="CSV file listing test modes. Supports relative paths.")
    parser.add_argument("--quiet", action="store_true", help="Do not print the summary table to stdout.")
    return parser.parse_args(argv)


# ===== 输入读取与日志解析 =====
def load_test_types(mode_csv: Path) -> list[str]:
    """读取 mode.csv 的 fileName 列，决定汇总顺序。"""

    if not mode_csv.exists():
        raise FileNotFoundError(f"mode.csv not found: {mode_csv}")

    with mode_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [row["fileName"].strip() for row in reader if row.get("fileName", "").strip()]


def parse_gtest_log(file_path: Path) -> dict[str, int | float | str]:
    """从单个日志文件中提取 total/passed/failed/skipped/time。"""

    if not file_path.exists():
        return parse_summary_from_log_text("")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    return parse_summary_from_log_text(content)


def parse_named_cases(file_path: Path, status_name: str) -> set[str]:
    """解析整份日志中的 FAILED/SKIPPED 用例名。

    这里只匹配形如 "[  FAILED  ] Test.Name (123 ms)" 的实时结果行，
    这样即使测试被中途打断，也能从已执行部分提取到结果。
    """

    if not file_path.exists():
        return set()

    content = file_path.read_text(encoding="utf-8", errors="replace")
    return extract_named_cases_by_status(content, status_name)


def compare_count_status(debug: dict[str, int | float | str], release: dict[str, int | float | str]) -> str:
    debug_tuple = (debug["Total"], debug["Passed"], debug["Failed"], debug["Skipped"])
    release_tuple = (release["Total"], release["Passed"], release["Failed"], release["Skipped"])

    if "-" in debug_tuple or "-" in release_tuple:
        return "PARTIAL"
    if debug_tuple == release_tuple:
        return "OK"
    return "MISMATCH"


def sum_numeric(values: Iterable[int | float | str]) -> int | float:
    """对混合了 '-' 占位符的数值列求和。"""

    total = 0
    for value in values:
        if value == "-":
            continue
        total += value
    return total


def build_rows(output_dir: Path, mode_csv: Path) -> list[SummaryRow]:
    """按 mode.csv 顺序构造明细行。"""

    rows: list[SummaryRow] = []
    for test_type in load_test_types(mode_csv):
        debug_file = output_dir / f"debug-{test_type}.log"
        release_file = output_dir / f"release-{test_type}.log"

        debug = parse_gtest_log(debug_file)
        release = parse_gtest_log(release_file)

        rows.append(
            SummaryRow(
                test=test_type,
                total=debug["Total"],
                passed=debug["Passed"],
                failed=debug["Failed"],
                skipped=debug["Skipped"],
                release_total=release["Total"],
                release_passed=release["Passed"],
                release_failed=release["Failed"],
                release_skipped=release["Skipped"],
                debug_rate=(round(debug["Passed"]/debug["Total"],5) if isinstance(debug["Total"], int) and debug["Total"]>0 else "-"),
                release_rate=(round(release["Passed"]/release["Total"],5) if isinstance(release["Total"], int) and release["Total"]>0 else "-"),
                debug_min=debug["TimeMin"],
                release_min=release["TimeMin"],
                debug_ms=debug["TimeMs"],
                release_ms=release["TimeMs"],
                count_status=compare_count_status(debug, release),
            )
        )
    return rows


def collect_status_cases(output_dir: Path, mode_csv: Path, status_name: str) -> dict[str, tuple[bool, bool]]:
    """收集指定状态下的用例名，并标记 debug/release 两侧是否出现。"""

    cases: dict[str, tuple[bool, bool]] = {}
    for test_type in load_test_types(mode_csv):
        debug_file = output_dir / f"debug-{test_type}.log"
        release_file = output_dir / f"release-{test_type}.log"

        debug_cases = parse_named_cases(debug_file, status_name)
        release_cases = parse_named_cases(release_file, status_name)

        for case_name in sorted(debug_cases | release_cases):
            cases[case_name] = (case_name in debug_cases, case_name in release_cases)

    return cases


def append_total_row(rows: list[SummaryRow]) -> list[SummaryRow]:
    """追加 TOTAL 汇总行。"""

    total_row = SummaryRow(
        test="TOTAL",
        total=sum_numeric(row.total for row in rows),
        passed=sum_numeric(row.passed for row in rows),
        failed=sum_numeric(row.failed for row in rows),
        skipped=sum_numeric(row.skipped for row in rows),
        release_total=sum_numeric(row.release_total for row in rows),
        release_passed=sum_numeric(row.release_passed for row in rows),
        release_failed=sum_numeric(row.release_failed for row in rows),
        release_skipped=sum_numeric(row.release_skipped for row in rows),
        debug_rate=(round(sum_numeric(row.passed for row in rows)/sum_numeric(row.total for row in rows),2) if sum_numeric(row.total for row in rows) else "-"),
        release_rate=(round(sum_numeric(row.release_passed for row in rows)/sum_numeric(row.release_total for row in rows),2) if sum_numeric(row.release_total for row in rows) else "-"),
        debug_min=round(sum_numeric(row.debug_min for row in rows), 2),
        release_min=round(sum_numeric(row.release_min for row in rows), 2),
        debug_ms=sum_numeric(row.debug_ms for row in rows),
        release_ms=sum_numeric(row.release_ms for row in rows),
        count_status="MISMATCH" if any(row.count_status == "MISMATCH" for row in rows) else "OK",
    )
    return [*rows, total_row]


# ===== 输出展示与导出 =====
def print_table(rows: list[SummaryRow]) -> None:
    """把汇总结果以易读表格形式打印到终端。"""

    print()
    print("Note: Total/Passed/Failed/Skipped columns show debug counts; release counts are checked separately.")
    header = f"{'Test':<24} {'Total':>8} {'Passed':>8} {'Failed':>8} {'Skipped':>8} {'DRate':>8} {'RRate':>8} {'Debug(min)':>12} {'Release(min)':>12} {'Check':>10}"
    separator = "-" * len(header)
    print(header)
    print(separator)

    def format_rate(val: float | str) -> str:
        return f"{val:.5f}" if isinstance(val, float) else str(val)

    for row in rows:
        print(
            f"{row.test:<24} {str(row.total):>8} {str(row.passed):>8} {str(row.failed):>8} "
            f"{str(row.skipped):>8} {format_rate(row.debug_rate):>8} {format_rate(row.release_rate):>8} {str(row.debug_min):>12} {str(row.release_min):>12} {row.count_status:>10}"
        )

    print(separator)

    mismatches = [row for row in rows if row.count_status == "MISMATCH"]
    if mismatches:
        print("Count mismatches detected between debug and release:")
        for row in mismatches:
            print(
                "  "
                f"{row.test}: "
                f"debug={row.total}/{row.passed}/{row.failed}/{row.skipped}, "
                f"release={row.release_total}/{row.release_passed}/{row.release_failed}/{row.release_skipped}"
            )

    print()


def write_csv(rows: list[SummaryRow], output_dir: Path) -> Path:
    """导出 summary.csv，方便后续比对或二次处理。"""

    csv_path = output_dir / "summary.csv"
    try:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "Test",
                    "DebugTotal",
                    "DebugPassed",
                    "DebugFailed",
                    "DebugSkipped",
                    "ReleaseTotal",
                    "ReleasePassed",
                    "ReleaseFailed",
                    "ReleaseSkipped",
                    "DebugRate",
                    "ReleaseRate",
                    "Debug(min)",
                    "Release(min)",
                    "Debug(ms)",
                    "Release(ms)",
                    "CountStatus",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_csv_dict())
    except PermissionError:
        print(f"[WARN] could not write {csv_path}: permission denied (maybe file open)", file=sys.stderr)
    return csv_path


def write_case_list(output_dir: Path, file_name: str, cases: dict[str, tuple[bool, bool]]) -> Path:
    """导出 FAILED/SKIPPED 用例清单 CSV，三列分别是用例名、debug、release。"""

    file_path = output_dir / file_name
    return write_case_presence_csv(file_path, cases)


# ===== 对外入口 =====
def generate_summary(output_dir: Path, mode_csv: Path, emit_console: bool = True) -> tuple[list[SummaryRow], Path]:
    """供命令行和 run_tests.py 复用的汇总入口。"""

    output_dir = (SCRIPT_DIR / output_dir).resolve() if not output_dir.is_absolute() else output_dir
    mode_csv = (SCRIPT_DIR / mode_csv).resolve() if not mode_csv.is_absolute() else mode_csv

    if not output_dir.exists():
        raise FileNotFoundError(f"Directory not found: {output_dir}")

    rows = append_total_row(build_rows(output_dir, mode_csv))
    skipped_cases = collect_status_cases(output_dir, mode_csv, "SKIPPED")
    failed_cases = collect_status_cases(output_dir, mode_csv, "FAILED")
    if emit_console:
        print_table(rows)
    csv_path = write_csv(rows, output_dir)
    skipped_path = write_case_list(output_dir, LIST_SKIPPED_FILE, skipped_cases)
    failed_path = write_case_list(output_dir, LIST_FAILED_FILE, failed_cases)
    if emit_console:
        print(f"CSV saved to: {csv_path}")
        print(f"Skipped cases saved to: {skipped_path}")
        print(f"Failed cases saved to: {failed_path}")
    return rows, csv_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    mode_csv = Path(args.mode_csv)

    try:
        generate_summary(output_dir=output_dir, mode_csv=mode_csv, emit_console=not args.quiet)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())