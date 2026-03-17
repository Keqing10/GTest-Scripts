from __future__ import annotations

import re


# --gtest_list_tests 解析规则。
SUITE_RE = re.compile(r"^(\S+)\.$")
LIST_CASE_RE = re.compile(r"^\s{2}(\S.*)$")
LIST_COMMENT_RE = re.compile(r"^\s{2}#")

# 单用例执行输出中的状态行解析规则。
FAILED_CASE_RE = re.compile(r"^\[\s+FAILED\s+\]\s+", re.MULTILINE)
SKIPPED_CASE_RE = re.compile(r"^\[\s+SKIPPED\s+\]\s+", re.MULTILINE)
PASSED_CASE_RE = re.compile(r"^\[\s+(OK|PASSED)\s+\]\s+", re.MULTILINE)
# run_tests 流式读取时用于识别“单个 case 完成”行。
COMPLETE_CASE_LINE_RE = re.compile(r"^\[\s*(OK|FAILED|SKIPPED)\s*\]")

# 执行日志中带耗时的 FAILED/SKIPPED 用例行。
NAMED_CASE_STATUS_RE = re.compile(r"^\[\s+(FAILED|SKIPPED)\s+\]\s+(.+?)\s+\((\d+)\s+ms\)\s*$")

# 单单个 case 启动标志
RUN_CASE_RE = re.compile(r"^\[\s*RUN\s*\]\s+(.+?)\s*$")

# 提取每一行明确的结果（包括 PASSED/OK, FAILED, SKIPPED）及测例名/耗时
COMPLETE_CASE_RESULT_RE = re.compile(r"^\[\s*(OK|PASSED|FAILED|SKIPPED)\s*\]\s+(.+?)(?:\s+\((\d+)\s+ms\))?\s*$")

# 汇总段解析规则。
SUMMARY_MARKER = "[----------] Global test environment tear-down"
SUMMARY_TOTAL_RE = re.compile(r"\[==========\]\s+(\d+)\s+tests?\s+from\s+.*ran\.\s+\((\d+)\s+ms\s+total\)")
SUMMARY_PASSED_RE = re.compile(r"\[\s+PASSED\s+\]\s+(\d+)\s+tests?")
SUMMARY_FAILED_RE = re.compile(r"\[\s+FAILED\s+\]\s+(\d+)\s+tests?")
SUMMARY_SKIPPED_RE = re.compile(r"\[\s+SKIPPED\s+\]\s+(\d+)\s+tests?")


def parse_gtest_list_output(output_text: str) -> list[str]:
    """解析 --gtest_list_tests 输出，返回去重保序后的完整用例名。"""

    suite_name = ""
    cases: list[str] = []
    for line in output_text.splitlines():
        if not line.strip():
            continue

        suite_match = SUITE_RE.match(line.strip())
        if suite_match:
            suite_name = suite_match.group(1)
            continue

        if LIST_COMMENT_RE.match(line):
            continue

        case_match = LIST_CASE_RE.match(line)
        if case_match and suite_name:
            case_leaf = case_match.group(1).split("#", 1)[0].strip()
            if case_leaf:
                cases.append(f"{suite_name}.{case_leaf}")

    seen: set[str] = set()
    ordered: list[str] = []
    for name in cases:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def count_gtest_list_cases(output_text: str) -> int:
    """统计 --gtest_list_tests 输出中的真实用例条目数量。"""

    return sum(
        1
        for line in output_text.splitlines()
        if LIST_CASE_RE.match(line) and not LIST_COMMENT_RE.match(line)
    )


def infer_single_case_status(output_text: str) -> str:
    """从单用例执行输出中推断 PASS/FAIL/SKIPPED；无法识别时返回 UNKNOWN。"""

    if FAILED_CASE_RE.search(output_text):
        return "FAIL"
    if SKIPPED_CASE_RE.search(output_text):
        return "SKIPPED"
    if PASSED_CASE_RE.search(output_text):
        return "PASS"
    return "UNKNOWN"


def is_complete_case_line(line: str) -> bool:
    """判断一行输出是否表示 gtest 单个 case 已完成。"""

    return bool(COMPLETE_CASE_LINE_RE.match(line))


def parse_batch_case_statuses(log_text: str) -> tuple[set[str], dict[str, str], dict[str, int]]:
    """批量解析一次执行输出，返回 (已开始集合, 已完成状态, 已完成耗时ms)。"""
    
    started_cases = set()
    parsed_results: dict[str, str] = {}
    elapsed_ms: dict[str, int] = {}
    for line in log_text.splitlines():
        run_match = RUN_CASE_RE.match(line)
        if run_match:
            started_cases.add(run_match.group(1).strip())
            continue

        match = COMPLETE_CASE_RESULT_RE.match(line)
        if match:
            status, case_name, elapsed = match.groups()
            case_name = case_name.strip()
            if status in ("OK", "PASSED"):
                parsed_results[case_name] = "PASS"
            elif status == "FAILED":
                parsed_results[case_name] = "FAIL"
            elif status == "SKIPPED":
                parsed_results[case_name] = "SKIPPED"

            if elapsed is not None:
                elapsed_ms[case_name] = int(elapsed)

    return started_cases, parsed_results, elapsed_ms


def extract_named_cases_by_status(log_text: str, status_name: str) -> set[str]:
    """提取日志中指定状态（FAILED/SKIPPED）的用例名。"""

    results: set[str] = set()
    for line in log_text.splitlines():
        match = NAMED_CASE_STATUS_RE.match(line)
        if not match:
            continue

        current_status, payload, _elapsed_ms = match.groups()
        if current_status != status_name:
            continue

        payload = payload.strip()
        if payload:
            results.add(payload)

    return results


def parse_summary_from_log_text(log_text: str) -> dict[str, int | float | str]:
    """从整份 gtest 日志文本中提取汇总段统计。"""

    result: dict[str, int | float | str] = {
        "Total": "-",
        "Passed": "-",
        "Failed": "-",
        "Skipped": "-",
        "TimeMin": "-",
        "TimeMs": "-",
    }

    if not log_text:
        return result

    marker_index = log_text.rfind(SUMMARY_MARKER)
    if marker_index < 0:
        return result

    tail = log_text[marker_index:]

    total_match = SUMMARY_TOTAL_RE.search(tail)
    if total_match:
        total = int(total_match.group(1))
        time_ms = int(total_match.group(2))
        result["Total"] = total
        result["TimeMs"] = time_ms
        result["TimeMin"] = round(time_ms / 60000.0, 2)

    passed_match = SUMMARY_PASSED_RE.search(tail)
    if passed_match:
        result["Passed"] = int(passed_match.group(1))

    skipped_match = SUMMARY_SKIPPED_RE.search(tail)
    if skipped_match:
        result["Skipped"] = int(skipped_match.group(1))

    failed_match = SUMMARY_FAILED_RE.search(tail)
    if failed_match:
        result["Failed"] = int(failed_match.group(1))

    if result["Total"] != "-" and result["Skipped"] == "-":
        result["Skipped"] = 0
    if result["Total"] != "-" and result["Failed"] == "-":
        result["Failed"] = 0

    return result