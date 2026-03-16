# GTest 日志汇总工具

这个仓库包含三段 Python 脚本，用于运行和汇总 Google Test (gtest) 日志。包含并行执行、进度显示、子进程清理和 CSV 导出等功能。

## 主要脚本

* `run_tests.py` – 依照 `mode.csv` 中列出的模式并行启动 debug/
  release 两种构建，对每个模式显示实时进度和输出，并在结束
  时自动调用 `summary.py`。
* `summary.py` – 读取 `output` 目录下的 `debug-*.log` 和
  `release-*.log`，提取测试总数、通过/失败/跳过计数、耗时及通
  过率，并生成 `summary.csv` 以及跳过/失败用例的清单文件。
* `partial_tests.py` – 按“每次只跑一个测例”的方式并行执行 gtest。
   支持 `full`（全量）和 `partial`（增量）两种模式，并输出
   `output-part/list.log`、`output-part/case_results.csv`、
   `output-part/list-skipped.csv`、`output-part/list-failed.csv`。
* `mode.csv` – 定义各测试模式及顺序的配置文件。


## 分类测试使用方法

1. 将 `mode.csv` 放在脚本同一目录（默认位置），或通过
   `--mode-csv` 指定其他路径。
2. 在 PowerShell 或命令行中运行：

   ```powershell
   python run_tests.py
   ```

   可选参数：
   * `--gtest-list`：使用二进制查询测例数（默认行为，可跳过此
     参数）。
   * `--skip-debug` / `--skip-release`：跳过某一配置的测试。
   * `--quiet`：不在控制台输出表格。

3. 执行完成后，`output` 目录里会有各条日志及
   `summary.csv`、`list-skipped.csv`、`list-failed.csv`。
4. 若只需重新生成汇总，可单独运行：

   ```powershell
   python summary.py
   ```

## partial_tests.py 使用方法

`partial_tests.py` 与 `run_tests.py` 独立，适合“按用例粒度并行重测”。

1. 默认执行（按脚本文件头配置）：

   ```powershell
   python partial_tests.py
   ```

2. 增量重测（只跑未 `debug=PASS` 且 `release=PASS` 的用例）：

   ```powershell
   python partial_tests.py --mode partial
   ```

3. 仅跑一种构建：

   ```powershell
   python partial_tests.py --run-mode debug
   python partial_tests.py --run-mode release
   ```

4. 自定义可执行文件与并行度：

   ```powershell
   python partial_tests.py --debug-exe D:\\path\\Debug\\tests.exe --release-exe D:\\path\\Release\\tests.exe --workers 8
   ```

5. 关闭进度条（进一步降低终端刷新开销）：

   ```powershell
   python partial_tests.py --no-progress
   ```

输出文件默认位于 `output-part` 目录：
* `list.log`：全部测例名清单（由 `--gtest_list_tests` 生成）。
* `case_results.csv`：三列结果文件（`case_name`, `debug_pass`, `release_pass`）。
* `list-skipped.csv`：跳过用例清单（列：`case_name`, `debug`, `release`）。
* `list-failed.csv`：失败用例清单（列：`case_name`, `debug`, `release`）。

## 注意事项

* 脚本基于 Python 3.14 开发，仅使用标准库，无需额外依赖。
* Windows 下使用 Job Object 来确保脚本退出时子进程被一并杀死。
* 汇总表包括 debug/release 的通过率，精确到小数点后五位；
  若 `summary.csv` 被占用，脚本会发出警告但仍继续运行。
* `partial_tests.py` 的性能主要受 `--workers` 和测试本身耗时影响；
   进度条采用低频刷新，默认资源占用较低。
