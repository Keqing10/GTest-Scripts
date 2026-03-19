from __future__ import annotations

import argparse
import csv
import math
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from utils.case_report import write_case_presence_csv
from utils.csv_utils import write_csv_rows
from utils.path_utils import resolve_path_from
from utils.path_utils import resolve_under_output
from utils.gtest_parser import parse_gtest_list_output
from utils.gtest_parser import parse_batch_case_statuses
from utils.progress import render_progress_bar
from utils.progress import SPINNER_FRAMES
from utils.process_job import JobObject


"""独立脚本：按“分组批量 + 并行进程”的方式运行 gtest。

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
DEFAULT_OUTPUT_DIR = Path("output-partial")
# DEFAULT_RESULT_CSV: 默认结果 CSV 文件名，包含每个用例的 debug/release 结果。
DEFAULT_RESULT_CSV = "case_results.csv"
# DEFAULT_LIST_LOG: 默认测例列表文件名，保存列出的全部测例名。
DEFAULT_LIST_LOG = "list.log"
# DEFAULT_LIST_SKIPPED_CSV: 默认跳过用例清单文件名（和 run_tests 风格一致）。
DEFAULT_LIST_SKIPPED_CSV = "list-skipped.csv"
# DEFAULT_LIST_FAILED_CSV: 默认失败用例清单文件名（和 run_tests 风格一致）。
DEFAULT_LIST_FAILED_CSV = "list-failed.csv"
# DEFAULT_SUMMARY_CSV: 默认执行总结文件名。
DEFAULT_SUMMARY_CSV = "summary.csv"
# DEFAULT_SUMMARY_PARTIAL_CSV: partial 模式附加明细汇总（记录 target_cases 等本次执行信息）。
DEFAULT_SUMMARY_PARTIAL_CSV = "summary-partial.csv"
# DEFAULT_WORKERS: 默认并行线程数（每个线程一次跑一批测例）。
DEFAULT_WORKERS = 30
# DEFAULT_TESTS_PER_GROUP: 每批次执行的测例数目。
# 组装数量太大可能触发命令行长度限制 (8191)。50左右较安全，能有效降低启动开销。
DEFAULT_TESTS_PER_GROUP = 50
# DEFAULT_GROUP_MULTIPLIER: 批次数倍率。目标批次数约为 workers * 倍率（同时受 group-size 上限约束）。
DEFAULT_GROUP_MULTIPLIER = 8
# DEFAULT_TEST_MODE: 默认测试范围。full=全量；partial=只重测未双通过用例。
DEFAULT_TEST_MODE = "partial"  # full | partial
# DEFAULT_RUN_MODE: 默认构建模式。both/debug/release。
DEFAULT_RUN_MODE = "both"  # both | debug | release
# DEFAULT_ENABLE_PROGRESS: 是否默认启用终端进度条。
DEFAULT_ENABLE_PROGRESS = True
# DEFAULT_PROGRESS_REFRESH_SEC: 进度条刷新间隔（秒），数值越大占用越低。
DEFAULT_PROGRESS_REFRESH_SEC = 1
# DEFAULT_PROGRESS_BAR_WIDTH: 进度条宽度。
DEFAULT_PROGRESS_BAR_WIDTH = 20
# DEFAULT_CASE_TIMEOUT_SEC: 单测例超时时间（秒），超时后标记 FAIL。
DEFAULT_CASE_TIMEOUT_SEC = 300.0

# DEFAULT_DEBUG_EXE: Debug 版 tests.exe 默认路径。支持绝对路径或相对路径。
DEFAULT_DEBUG_EXE = Path(r"Debug/tests.exe")
# DEFAULT_RELEASE_EXE: Release 版 tests.exe 默认路径。支持绝对路径或相对路径。
DEFAULT_RELEASE_EXE = Path(r"Release/tests.exe")


@dataclass(slots=True)
class CaseResult:
    case_name: str
    debug_pass: str
    release_pass: str


# ===== 命令行参数与路径处理 =====
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数：控制测试范围、构建模式、并发度和输出位置。"""
    parser = argparse.ArgumentParser(description="Run gtest in adaptive grouped batches with parallel workers.")
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
    parser.add_argument("--summary-csv", default=DEFAULT_SUMMARY_CSV, help="执行总结 CSV 文件名或路径。")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并行线程数。")
    parser.add_argument("--group-size", type=int, default=DEFAULT_TESTS_PER_GROUP, help="每个执行实例串行跑的最大用例数。")
    parser.add_argument("--group-multiplier", type=int, default=DEFAULT_GROUP_MULTIPLIER, help="批次数倍率（目标批次数约为 workers*倍率）。")
    parser.add_argument(
        "--case-timeout-sec",
        type=float,
        default=DEFAULT_CASE_TIMEOUT_SEC,
        help="单个测例超时时间（秒），默认 300 秒。",
    )
    parser.add_argument("--no-progress", action="store_true", help="关闭终端进度条，改为仅输出关键日志。")
    return parser.parse_args(argv)


def resolve_path(path_text: str) -> Path:
    # 相对路径统一按脚本目录解析，避免受当前终端 cwd 影响。
    return resolve_path_from(SCRIPT_DIR, path_text)


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

    return parse_gtest_list_output(completed.stdout)


def write_case_list_log(list_log: Path, cases: list[str]) -> None:
    with list_log.open("w", encoding="utf-8", newline="") as handle:
        for case_name in cases:
            handle.write(case_name)
            handle.write("\n")


def interleave_cases_by_suite(cases: list[str]) -> list[str]:
    """按 Suite 交织打散用例，减少同类慢测例在尾部扎堆。"""

    suite_buckets: dict[str, list[str]] = {}
    for case_name in cases:
        suite_name, _sep, _leaf = case_name.partition(".")
        if suite_name not in suite_buckets:
            suite_buckets[suite_name] = []
        suite_buckets[suite_name].append(case_name)

    if len(suite_buckets) <= 1:
        return list(cases)

    # 先取大 bucket，可更快把大 suite 打散到全局。
    suite_order = sorted(suite_buckets.keys(), key=lambda name: len(suite_buckets[name]), reverse=True)
    interleaved: list[str] = []
    active = True
    while active:
        active = False
        for suite_name in suite_order:
            bucket = suite_buckets[suite_name]
            if not bucket:
                continue
            interleaved.append(bucket.pop(0))
            active = True

    return interleaved


def run_single_case_with_timeout(exe_path: Path, case_name: str, timeout_sec: float, job: JobObject) -> str:
    if "DISABLED_" in case_name:
        return "SKIPPED"

    process = subprocess.Popen(
        [str(exe_path), f"--gtest_filter={case_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(SCRIPT_DIR),
    )
    job.add_process(process._handle)

    try:
        stdout_text, stderr_text = process.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        print(f"[TIMEOUT] 单测例超时: {case_name} > {int(timeout_sec)}s，标记 FAIL")
        return "FAIL"

    output_text = (stdout_text or "") + "\n" + (stderr_text or "")
    _started, finished, _parsed_elapsed = parse_batch_case_statuses(output_text)

    if case_name in finished:
        return finished[case_name]
    if "DISABLED" in output_text.upper():
        return "SKIPPED"
    return "FAIL"


def run_case_batch(
    exe_path: Path,
    cases: list[str],
    case_timeout_sec: float,
    job: JobObject,
    on_case_result: Callable[[str, str], None] | None = None,
) -> dict[str, str]:
    # 批次仅用于调度；批内逐条执行，确保“单测例超时判 FAIL”。
    results: dict[str, str] = {}
    for case_name in cases:
        status = run_single_case_with_timeout(exe_path, case_name, case_timeout_sec, job)
        results[case_name] = status
        if on_case_result is not None:
            on_case_result(case_name, status)
    return results


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
    group_size: int,
    group_multiplier: int,
    case_timeout_sec: float,
    label: str,
    show_progress: bool,
    job: JobObject,
) -> dict[str, str]:
    """并行执行用例（基于分组串行执行），返回 {case_name: PASS/FAIL/SKIPPED}。"""
    if not cases:
        return {}

    # 先按 suite 打散，再由 worker 在运行时按“剩余量”动态领取下一批。
    # 这样尾部剩余测例变少时，批次会自动变小，减少最后几批拖尾。
    ordered_cases = interleave_cases_by_suite(cases)
    total = len(ordered_cases)
    results: dict[str, str] = {}
    lock = threading.Lock()
    done = 0
    passed_count = 0
    failed_count = 0
    skipped_count = 0
    running = 0
    next_index = 0
    claimed_batches = 0
    target_batches_remaining = max(1, workers) * max(1, group_multiplier)

    print(
        f"[{label}] 分组策略: dynamic-claim workers={workers} group_mult={group_multiplier} max_group={group_size} "
        f"target_batches={target_batches_remaining} timeout={int(case_timeout_sec)}s"
    )

    interactive = show_progress and sys.stdout.isatty()
    progress_enabled = show_progress
    stop_event = threading.Event()
    start_time = time.time()

    def claim_next_batch() -> list[str]:
        nonlocal next_index, claimed_batches
        with lock:
            remaining = total - next_index
            if remaining <= 0:
                return []

            # 平滑收缩：保持大约 workers*group_multiplier 个待领批次。
            # remaining 越少，batch_size 越小，最终自然降到 1。
            dynamic_group_size = min(group_size, max(1, math.ceil(remaining / target_batches_remaining)))

            # 保证最后一轮是 1 个测例/批。
            if remaining <= max(1, workers):
                dynamic_group_size = 1

            end = min(total, next_index + dynamic_group_size)
            batch = ordered_cases[next_index:end]
            next_index = end
            claimed_batches += 1
            return batch

    def worker_loop() -> None:
        nonlocal running, done, passed_count, failed_count, skipped_count

        while True:
            batch = claim_next_batch()
            if not batch:
                return

            def on_case_result(case_name: str, status: str) -> None:
                nonlocal done, passed_count, failed_count, skipped_count
                with lock:
                    done += 1
                    if status == "PASS":
                        passed_count += 1
                    elif status == "SKIPPED":
                        skipped_count += 1
                    else:
                        failed_count += 1
                    results[case_name] = status

            with lock:
                running += 1
            try:
                run_case_batch(
                    exe_path,
                    batch,
                    case_timeout_sec,
                    job,
                    on_case_result=on_case_result,
                )
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
            bar = render_progress_bar(done_snapshot, total, DEFAULT_PROGRESS_BAR_WIDTH)
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
        final_bar = render_progress_bar(done_snapshot, total, DEFAULT_PROGRESS_BAR_WIDTH)
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

    executor = ThreadPoolExecutor(max_workers=workers)
    shutdown_wait = True
    try:
        futures = [executor.submit(worker_loop) for _ in range(workers)]
        for future in as_completed(futures):
            future.result()
    except KeyboardInterrupt:
        shutdown_wait = False
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if shutdown_wait:
            executor.shutdown(wait=True, cancel_futures=True)

        if progress_enabled:
            stop_event.set()
            if progress_thread is not None:
                progress_thread.join(timeout=2)
        else:
            print(
                f"[{label}] done {total}/{total} pass={passed_count} fail={failed_count} "
                f"skip={skipped_count} t={format_elapsed(start_time)}"
            )

    print(f"[{label}] dynamic batches claimed: {claimed_batches}")

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
    csv_rows = ([row.case_name, row.debug_pass, row.release_pass] for row in rows)
    write_csv_rows(csv_path, ["case_name", "debug_pass", "release_pass"], csv_rows)


def collect_case_flags(rows: list[CaseResult], status_name: str) -> dict[str, tuple[bool, bool]]:
    cases: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        debug_hit = row.debug_pass == status_name
        release_hit = row.release_pass == status_name
        if debug_hit or release_hit:
            cases[row.case_name] = (debug_hit, release_hit)
    return cases


def count_stage_statuses(rows: list[CaseResult], stage: str) -> tuple[int, int, int]:
    pass_count = 0
    fail_count = 0
    skip_count = 0
    for row in rows:
        status = row.debug_pass if stage == "debug" else row.release_pass
        if status == "PASS":
            pass_count += 1
        elif status == "FAIL":
            fail_count += 1
        elif status == "SKIPPED":
            skip_count += 1
    return pass_count, fail_count, skip_count


def count_statuses_from_updates(result_map: dict[str, str] | None) -> tuple[int, int, int]:
    if not result_map:
        return 0, 0, 0

    pass_count = 0
    fail_count = 0
    skip_count = 0
    for status in result_map.values():
        if status == "PASS":
            pass_count += 1
        elif status == "SKIPPED":
            skip_count += 1
        else:
            fail_count += 1
    return pass_count, fail_count, skip_count


def write_summary_csv(
    summary_csv: Path,
    mode: str,
    requested_run_mode: str,
    total_cases: int,
    elapsed_sec: int,
    merged_rows: list[CaseResult],
) -> None:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    header = [
        "generated_at",
        "mode",
        "run_mode",
        "total_cases",
        "pass_count",
        "fail_count",
        "skip_count",
        "pass_rate",
        "elapsed_sec",
    ]

    csv_rows: list[list[object]] = []

    stages: list[str]
    if requested_run_mode == "both":
        stages = ["debug", "release"]
    else:
        stages = [requested_run_mode]

    for stage in stages:
        pass_count, fail_count, skip_count = count_stage_statuses(merged_rows, stage)
        pass_rate = (pass_count / total_cases * 100.0) if total_cases > 0 else 0.0
        csv_rows.append(
            [
                generated_at,
                mode,
                stage,
                total_cases,
                pass_count,
                fail_count,
                skip_count,
                f"{pass_rate:.2f}%",
                elapsed_sec,
            ]
        )

    write_csv_rows(summary_csv, header, csv_rows)


def write_partial_summary_csv(
    summary_partial_csv: Path,
    mode: str,
    requested_run_mode: str,
    total_cases: int,
    target_cases: int,
    workers: int,
    group_size: int,
    group_multiplier: int,
    case_timeout_sec: float,
    elapsed_sec: int,
    debug_updates: dict[str, str] | None,
    release_updates: dict[str, str] | None,
) -> None:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    header = [
        "generated_at",
        "mode",
        "run_mode",
        "total_cases",
        "target_cases",
        "executed_cases",
        "pass_count",
        "fail_count",
        "skip_count",
        "pass_rate",
        "elapsed_sec",
        "workers",
        "group_size",
        "group_multiplier",
        "case_timeout_sec",
    ]

    rows: list[list[object]] = []
    stages = ["debug", "release"] if requested_run_mode == "both" else [requested_run_mode]

    def updates_for_stage(stage: str) -> dict[str, str] | None:
        return debug_updates if stage == "debug" else release_updates

    for stage in stages:
        updates = updates_for_stage(stage)
        pass_count, fail_count, skip_count = count_statuses_from_updates(updates)
        executed_cases = len(updates) if updates else 0
        pass_rate = (pass_count / executed_cases * 100.0) if executed_cases > 0 else 0.0
        rows.append(
            [
                generated_at,
                mode,
                stage,
                total_cases,
                target_cases,
                executed_cases,
                pass_count,
                fail_count,
                skip_count,
                f"{pass_rate:.2f}%",
                elapsed_sec,
                workers,
                group_size,
                group_multiplier,
                f"{case_timeout_sec:.1f}",
            ]
        )

    write_csv_rows(summary_partial_csv, header, rows)


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
    if args.group_size <= 0:
        raise ValueError("group-size 必须 > 0")
    if args.group_multiplier <= 0:
        raise ValueError("group-multiplier 必须 > 0")
    if args.case_timeout_sec <= 0:
        raise ValueError("case-timeout-sec 必须 > 0")

    run_start_time = time.time()

    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result_csv = resolve_under_output(args.result_csv, output_dir)
    summary_csv = resolve_under_output(args.summary_csv, output_dir)
    summary_partial_csv = resolve_under_output(DEFAULT_SUMMARY_PARTIAL_CSV, output_dir)
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

    interrupted = False

    try:
        with JobObject() as job:
            if args.run_mode in {"both", "debug"}:
                debug_updates = run_cases_parallel(
                    debug_exe,
                    target_cases,
                    args.workers,
                    args.group_size,
                    args.group_multiplier,
                    args.case_timeout_sec,
                    "debug",
                    show_progress,
                    job,
                )
            if args.run_mode in {"both", "release"}:
                release_updates = run_cases_parallel(
                    release_exe,
                    target_cases,
                    args.workers,
                    args.group_size,
                    args.group_multiplier,
                    args.case_timeout_sec,
                    "release",
                    show_progress,
                    job,
                )
    except KeyboardInterrupt:
        interrupted = True
        print("[CLEANUP] Terminating all child processes...")

    if interrupted:
        return 130

    merged_rows = merge_results(all_cases, existing, debug_updates, release_updates)
    write_result_csv(result_csv, merged_rows)
    skipped_cases = collect_case_flags(merged_rows, "SKIPPED")
    failed_cases = collect_case_flags(merged_rows, "FAIL")
    write_case_presence_csv(list_skipped_csv, skipped_cases)
    write_case_presence_csv(list_failed_csv, failed_cases)
    elapsed_sec = max(0, int(time.time() - run_start_time))
    write_summary_csv(
        summary_csv,
        mode=args.mode,
        requested_run_mode=args.run_mode,
        total_cases=len(all_cases),
        elapsed_sec=elapsed_sec,
        merged_rows=merged_rows,
    )
    if args.mode == "partial":
        write_partial_summary_csv(
            summary_partial_csv,
            mode=args.mode,
            requested_run_mode=args.run_mode,
            total_cases=len(all_cases),
            target_cases=len(target_cases),
            workers=args.workers,
            group_size=args.group_size,
            group_multiplier=args.group_multiplier,
            case_timeout_sec=args.case_timeout_sec,
            elapsed_sec=elapsed_sec,
            debug_updates=debug_updates,
            release_updates=release_updates,
        )
    print(f"[INFO] 已更新结果 CSV: {result_csv}")
    print(f"[INFO] 已输出跳过清单: {list_skipped_csv}")
    print(f"[INFO] 已输出失败清单: {list_failed_csv}")
    print(f"[INFO] 已输出执行汇总: {summary_csv}")
    if args.mode == "partial":
        print(f"[INFO] 已输出 partial 执行汇总: {summary_partial_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
