from __future__ import annotations

from pathlib import Path


def resolve_path_from(base_dir: Path, path_text: str) -> Path:
    """将 path_text 解析为绝对路径，相对路径基于 base_dir。"""

    path = Path(path_text)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def resolve_under_output(path_text: str, output_dir: Path) -> Path:
    """将结果文件解析到 output_dir 下；绝对路径保持不变。"""

    path = Path(path_text)
    if path.is_absolute():
        return path
    return output_dir / path