"""契约探针驱动：做一次插桩发送，回传四项逆向契约的可观测判定。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from run_selftest import TEXT_DLL, run


def probe(pid=None):
    result = run("--probe", bridge_path=TEXT_DLL, pid=pid, text="WXBOT_PROBE", target="filehelper")
    report = result.get("report") or {}
    observed = report.get("probe") or {}
    verdicts = {
        "completion_refcount": {"builder_consumed": observed.get("builder_consumed"),
                                "completion_destroys": observed.get("completion_destroys")},
        "request_string_move_or_copy": {"request_body_length": observed.get("request_body_length")},
        "scheduler_function_copy": {"lease_destroys": observed.get("lease_destroys")},
        "error_allocator_header": {"native_status": report.get("native_status")},
    }
    return {"code": result.get("code"), "contracts": verdicts, "report_path": result.get("report_path")}


if __name__ == "__main__":
    outcome = probe()
    print(json.dumps(outcome, ensure_ascii=False, indent=2), flush=True)
    sys.exit(0 if outcome["code"] == 0 else 1)
