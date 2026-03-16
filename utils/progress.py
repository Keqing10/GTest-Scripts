from __future__ import annotations


# Spinner frames for terminal progress animation.
SPINNER_FRAMES = ("|", "/", "-", "\\")


def render_progress_bar(done: int, total: int, width: int) -> str:
    """生成固定宽度进度条字符串。"""

    if total <= 0:
        return "-" * width
    filled = min(width, int((done / total) * width))
    return "#" * filled + "-" * (width - filled)