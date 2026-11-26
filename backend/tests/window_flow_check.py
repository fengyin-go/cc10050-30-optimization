"""解析质量审计窗口工作流的回归脚本（纯标准库，直接打 HTTP 接口）。

用法：先起后端，然后
    python3 -I backend/tests/window_flow_check.py --base http://127.0.0.1:8020
脚本会在 站点E / E站价格页模板 / E18、E19 上复现值班场景，并断言：
空样本不落 0%、全未命中记真 0%、补报只进所属窗口、重复提交不涨分母、旧回执可查。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

SITE = "站点E"
TEMPLATE = "E站价格页模板"
WINDOWS = ("E18", "E19")

failures: list[str] = []


def check(label: str, condition: bool, detail: Any = "") -> None:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail != "" else ""))
    if not condition:
        failures.append(label)


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None, **query: str) -> Any:
    url = base + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def windows(base: str) -> dict[str, dict[str, Any]]:
    payload = call(base, "GET", "/api/parse_audit/windows")
    return {item["样本窗口"]: item for item in payload["windows"]}


def metrics(base: str) -> dict[str, Any]:
    payload = call(base, "GET", "/api/collect_metrics/windows")
    return {
        item["样本窗口"]: item
        for item in payload["metrics"]
        if item["站点名称"] == SITE and item["解析模板"] == TEMPLATE
    }


def key(window: str) -> dict[str, str]:
    return {"site": SITE, "template": TEMPLATE, "window": window}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print("1) 恢复待发布空报告 E18，不伪造 0%")
    resumed = call(base, "POST", "/api/parse_audit/windows/open", key("E18"))
    window = resumed["window"]
    check("E18 仍为空样本", window["样本总数"] == 0, window["样本总数"])
    check("E18 命中率是「暂无样本」而不是 0%", window["字段命中率"] == "暂无样本", window["字段命中率"])
    check("E18 发布状态为确认中", window["发布状态"] == "确认中", window["发布状态"])

    print("2) 空报告重试发布仍为确认中")
    retried = call(base, "POST", "/api/parse_audit/windows/retry-publish", key("E18"))
    check("空报告未标为已发布", retried["published"] is False)
    check("回执分母为 0 且不写 0%", retried["receipt"]["分母"] == 0 and retried["receipt"]["命中率"] == "暂无样本")
    check("指标页不被写成 0%", metrics(base).get("E18", {}).get("命中率") == "暂无样本")

    print("3) 同一窗口迟到两条均解析失败，一条旁窗样本被拒收")
    backfill = call(base, "POST", "/api/parse_audit/windows/backfill", {
        **key("E18"),
        "samples": [
            {"样本指纹": "late-1", "命中结果": "未命中", "失败原因": "价格节点缺失"},
            {"样本指纹": "late-2", "命中结果": "未命中", "失败原因": "选择器失配"},
            {"样本指纹": "late-3", "样本窗口": "E19", "命中结果": "未命中"},
        ],
    })
    window = backfill["window"]
    check("只接收 2 条", backfill["accepted"] == 2, backfill["accepted"])
    check("旁窗样本被拒收", len(backfill["rejected"]) == 1, backfill["rejected"])
    check("分母为 2", window["样本总数"] == 2, window["样本总数"])
    check("有样本全未命中记真 0.00%", window["字段命中率"] == "0.00%", window["字段命中率"])
    check("判定为确有样本但未命中", window["样本判定"] == "确有样本但未命中", window["样本判定"])
    check("补报等待确认", window["发布状态"] == "补报等待确认", window["发布状态"])

    print("4) 重试发布，从空态变为真 0%")
    published = call(base, "POST", "/api/parse_audit/windows/retry-publish", key("E18"))
    receipt = published["receipt"]
    check("已发布", published["published"] is True)
    check("分母 2 / 分子 0", (receipt["分母"], receipt["分子"]) == (2, 0), f"{receipt['分母']}/{receipt['分子']}")
    check("命中率 0.00% 且失败类别记录", receipt["命中率"] == "0.00%" and receipt["失败类别"] == "解析全未命中")
    check("口径写明分子分母", "0/2" in receipt["口径"], receipt["口径"])
    check("指标消费失败类别", metrics(base).get("E18", {}).get("失败类别") == "解析全未命中")

    print("5) 重投一份更新，总数只可为 2，E19 不变")
    again = call(base, "POST", "/api/parse_audit/windows/retry-publish", key("E18"))
    check("重投后分母仍为 2", again["receipt"]["分母"] == 2, again["receipt"]["分母"])
    check("E19 仍为空样本", windows(base)["E19"]["样本总数"] == 0)
    check("E19 命中率仍为暂无样本", windows(base)["E19"]["字段命中率"] == "暂无样本")

    print("6) 重复提交补报不涨分母，旧回执保留")
    dup = call(base, "POST", "/api/parse_audit/windows/backfill", {
        **key("E18"), "samples": [{"样本指纹": "late-1"}, {"样本指纹": "late-2"}],
    })
    check("重复 2 条、新增 0 条", (dup["accepted"], dup["duplicates"]) == (0, 2), f"{dup['accepted']}/{dup['duplicates']}")
    check("分母仍为 2", dup["window"]["样本总数"] == 2, dup["window"]["样本总数"])
    receipts = call(base, "GET", "/api/parse_audit/windows/receipt", **key("E18"))["receipts"]
    check("历史回执可查", len(receipts) >= 3, len(receipts))
    check("首版空回执仍在", receipts[0]["命中率"] == "暂无样本", receipts[0]["命中率"])

    print("7) 重新进入同一窗口恢复原进度，不重置")
    resumed = call(base, "POST", "/api/parse_audit/windows/open", key("E18"))["window"]
    check("版本保留", resumed["版本"] == again["version"], resumed["版本"])
    check("分母仍为 2", resumed["样本总数"] == 2, resumed["样本总数"])
    check("回执历史保留", len(resumed["回执历史"]) >= 3, len(resumed["回执历史"]))

    print("8) 按审计回执核对指标口径")
    verified = call(base, "GET", "/api/parse_audit/windows/verify", **key("E18"))
    check("口径一致", verified["consistent"] is True, verified["differences"])
    rows = metrics(base)
    check("指标分母为 2", rows["E18"]["分母"] == 2, rows["E18"]["分母"])
    check("旁边窗口 E17 不受影响", rows.get("E17", {}).get("分母") == 3, rows.get("E17", {}).get("分母"))

    print()
    if failures:
        print(f"失败的断言 {len(failures)} 项：")
        for item in failures:
            print(f" - {item}")
        return 1
    print("全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
