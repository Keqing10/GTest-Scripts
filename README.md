# GTest 日志汇总工具

这个仓库提供一组 Python 脚本，用于并行执行 Google Test（gtest）并导出汇总结果。

## 主要文件

- `run_tests.py`：按 `mode.csv` 中的模式并行运行 debug/release，并可在结束后自动调用 `summary.py`。
- `partial_tests.py`：按“批量分组”串行聚合后并行执行，支持 `full`/`partial` 增量重测模式。
- `summary.py`：读取日志目录，生成 `summary.csv`、`list-skipped.csv`、`list-failed.csv`。
- `mode.csv`：测试模式配置（`fileName`, `filterName`, `testNum`）。

## 公共复用模块

- `utils/process_job.py`：Windows Job Object 封装（脚本退出或 Ctrl+C 时清理子进程）。
- `utils/terminal_output.py`：终端彩色输出与单行刷新逻辑（ANSI 开启、颜色包装、行清理、宽度裁剪）。
- `utils/path_utils.py`：路径解析工具（脚本目录相对路径、output 目录相对路径）。
- `utils/progress.py`：进度条字符串渲染。
- `utils/gtest_parser.py`：集中管理 gtest 输出解析规则（list、状态行、summary 段）。
- `utils/case_report.py`：统一导出 `case_name/debug/release` 三列 CSV 格式。

## run_tests.py 使用方法

基础运行：

```powershell
python run_tests.py
```

常用参数：

- `--debug-exe <path>`：指定 Debug 版 tests.exe。
- `--release-exe <path>`：指定 Release 版 tests.exe。
- `--mode-csv <path>`：指定模式 CSV。
- `--output-dir <dir>`：指定输出目录（默认 `output`）。
- `--skip-debug` / `--skip-release`：跳过某个构建。
- `--no-case-progress`：关闭用例级进度统计。
- `--no-summary`：执行后不自动运行 summary。

输出结果位于 `output`（或你指定的 `--output-dir`）目录。

## partial_tests.py 使用方法

基础运行：

```powershell
python partial_tests.py
```

常用场景：

```powershell
python partial_tests.py --mode partial
python partial_tests.py --run-mode debug
python partial_tests.py --run-mode release
python partial_tests.py --workers 8
python partial_tests.py --no-progress
```

常用参数：

- `--mode {full,partial}`：全量或增量重测。
- `--run-mode {both,debug,release}`：选择构建目标。
- `--workers <int>`：并行线程数（默认 6）。
- `--group-size <int>`：单个分组内的测例数量（默认 50）。提升能效的同时避免命令行过长，脚本遇到崩溃能够自动剔除死点接力下半场。
- `--debug-exe <path>` / `--release-exe <path>`：覆盖可执行文件路径。
- `--output-dir <dir>`：输出目录（当前默认 `output-partial`）。
- `--result-csv <path>` / `--list-log <path>`：覆盖结果文件路径。

输出目录默认包含：

- `list.log`（默认基于 debug 可执行生成；若 debug exe 不存在，则回退到 release）
- `case_results.csv`
- `list-skipped.csv`
- `list-failed.csv`

## Ctrl+C 行为

- `run_tests.py` 与 `partial_tests.py` 都支持 Ctrl+C 退出。
- 在 Windows 下会通过 Job Object 自动清理已启动的测试子进程。
- 中断退出码为 `130`，方便在 CI 或脚本链路中识别“人为中断”。

## 注意事项

- 脚本基于 Python 3 开发，仅依赖标准库。
- 使用相对路径时，均以脚本所在目录为基准解析，避免受终端当前目录影响。
