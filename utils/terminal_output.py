from __future__ import annotations

import ctypes
import os
import shutil
import sys


# Win32 console mode flags used to enable ANSI sequences on Windows terminals.
STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# ANSI color/control codes used by run scripts.
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_YELLOW = "\033[33m"
ANSI_CYAN = "\033[36m"
ANSI_CLEAR_LINE = "\033[2K"


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetStdHandle.argtypes = [ctypes.c_int]
kernel32.GetStdHandle.restype = ctypes.c_void_p
kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
kernel32.GetConsoleMode.restype = ctypes.c_int
kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.SetConsoleMode.restype = ctypes.c_int


def try_enable_ansi_colors() -> bool:
    """在 Windows 终端启用 ANSI 颜色；失败时退回纯文本。"""

    if os.name != "nt" or not sys.stdout.isatty():
        return False

    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    if not handle:
        return False

    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False

    if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
        return True

    return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))


def color_text(text: str, color: str | None, enabled: bool) -> str:
    """按需包裹 ANSI 颜色；禁用时原样返回。"""

    if not enabled or not color:
        return text
    return f"{color}{text}{ANSI_RESET}"


def clear_current_line(interactive: bool, previous_length: int, color_enabled: bool) -> None:
    """清空当前终端行，用于覆盖上一帧进度输出。"""

    if not interactive or previous_length <= 0:
        return

    if color_enabled:
        sys.stdout.write("\r" + ANSI_CLEAR_LINE)
    else:
        sys.stdout.write("\r" + (" " * previous_length) + "\r")
    sys.stdout.flush()


def get_terminal_width(default: int = 120) -> int:
    """获取当前终端宽度，用于避免进度行过长后自动换行。"""

    try:
        return max(40, shutil.get_terminal_size(fallback=(default, 20)).columns)
    except OSError:
        return default


def fit_status_to_terminal(status: str, interactive: bool) -> str:
    """将进度行裁剪到终端宽度内，避免 \r 覆盖时因为换行失效。"""

    if not interactive:
        return status

    width = get_terminal_width()
    max_length = max(10, width - 1)
    if len(status) <= max_length:
        return status

    if max_length <= 3:
        return status[:max_length]

    return status[: max_length - 3] + "..."