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

from utils.case_report import write_case_presence_csv
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
# DEFAULT_WORKERS: 默认并行线程数（每个线程一次跑一批测例）。
DEFAULT_WORKERS = 30
# DEFAULT_TESTS_PER_GROUP: 每批次执行的测例数目。
# 组装数量太大可能触发命令行长度限制 (8191)。50左右较安全，能有效降低启动开销。
DEFAULT_TESTS_PER_GROUP = 50
# DEFAULT_GROUP_MULTIPLIER: 批次数倍率。目标批次数约为 workers * 倍率（同时受 group-size 上限约束）。
DEFAULT_GROUP_MULTIPLIER = 8
# DEFAULT_BATCH_START_RETRIES: 批次在首用例前崩溃时的重试次数，避免瞬时资源抖动被误判为 FAIL。
DEFAULT_BATCH_START_RETRIES = 2
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
# DEFAULT_SMOOTH_SHRINK_START_RATIO: 从总量的该比例处开始平滑缩组（0~1）。
# 例如 0.60 表示前 40% 保持吞吐，后 60% 线性收敛到 1。
DEFAULT_SMOOTH_SHRINK_START_RATIO = 0.60

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
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并行线程数。")
    parser.add_argument("--group-size", type=int, default=DEFAULT_TESTS_PER_GROUP, help="每个执行实例串行跑的最大用例数。")
    parser.add_argument("--group-multiplier", type=int, default=DEFAULT_GROUP_MULTIPLIER, help="批次数倍率（目标批次数约为 workers*倍率）。")
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


def build_case_batches(
    cases: list[str],
    workers: int,
    max_group_size: int,
    group_multiplier: int,
) -> tuple[list[list[str]], int, str]:
    """构建自适应分组。

    策略：
    - `max_group_size` 仅作为上限；
    - 批次数会适度多于 worker，增强后半程补位能力；
    - 用 round-robin 分配，尽量打散潜在慢测例，缓解尾部拖尾。
    """

    if not cases:
        return [], 0, "empty"

    # 先按 suite 打散，再进入轮转分组，降低“慢 suite 尾部集中”概率。
    ordered_cases = interleave_cases_by_suite(cases)

    total = len(ordered_cases)
    min_groups_for_cap = max(1, math.ceil(total / max_group_size))
    # 保持比 worker 更多的批次，提升线程池后半程补位能力，减少尾部仅剩少量进程。
    target_groups = max(min_groups_for_cap, min(total, max(1, workers) * max(1, group_multiplier)))

    batches: list[list[str]] = [[] for _ in range(target_groups)]
    for index, case_name in enumerate(ordered_cases):
        batches[index % target_groups].append(case_name)

    compact_batches = [b for b in batches if b]
    adaptive_group_size = max(len(b) for b in compact_batches)
    return compact_batches, adaptive_group_size, "count-adaptive-rr"


def run_case_batch(exe_path: Path, cases: list[str], job: JobObject) -> dict[str, str]:
    # 按照批次顺次执行：如果遇到由于异常进程崩溃导致中断，记录崩溃用例为 FAIL，并重新拉起剩余未执行的用例。
    results: dict[str, str] = {}
    remaining = cases[:]
    start_retry_counter: dict[str, int] = {}

    while remaining:
        filter_arg = ":".join(remaining)
        process = subprocess.Popen(
            [str(exe_path), f"--gtest_filter={filter_arg}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(SCRIPT_DIR),
        )
        job.add_process(process._handle)
        stdout_text, stderr_text = process.communicate()

        output_text = (stdout_text or "") + "\n" + (stderr_text or "")
        started, finished, _parsed_elapsed = parse_batch_case_statuses(output_text)

        crashed_index = -1
        for i, c in enumerate(remaining):
            if c in finished:
                results[c] = finished[c]
            elif c in started:
                # 运行了一半没有完成，说明它是直接导致崩溃的元凶
                results[c] = "FAIL"
                crashed_index = i
                break
            else:
                # 既没运行完也没开始运行
                if "DISABLED_" in c:
                    # gtest 会自动跳过带有 DISABLED_ 前缀的测试，不输出 RUN 和完成状态，这是正常的
                    results[c] = "SKIPPED"
                else:
                    # 正常用例却没有被打出运行标志 -> 进程在到达它之前就崩溃了（比如上一个用例的 TearDown，或是进程起不来）
                    crashed_index = i
                    break

        if crashed_index == 0 and not started:
            # 情况A：程序在跑到第一个正常的用例（非DISABLED）之前就崩溃了。
            # 这可能是该用例的 SetUp 崩溃，或者是全局起不来。
            # 先短重试，避免瞬时资源抖动导致的误判 FAIL。
            head_case = remaining[0]
            retry_count = start_retry_counter.get(head_case, 0)
            if retry_count < DEFAULT_BATCH_START_RETRIES:
                start_retry_counter[head_case] = retry_count + 1
                time.sleep(0.05 * (retry_count + 1))
                continue

            # 多次重试仍失败，再标记为 FAIL 并跳过，防止死循环。
            results[head_case] = "FAIL"
            remaining = remaining[1:]
        elif crashed_index >= 0:
            # 发生了部分崩溃，尝试从崩溃位置继续接力
            if remaining[crashed_index] in started:
                # 这个用例运行一半挂了，下一批不再跑它，从它的下一个用例开始
                remaining = remaining[crashed_index + 1:]
            else:
                # 这个用例还没碰到就挂了，下一批从它自己开始重新跑
                remaining = remaining[crashed_index:]
        else:
            # 正常遍历结束，全部处理完成
            remaining = []

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
    smooth_shrink_start_remaining = max(1, math.ceil(total * DEFAULT_SMOOTH_SHRINK_START_RATIO))

    print(
        f"[{label}] 分组策略: dynamic-claim workers={workers} group_mult={group_multiplier} max_group={group_size} "
        f"smooth_shrink_start={smooth_shrink_start_remaining}/{total}"
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

            # 关键规则：总是按“剩余/并发”估算下一批大小，并受 group_size 上限约束。
            # 当剩余用例减少时，batch_size 会自然变小（最小 1）。
            base_group_size = min(group_size, max(1, math.ceil(remaining / max(1, workers))))

            # 平滑收缩：进入 shrink 区间后，允许的批大小从 group_size 线性下降到 1。
            # 同时叠加 base_group_size，兼顾并发利用率与尾部细粒度收尾。
            if remaining <= smooth_shrink_start_remaining:
                shrink_span = max(1, smooth_shrink_start_remaining - 1)
                shrink_progress = (smooth_shrink_start_remaining - remaining) / shrink_span
                smooth_cap = max(1, math.ceil(group_size - (group_size - 1) * shrink_progress))
                dynamic_group_size = min(base_group_size, smooth_cap)
            else:
                dynamic_group_size = base_group_size

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

            with lock:
                running += 1
            try:
                batch_result = run_case_batch(exe_path, batch, job)
            finally:
                with lock:
                    running -= 1

            with lock:
                done += len(batch_result)
                for case_name, status in batch_result.items():
                    if status == "PASS":
                        passed_count += 1
                    elif status == "SKIPPED":
                        skipped_count += 1
                    else:
                        failed_count += 1
                    results[case_name] = status

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
    print(f"[INFO] 已更新结果 CSV: {result_csv}")
    print(f"[INFO] 已输出跳过清单: {list_skipped_csv}")
    print(f"[INFO] 已输出失败清单: {list_failed_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
