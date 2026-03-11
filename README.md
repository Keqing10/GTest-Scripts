# GTest 日志汇总工具

这个仓库包含两段 Python 脚本，用于运行和汇总 Google Test (gtest) 的日志。包含进度显示、子进程清理和 CSV 导出等扩展功能。

## 主要脚本

* `run_tests.py` – 依照 `mode.csv` 中列出的模式并行启动 debug/
  release 两种构建，对每个模式显示实时进度和输出，并在结束
  时自动调用 `summary.py`。
* `summary.py` – 读取 `output` 目录下的 `debug-*.log` 和
  `release-*.log`，提取测试总数、通过/失败/跳过计数、耗时及通
  过率，并生成 `summary.csv` 以及跳过/失败用例的清单文件。
* `mode.csv` – 定义各测试模式及顺序的配置文件。


## 使用方法

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

## 注意事项

* 脚本基于 Python 3.14 开发，仅使用标准库，无需额外依赖。
* Windows 下使用 Job Object 来确保脚本退出时子进程被一并杀死。
* 汇总表包括 debug/release 的通过率，精确到小数点后五位；
  若 `summary.csv` 被占用，脚本会发出警告但仍继续运行。
