from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


"""独立脚本：按“每次一个用例”的方式并行运行 gtest。

核心流程：
1. 先列出全部测例并写入 list.log；
2. 根据 full/partial 选出本次待测列表；
3. 在 debug/release 下并行执行；
4. 合并并更新 case_results.csv；
5. 额外导出 list-skipped.csv 与 list-failed.csv。

所有相对路径都会以当前脚本所在目录为基准解析。
"""


# ===== 可配置默认值（文件头配置） =====
# SCRIPT_DIR: 当前脚本所在目录。相对路径都会以它为基准解析，通常不需要修改。
SCRIPT_DIR = Path(__file__).resolve().parent
# DEFAULT_OUTPUT_DIR: 默认输出目录，存放 list.log 和结果 CSV。
DEFAULT_OUTPUT_DIR = Path("output-part")
# DEFAULT_RESULT_CSV: 默认结果 CSV 文件名，包含每个用例的 debug/release 结果。
DEFAULT_RESULT_CSV = "case_results.csv"
# DEFAULT_LIST_LOG: 默认测例列表文件名，保存列出的全部测例名。
DEFAULT_LIST_LOG = "list.log"
# DEFAULT_LIST_SKIPPED_CSV: 默认跳过用例清单文件名（和 run_tests 风格一致）。
DEFAULT_LIST_SKIPPED_CSV = "list-skipped.csv"
# DEFAULT_LIST_FAILED_CSV: 默认失败用例清单文件名（和 run_tests 风格一致）。
DEFAULT_LIST_FAILED_CSV = "list-failed.csv"
# DEFAULT_WORKERS: 默认并行线程数（每个线程一次只跑一个测例）。
DEFAULT_WORKERS = 6
# DEFAULT_TEST_MODE: 默认测试范围。full=全量；partial=只重测未双通过用例。
DEFAULT_TEST_MODE = "full"  # full | partial
# DEFAULT_RUN_MODE: 默认构建模式。both/debug/release。
DEFAULT_RUN_MODE = "both"  # both | debug | release
# DEFAULT_ENABLE_PROGRESS: 是否默认启用终端进度条。
DEFAULT_ENABLE_PROGRESS = True
# DEFAULT_PROGRESS_REFRESH_SEC: 进度条刷新间隔（秒），数值越大占用越低。
DEFAULT_PROGRESS_REFRESH_SEC = 1
# DEFAULT_PROGRESS_BAR_WIDTH: 进度条宽度。
DEFAULT_PROGRESS_BAR_WIDTH = 20

# DEFAULT_DEBUG_EXE: Debug 版 tests.exe 默认路径。支持绝对路径或相对路径。
DEFAULT_DEBUG_EXE = Path(r"Debug/tests.exe")
# DEFAULT_RELEASE_EXE: Release 版 tests.exe 默认路径。支持绝对路径或相对路径。
DEFAULT_RELEASE_EXE = Path(r"Release/tests.exe")


# ===== 内部常量：gtest 列表解析规则，不建议修改 =====
SUITE_RE = re.compile(r"^(\S+)\.$")
CASE_RE = re.compile(r"^\s{2}(\S.*)$")
COMMENT_RE = re.compile(r"^\s{2}#")
SPINNER_FRAMES = ("|", "/", "-", "\\")
FAILED_CASE_RE = re.compile(r"^\[\s+FAILED\s+\]\s+")
SKIPPED_CASE_RE = re.compile(r"^\[\s+SKIPPED\s+\]\s+")
PASSED_CASE_RE = re.compile(r"^\[\s+OK\s+\]\s+")


@dataclass(slots=True)
class CaseResult:
    case_name: str
    debug_pass: str
    release_pass: str


# ===== 命令行参数与路径处理 =====
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数：控制测试范围、构建模式、并发度和输出位置。"""
    parser = argparse.ArgumentParser(description="Run gtest one case per process in parallel.")
    parser.add_argument(
        "--mode",
        choices=("full", "partial"),
        default=DEFAULT_TEST_MODE,
        help="full: 测全部用例；partial: 只测结果 CSV 中未全部通过的用例。",
    )
    parser.add_argument(
        "--run-mode",
        choices=("both", "debug", "release"),
        default=DEFAULT_RUN_MODE,
        help="选择测试 debug/release/both。",
    )
    parser.add_argument("--debug-exe", default=str(DEFAULT_DEBUG_EXE), help="Debug tests.exe 路径。")
    parser.add_argument("--release-exe", default=str(DEFAULT_RELEASE_EXE), help="Release tests.exe 路径。")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="输出目录（含 list.log 和结果 CSV）。")
    parser.add_argument("--result-csv", default=DEFAULT_RESULT_CSV, help="结果 CSV 文件名或路径。")
    parser.add_argument("--list-log", default=DEFAULT_LIST_LOG, help="测例清单文件名或路径。")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并行线程数。")
    parser.add_argument("--no-progress", action="store_true", help="关闭终端进度条，改为仅输出关键日志。")
    return parser.parse_args(argv)


def resolve_path(path_text: str) -> Path:
    # 相对路径统一按脚本目录解析，避免受当前终端 cwd 影响。
    path = Path(path_text)
    if path.is_absolute():
        return path
    return (SCRIPT_DIR / path).resolve()


def resolve_under_output(path_text: str, output_dir: Path) -> Path:
    # 结果文件未给绝对路径时，固定落在 output_dir 下。
    path = Path(path_text)
    if path.is_absolute():
        return path
    return output_dir / path


# ===== gtest 测例枚举与执行 =====
def list_cases(exe_path: Path) -> list[str]:
    """调用 --gtest_list_tests 并解析成完整用例名（Suite.Case）。"""
    completed = subprocess.run(
        [str(exe_path), "--gtest_list_tests"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"列出测例失败: {exe_path}\n{completed.stdout}\n{completed.stderr}")

    suite_name = ""
    cases: list[str] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue

        suite_match = SUITE_RE.match(line.strip())
        if suite_match:
            suite_name = suite_match.group(1)
            continue

        if COMMENT_RE.match(line):
            continue

        case_match = CASE_RE.match(line)
        if case_match and suite_name:
            case_leaf = case_match.group(1).split("#", 1)[0].strip()
            if case_leaf:
                cases.append(f"{suite_name}.{case_leaf}")

    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for name in cases:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def write_case_list_log(list_log: Path, cases: list[str]) -> None:
    with list_log.open("w", encoding="utf-8", newline="") as handle:
        for case_name in cases:
            handle.write(case_name)
            handle.write("\n")


def run_one_case(exe_path: Path, case_name: str) -> str:
    # 每个子进程只跑 1 个用例，满足“单测粒度并行”。
    completed = subprocess.run(
        [str(exe_path), f"--gtest_filter={case_name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    # 单用例模式下优先以用例状态行判定，避免把 SKIPPED 误归类为 FAIL。
    output_text = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if FAILED_CASE_RE.search(output_text):
        return "FAIL"
    if SKIPPED_CASE_RE.search(output_text):
        return "SKIPPED"
    if PASSED_CASE_RE.search(output_text):
        return "PASS"

    return "PASS" if completed.returncode == 0 else "FAIL"


def render_progress_bar(done: int, total: int, width: int = DEFAULT_PROGRESS_BAR_WIDTH) -> str:
    if total <= 0:
        return "-" * width
    filled = min(width, int((done / total) * width))
    return "#" * filled + "-" * (width - filled)


def format_elapsed(start_time: float) -> str:
    elapsed = max(0, int(time.time() - start_time))
    minutes, seconds = divmod(elapsed, 60)
    return f"{minutes:02d}:{seconds:02d}"


def print_progress_line(status: str, previous_len: int, interactive: bool) -> int:
    if interactive:
        if previous_len > 0:
            sys.stdout.write("\r" + (" " * previous_len) + "\r")
        sys.stdout.write("\r" + status)
        sys.stdout.flush()
        return len(status)

    print(status)
    return len(status)


def run_cases_parallel(
    exe_path: Path,
    cases: list[str],
    workers: int,
    label: str,
    show_progress: bool,
) -> dict[str, str]:
    """并行执行用例，返回 {case_name: PASS/FAIL/SKIPPED}。"""
    if not cases:
        return {}

    total = len(cases)
    results: dict[str, str] = {}
    lock = threading.Lock()
    done = 0
    passed_count = 0
    failed_count = 0
    skipped_count = 0
    running = 0

    interactive = show_progress and sys.stdout.isatty()
    progress_enabled = show_progress
    stop_event = threading.Event()
    start_time = time.time()

    def task(case_name: str) -> tuple[str, str]:
        nonlocal running
        with lock:
            running += 1
        try:
            return case_name, run_one_case(exe_path, case_name)
        finally:
            with lock:
                running -= 1

    def progress_loop() -> None:
        previous_len = 0
        frame_index = 0
        while not stop_event.is_set():
            with lock:
                done_snapshot = done
                pass_snapshot = passed_count
                fail_snapshot = failed_count
                skipped_snapshot = skipped_count
                running_snapshot = running

            spinner = SPINNER_FRAMES[frame_index % len(SPINNER_FRAMES)]
            frame_index += 1
            bar = render_progress_bar(done_snapshot, total)
            status = (
                f"[{label}] {spinner} [{bar}] {done_snapshot}/{total} "
                f"run={running_snapshot} pass={pass_snapshot} fail={fail_snapshot} skip={skipped_snapshot} "
                f"t={format_elapsed(start_time)}"
            )
            previous_len = print_progress_line(status, previous_len, interactive)
            stop_event.wait(DEFAULT_PROGRESS_REFRESH_SEC)

        # 收尾刷新到 100%，并换行固定最终结果。
        with lock:
            done_snapshot = done
            pass_snapshot = passed_count
            fail_snapshot = failed_count
            skipped_snapshot = skipped_count
        final_bar = render_progress_bar(done_snapshot, total)
        final_line = (
            f"[{label}] done [{final_bar}] {done_snapshot}/{total} "
            f"pass={pass_snapshot} fail={fail_snapshot} skip={skipped_snapshot} t={format_elapsed(start_time)}"
        )
        if interactive and previous_len > 0:
            sys.stdout.write("\r" + (" " * previous_len) + "\r")
        print(final_line)

    progress_thread: threading.Thread | None = None
    if progress_enabled:
        progress_thread = threading.Thread(target=progress_loop, name=f"progress-{label}", daemon=True)
        progress_thread.start()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(task, case_name): case_name for case_name in cases}
        for future in as_completed(future_map):
            case_name, status = future.result()
            with lock:
                done += 1
                if status == "PASS":
                    passed_count += 1
                elif status == "SKIPPED":
                    skipped_count += 1
                else:
                    failed_count += 1
            results[case_name] = status

    if progress_enabled:
        stop_event.set()
        if progress_thread is not None:
            progress_thread.join(timeout=2)
    else:
        print(
            f"[{label}] done {total}/{total} pass={passed_count} fail={failed_count} "
            f"skip={skipped_count} t={format_elapsed(start_time)}"
        )

    return results


# ===== CSV 读取、筛选与合并 =====
def load_existing_results(csv_path: Path) -> dict[str, CaseResult]:
    # partial 模式依赖历史结果来决定重测集合。
    if not csv_path.exists():
        return {}

    existing: dict[str, CaseResult] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            case_name = row.get("case_name", "").strip()
            if not case_name:
                continue
            debug_pass = normalize_pass_value(row.get("debug_pass", ""))
            release_pass = normalize_pass_value(row.get("release_pass", ""))
            existing[case_name] = CaseResult(case_name, debug_pass, release_pass)
    return existing


def normalize_pass_value(value: str | None) -> str:
    # 兼容旧 CSV 里可能存在的多种真假值写法。
    text = (value or "").strip().upper()
    if text in {"PASS", "TRUE", "1", "Y", "YES"}:
        return "PASS"
    if text in {"SKIPPED", "SKIP", "S"}:
        return "SKIPPED"
    if text in {"FAIL", "FALSE", "0", "N", "NO"}:
        return "FAIL"
    return "UNKNOWN"


def merge_results(
    all_cases: list[str],
    existing: dict[str, CaseResult],
    debug_updates: dict[str, str] | None,
    release_updates: dict[str, str] | None,
) -> list[CaseResult]:
    """把本次执行结果覆盖到历史结果上，未执行的列保持原值。"""
    rows: list[CaseResult] = []
    for case_name in all_cases:
        old = existing.get(case_name, CaseResult(case_name, "UNKNOWN", "UNKNOWN"))
        debug_pass = old.debug_pass
        release_pass = old.release_pass

        if debug_updates is not None and case_name in debug_updates:
            debug_pass = debug_updates[case_name]
        if release_updates is not None and case_name in release_updates:
            release_pass = release_updates[case_name]

        rows.append(CaseResult(case_name, debug_pass, release_pass))
    return rows


def select_cases_for_partial(all_cases: list[str], existing: dict[str, CaseResult]) -> list[str]:
    # 仅跳过“debug/release 均 PASS”的用例，其余都进入重测集合。
    target: list[str] = []
    for case_name in all_cases:
        item = existing.get(case_name)
        if not item:
            target.append(case_name)
            continue
        if item.debug_pass == "PASS" and item.release_pass == "PASS":
            continue
        target.append(case_name)
    return target


def write_result_csv(csv_path: Path, rows: list[CaseResult]) -> None:
    # 输出固定三列：case_name, debug_pass, release_pass。
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_name", "debug_pass", "release_pass"])
        for row in rows:
            writer.writerow([row.case_name, row.debug_pass, row.release_pass])


def collect_case_flags(rows: list[CaseResult], status_name: str) -> dict[str, tuple[bool, bool]]:
    cases: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        debug_hit = row.debug_pass == status_name
        release_hit = row.release_pass == status_name
        if debug_hit or release_hit:
            cases[row.case_name] = (debug_hit, release_hit)
    return cases


def write_case_list(csv_path: Path, cases: dict[str, tuple[bool, bool]]) -> None:
    # 与 summary.py 导出风格一致：case_name/debug/release 三列，Y/N 标记。
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_name", "debug", "release"])
        for case_name in sorted(cases):
            debug_flag, release_flag = cases[case_name]
            writer.writerow([case_name, "Y" if debug_flag else "N", "Y" if release_flag else "N"])


# ===== 运行前检查与主流程 =====
def pick_case_list_exe(run_mode: str, debug_exe: Path, release_exe: Path) -> Path:
    # 优先使用将要执行的模式对应 exe；both 模式优先 debug。
    if run_mode == "debug":
        return debug_exe
    if run_mode == "release":
        return release_exe
    return debug_exe if debug_exe.exists() else release_exe


def ensure_exe_exists(run_mode: str, debug_exe: Path, release_exe: Path) -> None:
    # 根据 run-mode 只校验会被实际使用到的可执行文件。
    if run_mode in {"both", "debug"} and not debug_exe.exists():
        raise FileNotFoundError(f"debug exe 不存在: {debug_exe}")
    if run_mode in {"both", "release"} and not release_exe.exists():
        raise FileNotFoundError(f"release exe 不存在: {release_exe}")


def main(argv: list[str] | None = None) -> int:
    """主入口：串联清单生成、筛选、并行执行、结果更新。"""
    args = parse_args(argv)

    if args.workers <= 0:
        raise ValueError("workers 必须 > 0")

    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result_csv = resolve_under_output(args.result_csv, output_dir)
    list_log = resolve_under_output(args.list_log, output_dir)
    list_skipped_csv = resolve_under_output(DEFAULT_LIST_SKIPPED_CSV, output_dir)
    list_failed_csv = resolve_under_output(DEFAULT_LIST_FAILED_CSV, output_dir)

    debug_exe = resolve_path(args.debug_exe)
    release_exe = resolve_path(args.release_exe)
    ensure_exe_exists(args.run_mode, debug_exe, release_exe)

    list_exe = pick_case_list_exe(args.run_mode, debug_exe, release_exe)
    all_cases = list_cases(list_exe)
    write_case_list_log(list_log, all_cases)
    print(f"[INFO] 已写入测例清单: {list_log} ({len(all_cases)} 条)")

    existing = load_existing_results(result_csv)
    if args.mode == "partial":
        target_cases = select_cases_for_partial(all_cases, existing)
    else:
        target_cases = list(all_cases)

    print(f"[INFO] 本次待测用例数: {len(target_cases)}")

    show_progress = DEFAULT_ENABLE_PROGRESS and not args.no_progress

    debug_updates: dict[str, str] | None = None
    release_updates: dict[str, str] | None = None

    if args.run_mode in {"both", "debug"}:
        debug_updates = run_cases_parallel(debug_exe, target_cases, args.workers, "debug", show_progress)
    if args.run_mode in {"both", "release"}:
        release_updates = run_cases_parallel(release_exe, target_cases, args.workers, "release", show_progress)

    merged_rows = merge_results(all_cases, existing, debug_updates, release_updates)
    write_result_csv(result_csv, merged_rows)
    skipped_cases = collect_case_flags(merged_rows, "SKIPPED")
    failed_cases = collect_case_flags(merged_rows, "FAIL")
    write_case_list(list_skipped_csv, skipped_cases)
    write_case_list(list_failed_csv, failed_cases)
    print(f"[INFO] 已更新结果 CSV: {result_csv}")
    print(f"[INFO] 已输出跳过清单: {list_skipped_csv}")
    print(f"[INFO] 已输出失败清单: {list_failed_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
