"""契约探针驱动：做一次插桩发送，回传四项逆向契约的可观测判定。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from run_selftest import TEXT_DLL, run


def probe(pid=None):
    result = run("--probe", bridge_path=TEXT_DLL, pid=pid, text="WXBOT_PROBE", target="filehelper")
    report = result.get("report") or {}
    observed = report.get("probe") or {}
    probe_file = Path(str(result.get("report_path", "")) + ".probe.json")
    if probe_file.is_file():
        try:
            observed = json.loads(probe_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    probe_file.unlink(missing_ok=True)
    verdicts = {
        "completion_refcount": {"builder_consumed": observed.get("builder_consumed"),
                                "completion_destroys": observed.get("completion_destroys")},
        "request_string_move_or_copy": {"request_body_length": observed.get("request_body_length")},
        "scheduler_function_copy": {"lease_destroys": observed.get("lease_destroys")},
        "error_allocator_header": {"native_status": report.get("native_status")},
    }
    return {"code": result.get("code"), "success": result.get("success") is True,
            "contracts": verdicts, "report_path": result.get("report_path")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行四项逆向契约探针")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--contracts", action="store_true", help="兼容文档写法，无额外行为")
    options = parser.parse_args()
    outcome = probe(pid=options.pid)
    print(json.dumps(outcome, ensure_ascii=False, indent=2), flush=True)
    verdicts = outcome["contracts"]
    # -1 是原生侧的「未观测到」哨兵；失败报告里的 native_status=0 也不代表契约成立。
    complete = all(value is not None and value != -1
                   for item in verdicts.values() for value in item.values())
    sys.exit(0 if outcome["code"] == 0 and outcome["success"] and complete else 1)
