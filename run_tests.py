from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import summary
from utils.gtest_parser import count_gtest_list_cases
from utils.gtest_parser import is_complete_case_line
from utils.path_utils import resolve_path_from
from utils.progress import render_progress_bar
from utils.progress import SPINNER_FRAMES
from utils.process_job import JobObject
from utils.terminal_output import ANSI_CYAN
from utils.terminal_output import ANSI_GREEN
from utils.terminal_output import ANSI_RED
from utils.terminal_output import ANSI_YELLOW
from utils.terminal_output import clear_current_line
from utils.terminal_output import color_text
from utils.terminal_output import fit_status_to_terminal
from utils.terminal_output import try_enable_ansi_colors


"""并发执行 gtest 任务，并在结束后生成汇总。

常见修改方式：
1. 使用相对路径：把 DEFAULT_DEBUG_EXE / DEFAULT_RELEASE_EXE 改成 Path("Debug/tests.exe") 这类写法。
2. 只跑一种构建：把 DEFAULT_RUN_DEBUG 或 DEFAULT_RUN_RELEASE 设为 False，或运行时使用 --skip-debug / --skip-release。
3. 修改日志目录：调整 DEFAULT_OUTPUT_DIR，支持相对路径。

所有相对路径都会以当前脚本所在目录为基准解析。
"""


# ===== 可配置项：用户可以按需修改这些默认值 =====
# SCRIPT_DIR: 当前脚本所在目录。相对路径都会以它为基准解析，通常不需要修改。
SCRIPT_DIR = Path(__file__).resolve().parent
# DEFAULT_DEBUG_EXE: Debug 版 tests.exe 的默认路径。支持绝对路径，也支持相对脚本目录的相对路径。
DEFAULT_DEBUG_EXE = Path(r"Debug/tests.exe")
# DEFAULT_RELEASE_EXE: Release 版 tests.exe 的默认路径。支持绝对路径，也支持相对脚本目录的相对路径。
DEFAULT_RELEASE_EXE = Path(r"Release/tests.exe")
# DEFAULT_RUN_DEBUG: 是否默认执行 Debug 测试。若只想跑 Release，可改成 False。
DEFAULT_RUN_DEBUG = True
# DEFAULT_RUN_RELEASE: 是否默认执行 Release 测试。若只想跑 Debug，可改成 False。
DEFAULT_RUN_RELEASE = True
# 提示：
#   1. 只跑 Debug：把 DEFAULT_RUN_RELEASE 设为 False，或运行时加 --skip-release。
#   2. 只跑 Release：把 DEFAULT_RUN_DEBUG 设为 False，或运行时加 --skip-debug。
#   3. 使用相对路径：例如 Path("Debug/tests.exe")、Path("Release/tests.exe")。
# DEFAULT_MODE_CSV: 测试模式配置文件，决定要跑哪些 gtest_filter。通常就是 mode.csv。
DEFAULT_MODE_CSV = Path("mode.csv")
# DEFAULT_OUTPUT_DIR: 日志输出目录。每个任务的 stdout/stderr 和最终 summary.csv 都会写到这里。
DEFAULT_OUTPUT_DIR = Path("output")
# DEFAULT_ENABLE_CASE_PROGRESS: 是否默认启用用例级进度统计。关闭后启动更快，但只能看任务级进度。
DEFAULT_ENABLE_CASE_PROGRESS = True
# DEFAULT_RUN_SUMMARY: 全部任务结束后是否默认自动调用 summary.py 生成汇总。
DEFAULT_RUN_SUMMARY = True

# ===== 内部常量：下面这些是脚本实现细节，不建议修改 =====
# SPINNER_FRAMES: 进度行前面的转轮动画字符（来自公共进度模块）。
# PROGRESS_BAR_WIDTH: 任务/用例进度条的固定宽度，调大更细致，调小更紧凑。
PROGRESS_BAR_WIDTH = 18
# ACTIVE_NAME_WIDTH: 终端里 active 名称的最大显示宽度，避免名称过长导致进度行抖动。
ACTIVE_NAME_WIDTH = 32
# ANSI_*: 终端颜色常量来自公共终端模块。


@dataclass(slots=True)
class Mode:
    """mode.csv 中的一条测试配置。"""

    file_name: str
    filter_name: str
    test_num: int | None


@dataclass(slots=True)
class Task:
    """单个 gtest 子进程及其运行状态。"""

    process: subprocess.Popen[str]
    name: str
    filter_name: str
    case_count: int
    out_file: Path
    err_file: Path
    prefix: str
    mode_name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    case_done: int = 0
    done: bool = False
    exit_code: int | None = None
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    stderr_merged: bool = False

    def increment_case_done(self) -> None:
        with self.lock:
            self.case_done += 1
            if self.case_count >= 0 and self.case_done > self.case_count:
                self.case_done = self.case_count

    def get_case_done(self) -> int:
        with self.lock:
            return self.case_done

    def set_case_done(self, value: int) -> None:
        with self.lock:
            self.case_done = value


# ===== 命令行参数与配置解析 =====
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run gtest filters for Debug and Release in parallel.")
    parser.add_argument("--debug-exe", default=str(DEFAULT_DEBUG_EXE), help="Path to Debug tests.exe. Supports absolute or relative paths.")
    parser.add_argument("--release-exe", default=str(DEFAULT_RELEASE_EXE), help="Path to Release tests.exe. Supports absolute or relative paths.")
    parser.add_argument("--mode-csv", default=str(DEFAULT_MODE_CSV), help="CSV file listing test modes. Supports relative paths.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory where logs will be written. Supports relative paths.")
    parser.add_argument("--skip-debug", action="store_true", help="Skip Debug tests even if a Debug executable is configured.")
    parser.add_argument("--skip-release", action="store_true", help="Skip Release tests even if a Release executable is configured.")
    parser.add_argument("--no-case-progress", action="store_true", help="Disable case-level progress counting.")
    parser.add_argument("--no-summary", action="store_true", help="Do not run summary.py after tests finish.")
    return parser.parse_args(argv)


def resolve_path(path_text: str) -> Path:
    # 所有相对路径都基于脚本目录解析，避免受当前终端 cwd 影响。
    return resolve_path_from(SCRIPT_DIR, path_text)


def build_configs(args: argparse.Namespace, color_enabled: bool) -> list[tuple[str, Path]]:
    """根据默认配置和命令行参数决定实际要跑哪些构建。"""

    configs: list[tuple[str, Path]] = []

    debug_enabled = DEFAULT_RUN_DEBUG and not args.skip_debug
    release_enabled = DEFAULT_RUN_RELEASE and not args.skip_release

    if debug_enabled:
        debug_exe_arg = args.debug_exe.strip()
        if debug_exe_arg:
            configs.append(("debug", resolve_path(debug_exe_arg)))
        else:
            print(color_text("[SKIP] debugExe is empty, skipping debug tests.", ANSI_YELLOW, color_enabled))
    else:
        print(color_text("[SKIP] Debug tests disabled by configuration.", ANSI_YELLOW, color_enabled))

    if release_enabled:
        release_exe_arg = args.release_exe.strip()
        if release_exe_arg:
            configs.append(("release", resolve_path(release_exe_arg)))
        else:
            print(color_text("[SKIP] releaseExe is empty, skipping release tests.", ANSI_YELLOW, color_enabled))
    else:
        print(color_text("[SKIP] Release tests disabled by configuration.", ANSI_YELLOW, color_enabled))

    return configs


# ===== mode.csv / gtest 信息读取 =====
def load_modes(mode_csv: Path) -> list[Mode]:
    """读取 mode.csv，得到要启动的过滤器列表。"""

    if not mode_csv.exists():
        raise FileNotFoundError(f"mode.csv not found: {mode_csv}")

    modes: list[Mode] = []
    with mode_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            file_name = row.get("fileName", "").strip()
            filter_name = row.get("filterName", "").strip()
            test_num_text = row.get("testNum", "").strip()
            if not file_name or not filter_name:
                continue
            test_num = int(test_num_text) if test_num_text.isdigit() else None
            modes.append(Mode(file_name=file_name, filter_name=filter_name, test_num=test_num))
    return modes


def get_gtest_case_count(exe: Path, filter_name: str) -> int:
    """通过 --gtest_list_tests 预估当前 filter 的用例数。"""

    try:
        completed = subprocess.run(
            [str(exe), f"--gtest_filter={filter_name}", "--gtest_list_tests"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return -1

    if not completed.stdout:
        return -1

    return count_gtest_list_cases(completed.stdout)


def stream_output(stream, target_file: Path, task: Task | None, count_cases: bool) -> None:
    # 逐行转存子进程输出，避免 stdout/stderr 管道堵塞。
    with target_file.open("w", encoding="utf-8", errors="replace", newline="", buffering=1) as handle:
        for line in iter(stream.readline, ""):
            handle.write(line)
            if count_cases and task and is_complete_case_line(line):
                task.increment_case_done()
    stream.close()


def append_stderr_to_out_file(out_file: Path, err_file: Path) -> None:
    if not err_file.exists():
        return

    err_content = err_file.read_text(encoding="utf-8", errors="replace")
    if err_content:
        with out_file.open("a", encoding="utf-8", errors="replace", newline="") as handle:
            handle.write("\n--- STDERR ---\n")
            handle.write(err_content)

    err_file.unlink(missing_ok=True)


def finalize_task_outputs(task: Task) -> None:
    """等待日志线程收尾，并把 stderr 合并回主日志后删除 err 文件。"""

    if task.stdout_thread:
        task.stdout_thread.join(timeout=5)
    if task.stderr_thread:
        task.stderr_thread.join(timeout=5)

    if not task.stderr_merged:
        append_stderr_to_out_file(task.out_file, task.err_file)
        task.stderr_merged = True


def remove_empty_error_logs(output_dir: Path) -> None:
    for err_log in output_dir.glob("err-*.log"):
        if err_log.is_file() and err_log.stat().st_size == 0:
            err_log.unlink(missing_ok=True)


def format_elapsed(start_time: float) -> str:
    elapsed = max(0, int(time.time() - start_time))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_running_names(names: list[str], limit: int = 6) -> str:
    if not names:
        return "none"
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])}, ... (+{len(names) - limit} more)"


def shorten_text(text: str, width: int) -> str:
    if width <= 3 or len(text) <= width:
        return text[:width]
    return text[: width - 3] + "..."


def build_status_line(
    completed: int,
    total: int,
    completed_cases: int,
    known_case_total: int,
    unknown_case_tasks: int,
    running_names: list[str],
    start_time: float,
    spinner: str,
) -> str:
    task_bar = render_progress_bar(completed, total, PROGRESS_BAR_WIDTH)
    status = f"{spinner} task[{task_bar}] {completed}/{total} | run={len(running_names)} | t={format_elapsed(start_time)}"

    if known_case_total > 0:
        case_bar = render_progress_bar(completed_cases, known_case_total, PROGRESS_BAR_WIDTH)
        status += f" | case[{case_bar}] {completed_cases}/{known_case_total}"
        if unknown_case_tasks:
            status += f" +{unknown_case_tasks}?"
    elif unknown_case_tasks:
        status += f" | case[{'?' * PROGRESS_BAR_WIDTH}] unknown x{unknown_case_tasks}"

    active_display = shorten_text(format_running_names(running_names), ACTIVE_NAME_WIDTH)
    status += f" | active={active_display:<{ACTIVE_NAME_WIDTH}}"
    return status


def print_status_line(status: str, interactive: bool, previous_length: int, color_enabled: bool) -> int:
    # 进度行只占用当前一行，下一次刷新时直接覆盖。
    if interactive:
        fitted_status = fit_status_to_terminal(status, interactive)
        clear_current_line(interactive, previous_length, color_enabled)
        sys.stdout.write("\r" + fitted_status)
        sys.stdout.flush()
        return len(fitted_status)

    print(status)
    return len(status)


def finalize_status_line(interactive: bool, previous_length: int, color_enabled: bool) -> None:
    if interactive and previous_length:
        clear_current_line(interactive, previous_length, color_enabled)


def print_event_line(message: str, interactive: bool, previous_length: int, color: str | None, color_enabled: bool) -> int:
    # 在输出 DONE/FAIL 等事件前先清掉当前进度行，让事件直接落在那一行的位置。
    clear_current_line(interactive, previous_length, color_enabled)
    print(color_text(message, color, color_enabled))
    return 0


# ===== 终端输出与子进程执行 =====
def start_task(
    job: JobObject,
    exe: Path,
    prefix: str,
    mode: Mode,
    output_dir: Path,
    enable_case_progress: bool,
) -> Task:
    """启动一个 gtest 进程，并挂接日志转存线程。"""

    out_file = output_dir / f"{prefix}-{mode.file_name}.log"
    err_file = output_dir / f"err-{prefix}-{mode.file_name}.log"

    case_count = -1
    if enable_case_progress:
        case_count = get_gtest_case_count(exe, mode.filter_name)

    process = subprocess.Popen(
        [str(exe), f"--gtest_filter={mode.filter_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=str(SCRIPT_DIR),
    )

    job.add_process(process._handle)

    task = Task(
        process=process,
        name=f"{prefix}-{mode.file_name}",
        filter_name=mode.filter_name,
        case_count=case_count,
        out_file=out_file,
        err_file=err_file,
        prefix=prefix,
        mode_name=mode.file_name,
    )

    stdout_thread = threading.Thread(
        target=stream_output,
        args=(process.stdout, out_file, task, enable_case_progress),
        name=f"stdout-{task.name}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stream_output,
        args=(process.stderr, err_file, None, False),
        name=f"stderr-{task.name}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    task.stdout_thread = stdout_thread
    task.stderr_thread = stderr_thread
    return task


def run_summary(output_dir: Path, mode_csv: Path) -> int:
    print("[SUMMARY] Generating summary...")
    try:
        summary.generate_summary(output_dir=output_dir, mode_csv=mode_csv, emit_console=True)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


def run(argv: list[str] | None = None) -> int:
    """主流程：启动全部任务、实时显示进度、清理子进程、生成汇总。"""

    args = parse_args(argv)
    mode_csv = resolve_path(args.mode_csv)
    output_dir = resolve_path(args.output_dir)
    enable_case_progress = DEFAULT_ENABLE_CASE_PROGRESS and not args.no_case_progress
    run_summary_after_tests = DEFAULT_RUN_SUMMARY and not args.no_summary
    interactive = sys.stdout.isatty()
    color_enabled = try_enable_ansi_colors()

    if not mode_csv.exists():
        print(f"[ERROR] mode.csv not found: {mode_csv}", file=sys.stderr)
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    modes = load_modes(mode_csv)
    configs = build_configs(args, color_enabled)

    tasks: list[Task] = []
    failed = 0
    interrupted = False
    status_length = 0
    spinner_index = 0
    start_time = time.time()

    try:
        with JobObject() as job:
            for prefix, exe in configs:
                if not exe.exists():
                    print(color_text(f"[SKIP] {exe} not found, skipping {prefix} tests.", ANSI_YELLOW, color_enabled))
                    continue

                for mode in modes:
                    task = start_task(job, exe, prefix, mode, output_dir, enable_case_progress)
                    tasks.append(task)
                    if enable_case_progress:
                        estimated = task.case_count if task.case_count >= 0 else "unknown"
                        print(color_text(f"[INFO] {prefix} - {mode.file_name} estimated cases: {estimated}", ANSI_CYAN, color_enabled))
                    print(color_text(f"[START] {prefix} - {mode.file_name} (filter: {mode.filter_name})", ANSI_CYAN, color_enabled))

            total = len(tasks)
            print(f"Waiting for {total} tests to finish...")

            known_case_total = sum(task.case_count for task in tasks if task.case_count >= 0)
            unknown_case_tasks = sum(1 for task in tasks if task.case_count < 0)

            while True:
                completed = 0
                running_names: list[str] = []

                for task in tasks:
                    exit_code = task.process.poll()
                    if exit_code is None:
                        running_names.append(task.name)
                        continue

                    if not task.done:
                        task.process.wait()
                        finalize_task_outputs(task)
                        if enable_case_progress:
                            if task.case_count >= 0:
                                task.set_case_done(task.case_count)
                            else:
                                task.set_case_done(task.get_case_done())

                        task.done = True
                        task.exit_code = exit_code
                        if exit_code == 0:
                            status_length = print_event_line(
                                f"[DONE] {task.name} -> {task.out_file}",
                                interactive,
                                status_length,
                                ANSI_GREEN,
                                color_enabled,
                            )
                        else:
                            status_length = print_event_line(
                                f"[FAIL] {task.name} exit={exit_code} -> {task.out_file}",
                                interactive,
                                status_length,
                                ANSI_RED,
                                color_enabled,
                            )
                            failed += 1

                    completed += 1

                completed_cases = sum(task.get_case_done() for task in tasks if task.case_count >= 0)
                spinner = SPINNER_FRAMES[spinner_index]
                spinner_index = (spinner_index + 1) % len(SPINNER_FRAMES)
                status = build_status_line(
                    completed=completed,
                    total=len(tasks),
                    completed_cases=completed_cases,
                    known_case_total=known_case_total if enable_case_progress else 0,
                    unknown_case_tasks=unknown_case_tasks if enable_case_progress else 0,
                    running_names=running_names,
                    start_time=start_time,
                    spinner=spinner,
                )

                status_length = print_status_line(status, interactive, status_length, color_enabled)

                if completed == len(tasks):
                    break

                time.sleep(1)

    except KeyboardInterrupt:
        interrupted = True
        status_length = print_event_line(
            "[CLEANUP] Terminating all child processes...",
            interactive,
            status_length,
            ANSI_YELLOW,
            color_enabled,
        )
    finally:
        finalize_status_line(interactive, status_length, color_enabled)
        for task in tasks:
            finalize_task_outputs(task)
        remove_empty_error_logs(output_dir)

    print(color_text(f"All done! {len(tasks)} tests launched, {failed} failed.", ANSI_CYAN, color_enabled))

    summary_exit = 0
    if run_summary_after_tests:
        if interrupted:
            print(color_text("[SUMMARY] Interrupted run detected. Summary may be partial.", ANSI_YELLOW, color_enabled))
        summary_exit = run_summary(output_dir, mode_csv)

    if interrupted:
        return 130
    if failed:
        return 1
    return summary_exit


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())