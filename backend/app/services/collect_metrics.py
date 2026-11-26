"""采集量指标领域服务：统计各站点采集-解析-产出的量级健康度，负责波动检测与断采告警。

数据链路位置：观测层，汇聚爬取任务、调度计划、样本库与隔离区上报的量级数据，
产出的健康度结论供采集治理决策与告警联动。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CollectMetricsService(BaseModuleService):
    MODULE = "collect_metrics"
    ENTITY = "指标记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["统计时间", "站点名称", "抓取量", "解析量", "产出量"]
    # 指标页需要按解析窗口筛选，表格列保持原五列。
    FILTER_FIELDS = [*LIST_FIELDS, "解析模板", "样本窗口"]
    REQUIRED_FIELDS = ["统计时间", "站点名称", "抓取量"]
    STATUS_ORDER = ["正常", "量级波动", "断采"]
    ACTION_RULES = {"查看趋势": "量级波动", "忽略波动": "断采"}
    NEGATIVE_ACTIONS = ["忽略波动"]

    # ---- 业务扩展：量级统计与健康度判定 ----

    def record_stats(self, site: str, crawled: int, parsed: int, output: int) -> dict[str, Any]:
        """记录一次站点量级统计；窗口指标行由审计发布独占，这里只写站点汇总行。"""
        if crawled < 0 or parsed < 0 or output < 0:
            return {"ok": False, "message": "量级不能为负数"}
        rows = store.rows(self.MODULE)
        row = next(
            (r for r in rows if r.get("站点名称") == site and not self._window_of(r)),
            None,
        )
        now = datetime.now(timezone.utc).isoformat()
        if row is None:
            mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            row = {
                "id": mid,
                "统计时间": now,
                "站点名称": site,
                "抓取量": str(crawled),
                "解析量": str(parsed),
                "产出量": str(output),
                "status": "正常",
                "pending": True,
                "abnormal": False,
            }
            rows.append(row)
        else:
            row["抓取量"] = str(int(row.get("抓取量", "0") or 0) + crawled)
            row["解析量"] = str(int(row.get("解析量", "0") or 0) + parsed)
            row["产出量"] = str(int(row.get("产出量", "0") or 0) + output)
            row["统计时间"] = now
        return {"ok": True, "message": "量级已记录", "site": site}

    def detect_anomaly(self, entry_id: int) -> dict[str, Any]:
        """检测站点量级异常：断采与波动判定。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        crawled = int(str(entry.get("抓取量", "0") or 0))
        parsed = int(str(entry.get("解析量", "0") or 0))
        output = int(str(entry.get("产出量", "0") or 0))
        if crawled == 0:
            entry["status"] = "断采"
            entry["abnormal"] = True
            return {"ok": True, "message": "检测到断采", "anomaly": "断采"}
        if parsed < crawled * 0.5:
            entry["status"] = "量级波动"
            entry["pending"] = True
            return {"ok": True, "message": "解析量占比过低，量级波动", "anomaly": "量级波动"}
        entry["status"] = "正常"
        entry["abnormal"] = False
        entry["pending"] = False
        return {"ok": True, "message": "量级健康", "anomaly": ""}

    def cross_check_site(self, entry_id: int) -> dict[str, Any]:
        """与站点目录交叉核对指标站点是否仍有效。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "指标记录不存在"}
        site = entry.get("站点名称", "")
        site_row = next(
            (s for s in store.rows("shop_site") if s.get("站点域名") == site), None
        )
        if site_row is None:
            return {"ok": False, "message": f"站点 {site} 不在站点目录"}
        if site_row.get("status") == "已封禁":
            entry["status"] = "断采"
            entry["abnormal"] = True
            return {"ok": True, "message": "站点已封禁，指标标记断采"}
        return {"ok": True, "message": "站点有效", "site_status": site_row.get("status")}

    def aggregate_health(self) -> dict[str, Any]:
        """汇总全部站点的量级健康度。"""
        rows = store.rows(self.MODULE)
        normal = sum(1 for r in rows if r.get("status") == "正常")
        broken = sum(1 for r in rows if r.get("status") == "断采")
        total_crawled = sum(int(str(r.get("抓取量", "0") or 0)) for r in rows)
        total_output = sum(int(str(r.get("产出量", "0") or 0)) for r in rows)
        return {
            "ok": True,
            "message": "健康度汇总完成",
            "sites": len(rows),
            "normal": normal,
            "broken": broken,
            "total_crawled": total_crawled,
            "total_output": total_output,
            "yield_rate": f"{(total_output / total_crawled):.0%}" if total_crawled else "0%",
        }

    def notify_quarantine_pressure(self, entry_id: int) -> dict[str, Any]:
        """隔离区压力大时把压力传导到指标异常标记。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "指标记录不存在"}
        pending_quarantine = sum(
            1 for q in store.rows("failure_quarantine")
            if q.get("status") in ("待处理", "重试中")
        )
        output = int(str(entry.get("产出量", "0") or 0))
        if pending_quarantine > max(output * 0.2, 5):
            entry["status"] = "量级波动"
            entry["abnormal"] = True
            return {
                "ok": True,
                "message": "隔离区压力已传导",
                "pending_quarantine": pending_quarantine,
            }
        return {"ok": True, "message": "隔离区压力正常", "pending_quarantine": pending_quarantine}

    def trend_report(self, site: str) -> dict[str, Any]:
        """输出站点量级趋势报告供告警规则参考。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("站点名称") == site
            and self._window_of(r) == ""
        ]
        if not rows:
            return {"ok": False, "message": f"站点 {site} 没有指标记录"}
        latest = rows[-1]
        return {
            "ok": True,
            "message": "趋势报告已生成",
            "site": site,
            "crawled": latest.get("抓取量", ""),
            "parsed": latest.get("解析量", ""),
            "output": latest.get("产出量", ""),
            "status": latest.get("status", ""),
        }

    # ---- 业务扩展：消费解析质量审计的窗口报告 ----

    @staticmethod
    def _window_of(row: dict[str, Any]) -> str:
        return str(row.get("样本窗口") or "").strip()

    def _window_row(
        self, site: str, template: str, window: str
    ) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                str(row.get("站点名称", "")).strip() == site
                and str(row.get("样本窗口", "")).strip() == window
                and str(row.get("解析模板", "")).strip() == template
            ):
                return row
        return None

    def consume_audit_report(
        self,
        *,
        site: str,
        template: str,
        window: str,
        report_id: int,
        numerator: int,
        denominator: int,
        rate_text: str,
        failure_category: str,
        receipt_id: str = "",
    ) -> dict[str, Any]:
        """消费解析质量报告：把分子分母与失败类别落到该站点模板窗口的指标状态。

        - 只按「站点 + 模板 + 样本窗口」精确匹配，旁边窗口的指标不被改写。
        - 重复提交同一份报告只覆盖同一行，不会累加分母。
        - 分母为 0 时不写 0% 命中率，指标标记「暂无样本」。
        """
        site = str(site or "").strip()
        template = str(template or "").strip()
        window = str(window or "").strip()
        now = datetime.now(timezone.utc).isoformat()

        row = self._window_row(site, template, window)
        if row is None:
            rows = store.rows(self.MODULE)
            mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            row = {
                "id": mid,
                "统计时间": now,
                "站点名称": site,
                "解析模板": template,
                "样本窗口": window,
                "抓取量": "0",
                "解析量": "0",
                "产出量": "0",
                "status": "正常",
                "pending": True,
                "abnormal": False,
                "分母": "0",
                "分子": "0",
                "命中率": "暂无样本",
                "失败类别": "暂无样本",
                "审计回执号": "",
                "审计明细id": report_id,
            }
            rows.append(row)

        if denominator <= 0:
            row["抓取量"] = "0"
            row["解析量"] = "0"
            row["分母"] = "0"
            row["分子"] = "0"
            row["命中率"] = "暂无样本"
            row["失败类别"] = failure_category or "暂无样本"
            row["status"] = "正常"
            row["pending"] = True
            row["abnormal"] = False
            row["统计时间"] = now
            row["审计回执号"] = receipt_id
            row["审计明细id"] = report_id
            return {
                "ok": True,
                "message": "暂无样本，指标不计算命中率",
                "metrics_id": row.get("id"),
                "status": row["status"],
                "denominator": 0,
                "numerator": 0,
                "rate": "暂无样本",
            }

        ratio = numerator / denominator
        row["抓取量"] = str(denominator)
        row["解析量"] = str(numerator)
        row["产出量"] = str(numerator)
        row["分母"] = str(denominator)
        row["分子"] = str(numerator)
        row["命中率"] = rate_text or "暂无样本"
        row["失败类别"] = failure_category or ("无失败" if numerator == denominator else "部分未命中")
        row["统计时间"] = now
        row["审计回执号"] = receipt_id
        row["审计明细id"] = report_id
        if ratio >= 0.9:
            row["status"] = "正常"
            row["pending"] = False
            row["abnormal"] = False
        else:
            # 有抓取但解析产出不足，按量级波动处理；全未命中时失败类别单独留痕。
            row["status"] = "量级波动"
            row["pending"] = True
            row["abnormal"] = ratio < 0.6
        return {
            "ok": True,
            "message": "解析质量口径已消费",
            "metrics_id": row.get("id"),
            "status": row["status"],
            "denominator": denominator,
            "numerator": numerator,
            "rate": row["命中率"],
            "failure_category": row["失败类别"],
            "receipt_id": receipt_id,
        }

    def audit_report_view(self, site: str, template: str, window: str) -> dict[str, Any]:
        """按原窗口读取指标页采用的解析质量口径，供审计核对。"""
        site = str(site or "").strip()
        template = str(template or "").strip()
        window = str(window or "").strip()
        row = self._window_row(site, template, window)
        if row is None:
            return {"ok": False, "message": f"窗口 {window} 尚无指标记录"}
        return {
            "ok": True,
            "message": "指标口径已读取",
            "site": site,
            "template": template,
            "window": window,
            "metrics_id": row.get("id"),
            "denominator": int(str(row.get("分母", "0") or 0)),
            "numerator": int(str(row.get("分子", "0") or 0)),
            "rate": row.get("命中率", "暂无样本"),
            "failure_category": row.get("失败类别", ""),
            "status": row.get("status", ""),
            "receipt_id": row.get("审计回执号", ""),
        }

    def list_window_metrics(self) -> dict[str, Any]:
        """列出带样本窗口的指标记录，旁边时段独立展示。"""
        rows = [
            {
                "id": row.get("id"),
                "站点名称": row.get("站点名称", ""),
                "解析模板": row.get("解析模板", ""),
                "样本窗口": row.get("样本窗口", ""),
                "分母": int(str(row.get("分母", "0") or 0)),
                "分子": int(str(row.get("分子", "0") or 0)),
                "命中率": row.get("命中率", "暂无样本"),
                "失败类别": row.get("失败类别", ""),
                "status": row.get("status", ""),
                "审计回执号": row.get("审计回执号", ""),
                "统计时间": row.get("统计时间", ""),
            }
            for row in store.rows(self.MODULE)
            if self._window_of(row)
        ]
        return {"ok": True, "message": "窗口指标已读取", "metrics": rows}
