"""解析质量审计领域服务：审计解析模板的字段命中率与失败分布，负责命中率劣化告警与沉淀。

数据链路位置：质量审计层，消费页面解析模板上报的命中数据与失败隔离区的
失败原因分布，产出的审计结论供模板版本升级决策，并向采集量指标发布解析口径。

窗口报告约定：
- 报告以「站点名称 + 解析模板 + 样本窗口」唯一标识，例如 站点E / E站价格页模板v2 / E18。
- 样本只有归属到同一站点、同一模板、同一窗口时才计入该窗口分母，迟到补报同样遵守。
- 空样本不计算命中率，字段记「暂无样本」，绝不落成 0%；有样本但全部未命中才记录真 0.00%。
- 补报按样本指纹去重，重复提交不会重复计入分母；每次发布都会追加回执，旧回执保留可查。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

HIT = "命中"
MISS = "未命中"

EMPTY_STATE = "暂无样本"
ALL_MISS_STATE = "确有样本但未命中"
PARTIAL_STATE = "部分命中"
ALL_HIT_STATE = "全部命中"

PENDING_PUBLISH = "待发布"
CONFIRMING = "确认中"
BACKFILL_PENDING = "补报等待确认"
PUBLISHED = "已发布"

_HIT_TOKENS = {"命中", "解析成功", "成功", "hit", "true", "1", "yes"}
_MISS_TOKENS = {"未命中", "解析失败", "失败", "miss", "false", "0", "no"}


class ParseAuditService(BaseModuleService):
    MODULE = "parse_audit"
    ENTITY = "审计明细"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["审计时间", "解析模板", "样本总数", "字段命中率", "失败原因分布"]
    # 审计页可按站点/窗口筛选，但表格仍只展示五列。
    FILTER_FIELDS = [*LIST_FIELDS, "站点名称", "样本窗口", "报告类型"]
    REQUIRED_FIELDS = ["审计时间", "解析模板", "样本总数"]
    STATUS_ORDER = ["正常", "命中率下降", "解析异常"]
    ACTION_RULES = {"查看详情": "命中率下降", "忽略告警": "解析异常"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    # ---- 窗口报告基础工具 ----

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    def _find_window(
        self, site: str, template: str, window: str
    ) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                row.get("报告类型") == "窗口报告"
                and self._text(row.get("站点名称")) == site
                and self._text(row.get("解析模板")) == template
                and self._text(row.get("样本窗口")) == window
            ):
                return row
        return None

    def _window_samples(self, site: str, template: str, window: str) -> list[dict[str, Any]]:
        return [
            sample for sample in store.rows("sample_store")
            if self._text(sample.get("站点名称")) == site
            and self._text(sample.get("解析模板")) == template
            and self._text(sample.get("样本窗口")) == window
        ]

    def _find_metrics_row(
        self, site: str, template: str, window: str
    ) -> dict[str, Any] | None:
        for row in store.rows("collect_metrics"):
            if (
                self._text(row.get("站点名称")) == site
                and self._text(row.get("样本窗口")) == window
                and self._text(row.get("解析模板")) == template
            ):
                return row
        return None

    def _validate_template(self, site: str, template: str) -> tuple[dict[str, Any] | None, str]:
        if not site:
            return None, "站点名称不能为空"
        if not template:
            return None, "解析模板不能为空"
        # 窗口以「站点 + 模板 + 样本窗口」唯一标识；已登记模板必须归属本站点，
        # 否则旁站模板会把样本混进不该进的窗口。
        template_row = next(
            (t for t in store.rows("page_parser") if self._text(t.get("模板名称")) == template),
            None,
        )
        if template_row is not None:
            owner = self._text(template_row.get("适用站点"))
            if owner and owner != site:
                return None, f"模板 {template} 归属站点 {owner}，不能用于站点 {site} 的样本窗口"
        return template_row, ""

    def _new_window_row(self, site: str, template: str, window: str) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        aid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        now = self._now()
        row = {
            "id": aid,
            "审计时间": now,
            "更新时间": now,
            "解析模板": template,
            "样本总数": "0",
            "字段命中率": EMPTY_STATE,
            "失败原因分布": "",
            "status": "正常",
            "pending": True,
            "abnormal": False,
            # 窗口报告扩展字段
            "报告类型": "窗口报告",
            "站点名称": site,
            "样本窗口": window,
            "命中数": "0",
            "未命中数": "0",
            "样本判定": EMPTY_STATE,
            "发布状态": PENDING_PUBLISH,
            "版本": "0",
            "失败类别": EMPTY_STATE,
            "当前回执": None,
            "回执历史": [],
        }
        rows.append(row)
        return row

    def _recount(self, report: dict[str, Any]) -> dict[str, Any]:
        """按窗口内去重样本重算分子分母；空样本不产生 0% 命中率。"""
        site = self._text(report.get("站点名称"))
        template = self._text(report.get("解析模板"))
        window = self._text(report.get("样本窗口"))
        samples = self._window_samples(site, template, window)
        total = len(samples)
        hits = sum(1 for sample in samples if self._text(sample.get("命中结果")) == HIT)
        misses = total - hits
        report["命中数"] = str(hits)
        report["未命中数"] = str(misses)
        report["样本总数"] = str(total)
        report["更新时间"] = self._now()

        reason_counts: dict[str, int] = {}
        for sample in samples:
            if self._text(sample.get("命中结果")) == HIT:
                continue
            reason = self._text(sample.get("失败原因")) or "解析失败"
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

        if total == 0:
            # 空窗口：命中率未知，不伪造 0%。
            report["字段命中率"] = EMPTY_STATE
            report["样本判定"] = EMPTY_STATE
            report["失败类别"] = EMPTY_STATE
            report["失败原因分布"] = ""
            report["status"] = "正常"
            report["abnormal"] = False
            report["pending"] = True
            return report

        rate = hits / total
        report["字段命中率"] = f"{rate:.2%}"
        if hits == 0:
            report["样本判定"] = ALL_MISS_STATE
            report["失败类别"] = "解析全未命中"
        elif hits == total:
            report["样本判定"] = ALL_HIT_STATE
            report["失败类别"] = "无失败"
        else:
            report["样本判定"] = PARTIAL_STATE
            report["失败类别"] = "部分未命中"
        report["失败原因分布"] = ";".join(
            f"{reason}:{count}" for reason, count in sorted(
                reason_counts.items(), key=lambda item: (-item[1], item[0])
            )[:3]
        )
        report["status"] = "正常" if rate >= 0.9 else ("命中率下降" if rate >= 0.6 else "解析异常")
        # 已忽略的告警不会被重新计数恢复成待处理/异常；命中率仍按样本真实重算。
        ignored = bool(report.get("告警已忽略"))
        report["abnormal"] = rate < 0.6 and not ignored
        report["pending"] = not ignored and (
            rate < 0.9 or self._text(report.get("发布状态")) != PUBLISHED
        )
        return report

    @staticmethod
    def _normalize_hit(value: Any) -> str:
        token = str(value).strip().casefold() if value is not None else ""
        if token in {item.casefold() for item in _HIT_TOKENS}:
            return HIT
        if token in {item.casefold() for item in _MISS_TOKENS}:
            return MISS
        return ""

    def _normalize_sample(
        self,
        raw: dict[str, Any],
        *,
        site: str,
        template: str,
        window: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """校验一条补报样本是否属于目标站点/模板/窗口，并生成稳定指纹。"""
        declared_site = self._text(raw.get("站点名称") or raw.get("site"))
        declared_template = self._text(raw.get("解析模板") or raw.get("template"))
        declared_window = self._text(raw.get("样本窗口") or raw.get("window"))
        if declared_site and declared_site != site:
            return None, f"样本站点 {declared_site} 不属于 {site}"
        if declared_template and declared_template != template:
            return None, f"样本模板 {declared_template} 不属于 {template}"
        if declared_window and declared_window != window:
            return None, f"样本窗口 {declared_window} 不属于 {window}"

        fingerprint = self._text(
            raw.get("样本指纹") or raw.get("fingerprint") or raw.get("指纹")
        )
        summary = self._text(raw.get("原始HTML摘要") or raw.get("html") or raw.get("摘要"))
        if not fingerprint:
            if not summary:
                return None, "缺少样本指纹或原始HTML摘要，无法幂等去重"
            digest = hashlib.sha1(
                f"{site}|{template}|{window}|{summary}".encode("utf-8")
            ).hexdigest()[:16]
            fingerprint = f"fp-{digest}"

        result = self._normalize_hit(
            raw.get("命中结果") if "命中结果" in raw else raw.get("hit_result")
        )
        if not result:
            if "命中" in raw and isinstance(raw.get("命中"), bool):
                result = HIT if raw.get("命中") else MISS
            elif "hit" in raw and isinstance(raw.get("hit"), bool):
                result = HIT if raw.get("hit") else MISS
            elif self._text(raw.get("失败原因") or raw.get("failure_reason")):
                result = MISS
            else:
                # 迟到补报默认按解析未命中处理，避免把失败样本伪装成命中。
                result = MISS

        reason = self._text(raw.get("失败原因") or raw.get("failure_reason"))
        if result == MISS and not reason:
            reason = "解析失败"

        return {
            "site": site,
            "template": template,
            "window": window,
            "fingerprint": fingerprint,
            "result": result,
            "reason": reason,
            "summary": summary or f"{window}-{fingerprint}",
            "captured_at": self._text(
                raw.get("抓取时间") or raw.get("captured_at")
            ) or self._now(),
            "late": bool(raw.get("late", True)),
        }, ""

    def _append_sample(self, sample: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("sample_store")
        sid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        row = {
            "id": sid,
            "样本批次": f"{sample['window']}-{sample['site']}",
            "站点名称": sample["site"],
            "原始HTML摘要": sample["summary"],
            "抓取时间": sample["captured_at"],
            "样本指纹": sample["fingerprint"],
            "status": "已解析" if sample["result"] == HIT else "已保存",
            "pending": sample["result"] != HIT,
            "abnormal": False,
            # 审计窗口关联字段
            "解析模板": sample["template"],
            "样本窗口": sample["window"],
            "命中结果": sample["result"],
            "失败原因": sample["reason"],
            "迟到补报": sample["late"],
        }
        rows.append(row)
        return row

    def _snapshot(self, report: dict[str, Any]) -> dict[str, Any]:
        if self._text(report.get("样本窗口")):
            self._recount(report)
        metrics_row = self._find_metrics_row(
            self._text(report.get("站点名称")),
            self._text(report.get("解析模板")),
            self._text(report.get("样本窗口")),
        )
        return {
            "审计明细id": report.get("id"),
            "站点名称": report.get("站点名称", ""),
            "解析模板": report.get("解析模板", ""),
            "样本窗口": report.get("样本窗口", ""),
            "样本总数": int(self._text(report.get("样本总数")) or 0),
            "命中数": int(self._text(report.get("命中数")) or 0),
            "未命中数": int(self._text(report.get("未命中数")) or 0),
            "字段命中率": report.get("字段命中率", EMPTY_STATE),
            "样本判定": report.get("样本判定", EMPTY_STATE),
            "发布状态": report.get("发布状态", PENDING_PUBLISH),
            "版本": int(self._text(report.get("版本")) or 0),
            "失败类别": report.get("失败类别", EMPTY_STATE),
            "失败原因分布": report.get("失败原因分布", ""),
            "status": report.get("status", "正常"),
            "当前回执": report.get("当前回执"),
            "回执历史": report.get("回执历史", []),
            "指标口径": (
                {
                    "指标记录id": metrics_row.get("id"),
                    "分母": metrics_row.get("分母"),
                    "分子": metrics_row.get("分子"),
                    "命中率": metrics_row.get("命中率"),
                    "失败类别": metrics_row.get("失败类别"),
                    "指标状态": metrics_row.get("status"),
                    "审计回执号": metrics_row.get("审计回执号"),
                }
                if metrics_row is not None
                else None
            ),
        }

    # ---- 业务扩展：窗口报告的打开、恢复、补报与发布 ----

    def open_window(
        self, site: str, template: str, window: str, *, note: str = ""
    ) -> dict[str, Any]:
        """打开或恢复站点模板的样本窗口；重复进入只恢复进度，不重置计数。"""
        site, template, window = self._text(site), self._text(template), self._text(window)
        if not window:
            return {"ok": False, "message": "样本窗口不能为空"}
        _, error = self._validate_template(site, template)
        if error:
            return {"ok": False, "message": error}
        report = self._find_window(site, template, window)
        if report is None:
            report = self._new_window_row(site, template, window)
            self._recount(report)
            message = f"窗口 {window} 已打开，当前暂无样本"
        else:
            # 恢复进度：重新按样本库重算，保留原版本与回执历史。
            self._recount(report)
            message = f"窗口 {window} 已恢复原进度"
        if note:
            report["恢复备注"] = note
        return {"ok": True, "message": message, "window": self._snapshot(report)}

    # 语义别名：值班员「恢复待发布报告」与「重新进入窗口」都走同一条路径。
    resume_window = open_window
    restore_window = open_window

    def window_progress(self, site: str, template: str, window: str) -> dict[str, Any]:
        """只读查看窗口当前进度，不创建也不改变任何计数。"""
        site, template, window = self._text(site), self._text(template), self._text(window)
        report = self._find_window(site, template, window)
        if report is None:
            return {"ok": False, "message": f"窗口 {window} 尚未打开"}
        return {"ok": True, "message": "窗口进度已读取", "window": self._snapshot(report)}

    def list_windows(self) -> dict[str, Any]:
        """列出全部窗口报告摘要，供审计页恢复进度。"""
        windows = [
            self._snapshot(row) for row in store.rows(self.MODULE)
            if row.get("报告类型") == "窗口报告"
        ]
        return {"ok": True, "message": "窗口列表已读取", "windows": windows}

    def backfill_window(
        self,
        site: str,
        template: str,
        window: str,
        samples: list[dict[str, Any]],
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """按站点模板窗口补报迟到样本，只接受同窗口样本并按指纹幂等去重。"""
        site, template, window = self._text(site), self._text(template), self._text(window)
        if not window:
            return {"ok": False, "message": "样本窗口不能为空"}
        if not isinstance(samples, list) or not samples:
            return {"ok": False, "message": "补报样本不能为空"}
        _, error = self._validate_template(site, template)
        if error:
            return {"ok": False, "message": error}

        report = self._find_window(site, template, window)
        if report is None:
            report = self._new_window_row(site, template, window)

        accepted = 0
        duplicates = 0
        rejected: list[dict[str, Any]] = []
        existing = {
            self._text(sample.get("样本指纹"))
            for sample in self._window_samples(site, template, window)
        }
        for index, raw in enumerate(samples, start=1):
            if not isinstance(raw, dict):
                rejected.append({"index": index, "reason": "样本格式必须是对象"})
                continue
            normalized, reason = self._normalize_sample(
                raw, site=site, template=template, window=window
            )
            if normalized is None:
                rejected.append({"index": index, "reason": reason})
                continue
            fingerprint = normalized["fingerprint"]
            if fingerprint in existing:
                duplicates += 1
                continue
            existing.add(fingerprint)
            self._append_sample(normalized)
            accepted += 1

        self._recount(report)
        if accepted > 0:
            # 已经尝试过空发布或已有已发布版本时，新样本进入「补报等待确认」。
            if self._text(report.get("发布状态")) in (CONFIRMING, PUBLISHED, BACKFILL_PENDING):
                report["发布状态"] = BACKFILL_PENDING
                report["pending"] = True
            elif int(self._text(report.get("版本")) or 0) > 0:
                report["发布状态"] = BACKFILL_PENDING
                report["pending"] = True
        if note:
            report["补报备注"] = note

        message = (
            f"补报已受理：新增 {accepted} 条，重复 {duplicates} 条，"
            f"跨窗口/跨模板拒收 {len(rejected)} 条"
        )
        if accepted:
            message += f"；当前状态 {report.get('发布状态', BACKFILL_PENDING)}"
        elif duplicates:
            message += "；重复提交未重复计入分母"
        return {
            "ok": accepted > 0 or duplicates > 0,
            "message": message,
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "window": self._snapshot(report),
        }

    backfill_report = backfill_window
    report_late_samples = backfill_window

    def publish_window(
        self,
        site: str,
        template: str,
        window: str,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        """发布或重试发布窗口报告：每次追加回执，旧回执保留可查。"""
        site, template, window = self._text(site), self._text(template), self._text(window)
        if not window:
            return {"ok": False, "message": "样本窗口不能为空"}
        report = self._find_window(site, template, window)
        if report is None:
            return {"ok": False, "message": f"窗口 {window} 尚未打开，请先恢复窗口"}

        self._recount(report)
        total = int(self._text(report.get("样本总数")) or 0)
        hit = int(self._text(report.get("命中数")) or 0)
        version = int(self._text(report.get("版本")) or 0) + 1
        report["版本"] = str(version)
        report["更新时间"] = self._now()

        metric: dict[str, Any] | None = None
        metric_status = "暂无样本"
        if total == 0:
            # 空报告仍在确认：不计算命中率，不写伪 0%。
            rate_text = EMPTY_STATE
            report["字段命中率"] = EMPTY_STATE
            report["样本判定"] = EMPTY_STATE
            report["失败类别"] = EMPTY_STATE
            report["发布状态"] = CONFIRMING
            report["pending"] = True
            report["abnormal"] = False
            report["status"] = "正常"
            caliber = "0/0：暂无样本，不计算命中率，不向指标写入 0%"
            published = False
            metric = self._consume_metrics(report, 0, 0, EMPTY_STATE)
        else:
            rate = hit / total
            rate_text = f"{rate:.2%}"
            report["字段命中率"] = rate_text
            report["发布状态"] = PUBLISHED
            report["pending"] = rate < 0.9
            report["abnormal"] = rate < 0.6
            report["status"] = (
                "正常" if rate >= 0.9 else ("命中率下降" if rate >= 0.6 else "解析异常")
            )
            caliber = f"{hit}/{total}：分子为命中样本数，分母为窗口内去重样本总数"
            published = True
            metric = self._consume_metrics(report, hit, total, rate_text)
            metric_status = (metric or {}).get("status", "正常")

        receipt = {
            "回执号": f"{site}-{template}-{window}-v{version}",
            "版本": version,
            "站点名称": site,
            "解析模板": template,
            "样本窗口": window,
            "分母": total,
            "分子": hit,
            "命中率": rate_text,
            "样本判定": report.get("样本判定", EMPTY_STATE),
            "失败类别": report.get("失败类别", EMPTY_STATE),
            "发布状态": report.get("发布状态", CONFIRMING),
            "口径": caliber,
            "指标记录id": (metric or {}).get("metrics_id"),
            "指标状态": (metric or {}).get("status", metric_status),
            "指标回执": metric,
            "发布时间": self._now(),
            "备注": note,
        }
        history = report.setdefault("回执历史", [])
        history.append(receipt)
        report["当前回执"] = receipt

        message = (
            f"窗口 {window} 已发布第 {version} 版："
            + (f"分母 {total}、分子 {hit}、命中率 {rate_text}" if total else "暂无样本，仍待确认")
        )
        return {
            "ok": True,
            "published": published,
            "message": message,
            "version": version,
            "receipt": receipt,
            "window": self._snapshot(report),
        }

    retry_publish = publish_window
    republish = publish_window

    def _consume_metrics(
        self, report: dict[str, Any], numerator: int, denominator: int, rate_text: str
    ) -> dict[str, Any]:
        """把发布口径交采集量指标消费，失败类别与指标状态一起落库。"""
        from app.services.collect_metrics import CollectMetricsService

        site = self._text(report.get("站点名称"))
        template = self._text(report.get("解析模板"))
        window = self._text(report.get("样本窗口"))
        receipt_id = f"{site}-{template}-{window}-v{report.get('版本')}"
        metrics = CollectMetricsService().consume_audit_report(
            site=site,
            template=template,
            window=window,
            report_id=int(report.get("id", 0)),
            numerator=numerator,
            denominator=denominator,
            rate_text=rate_text,
            failure_category=self._text(report.get("失败类别")) or EMPTY_STATE,
            receipt_id=receipt_id,
        )
        return metrics

    def window_receipt(
        self, site: str, template: str, window: str, version: int | None = None
    ) -> dict[str, Any]:
        """按原窗口与版本读取发布回执，供指标页核对口径。"""
        report = self._find_window(site, template, window)
        if report is None:
            return {"ok": False, "message": f"窗口 {window} 尚未打开"}
        history: list[dict[str, Any]] = list(report.get("回执历史", []))
        if version is not None:
            history = [item for item in history if int(item.get("版本", 0)) == version]
        if not history:
            return {"ok": False, "message": "该窗口还没有发布回执"}
        return {
            "ok": True,
            "message": "发布回执已读取",
            "receipts": history,
            "current": report.get("当前回执"),
            "window": self._snapshot(report),
        }

    def verify_window(self, site: str, template: str, window: str) -> dict[str, Any]:
        """用审计回执核对指标页采用的口径，确认分子分母与失败类别一致。"""
        site, template, window = self._text(site), self._text(template), self._text(window)
        report = self._find_window(site, template, window)
        if report is None:
            return {"ok": False, "message": f"窗口 {window} 尚未打开"}
        receipt = report.get("当前回执")
        if not receipt:
            return {"ok": False, "message": "该窗口尚未发布，没有可核对的回执"}
        metrics_row = self._find_metrics_row(site, template, window)
        differences: list[str] = []
        if metrics_row is None:
            differences.append("指标页缺少该窗口记录")
        else:
            if int(self._text(metrics_row.get("分母")) or 0) != int(receipt.get("分母", 0)):
                differences.append(
                    f"分母不一致：回执 {receipt.get('分母')} / 指标 {metrics_row.get('分母')}"
                )
            if int(self._text(metrics_row.get("分子")) or 0) != int(receipt.get("分子", 0)):
                differences.append(
                    f"分子不一致：回执 {receipt.get('分子')} / 指标 {metrics_row.get('分子')}"
                )
            if self._text(metrics_row.get("命中率")) != self._text(receipt.get("命中率")):
                differences.append(
                    f"命中率不一致：回执 {receipt.get('命中率')} / 指标 {metrics_row.get('命中率')}"
                )
            if self._text(metrics_row.get("样本窗口")) != window:
                differences.append("指标记录的窗口与回执窗口不一致")
            if self._text(metrics_row.get("失败类别")) != self._text(receipt.get("失败类别")):
                differences.append("失败类别与指标记录不一致")
            if self._text(metrics_row.get("审计回执号")) != self._text(receipt.get("回执号")):
                differences.append("指标记录的审计回执号不是当前回执")
        return {
            "ok": True,
            "consistent": not differences,
            "message": "口径一致" if not differences else "口径存在差异",
            "differences": differences,
            "receipt": receipt,
            "metrics": metrics_row,
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记审计明细：带站点与样本窗口时登记为窗口报告，避免空样本落成 0%。"""
        site = self._text(values.get("站点名称"))
        window = self._text(values.get("样本窗口"))
        if site and window:
            template = self._text(values.get("解析模板"))
            _, error = self._validate_template(site, template)
            if error:
                return None, [error]
            report = self._find_window(site, template, window)
            if report is None:
                report = self._new_window_row(site, template, window)
            self._recount(report)
            return report, []
        return super().create_entry(values)
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """窗口报告的详情/忽略不能改写计算出的命中率与状态，只做回执与确认标记。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is not None and entry.get("报告类型") == "窗口报告":
            if action == "查看详情":
                self._recount(entry)
                return entry, f"窗口 {entry.get('样本窗口')} 进度已刷新，当前判定 {entry.get('样本判定')}"
            if action == "忽略告警":
                if self._text(entry.get("样本判定")) == EMPTY_STATE:
                    return None, "暂无样本的窗口没有告警可忽略，请先补报或继续等待"
                entry["告警已忽略"] = True
                entry["pending"] = False
                entry["abnormal"] = False
                return entry, f"窗口 {entry.get('样本窗口')} 告警已忽略，命中率口径保持不变"
            return None, f"动作「{action}」不适用于窗口报告"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        windows = [row for row in store.rows(self.MODULE) if row.get("报告类型") == "窗口报告"]
        snapshot["windows"] = len(windows)
        snapshot["empty_windows"] = sum(
            1 for row in windows if self._text(row.get("样本判定")) == EMPTY_STATE
        )
        snapshot["all_miss_windows"] = sum(
            1 for row in windows if self._text(row.get("样本判定")) == ALL_MISS_STATE
        )
        snapshot["backfill_pending"] = sum(
            1 for row in windows if self._text(row.get("发布状态")) == BACKFILL_PENDING
        )
        snapshot["published"] = sum(
            1 for row in windows if self._text(row.get("发布状态")) == PUBLISHED
        )
        return snapshot

    # ---- 兼容既有审计入口 ----

    def record_hit_rate(
        self,
        template: str,
        total: int,
        hit: int,
        *,
        site: str = "",
        window: str = "",
        failure_reason: str = "",
    ) -> dict[str, Any]:
        """记录一次模板命中率审计明细；空样本不再报错，也不伪造 0%。"""
        if total < 0 or hit < 0:
            return {"ok": False, "message": "样本总数与命中数不能为负数"}
        if total > 0 and hit > total:
            return {"ok": False, "message": "命中数不能大于样本总数"}
        if total == 0 and hit > 0:
            return {"ok": False, "message": "暂无样本时命中数必须为 0"}

        rows = store.rows(self.MODULE)
        aid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rate_text = EMPTY_STATE if total == 0 else f"{hit / total:.2%}"
        if total == 0:
            status = "正常"
            pending, abnormal = True, False
            distribution = ""
            state = EMPTY_STATE
        else:
            rate = hit / total
            status = "正常" if rate >= 0.9 else ("命中率下降" if rate >= 0.6 else "解析异常")
            pending = status != "正常"
            abnormal = status == "解析异常"
            distribution = ""
            if hit < total:
                distribution = failure_reason or "选择器失配/字段缺失"
            state = ALL_MISS_STATE if hit == 0 else (ALL_HIT_STATE if hit == total else PARTIAL_STATE)
        row = {
            "id": aid,
            "审计时间": self._now(),
            "解析模板": template,
            "样本总数": str(total),
            "字段命中率": rate_text,
            "失败原因分布": distribution,
            "status": status,
            "pending": pending,
            "abnormal": abnormal,
            "命中数": str(hit),
            "未命中数": str(total - hit),
            "样本判定": state,
            "失败类别": (
                EMPTY_STATE if total == 0 else ("解析全未命中" if hit == 0 else "")
            ),
        }
        if site:
            row["站点名称"] = site
        if window:
            row["样本窗口"] = window
        rows.append(row)
        return {
            "ok": True,
            "message": (
                "暂无样本，命中率待补报确认" if total == 0
                else "审计明细已记录"
            ),
            "audit_id": aid,
            "rate": None if total == 0 else rate_text,
            "hit_rate": rate_text,
            "state": state,
            "total": total,
            "hit": hit,
        }

    def publish_metrics(self, entry_id: int) -> dict[str, Any]:
        """把审计结论发布到采集量指标；窗口报告走完整回执发布流程。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "审计明细不存在"}
        if entry.get("报告类型") == "窗口报告":
            return self.publish_window(
                self._text(entry.get("站点名称")),
                self._text(entry.get("解析模板")),
                self._text(entry.get("样本窗口")),
                note="publish_metrics",
            )

        # 兼容普通审计明细：按样本总数与字段命中率发布解析口径。
        total = int(self._text(entry.get("样本总数")) or 0)
        rate_text = self._text(entry.get("字段命中率"))
        if total <= 0 or rate_text == EMPTY_STATE:
            # 空样本没有分母，既不发布 0%，也不覆盖指标行。
            return {
                "ok": False,
                "message": "暂无样本，没有可发布的分母；不得向指标写入 0% 命中率",
                "state": EMPTY_STATE,
            }
        try:
            hit = round(float(rate_text.rstrip("%")) / 100 * total)
        except ValueError:
            return {"ok": False, "message": "字段命中率格式无效"}
        metrics = store.rows("collect_metrics")
        template = self._text(entry.get("解析模板"))
        row = next(
            (m for m in metrics if m.get("站点名称") == template and not m.get("样本窗口")),
            None,
        )
        if row is None:
            mid = max((int(m.get("id", 0)) for m in metrics), default=0) + 1
            row = {
                "id": mid,
                "统计时间": self._now(),
                "站点名称": template,
                "抓取量": str(total),
                "解析量": str(hit),
                "产出量": str(hit),
                "status": "正常",
                "pending": True,
                "abnormal": False,
            }
            metrics.append(row)
        row["抓取量"] = str(total)
        row["解析量"] = str(hit)
        row["解析口径"] = f"{hit}/{total}"
        row["命中率"] = rate_text
        row["统计时间"] = self._now()
        return {"ok": True, "message": "审计结论已发布指标", "metrics_id": row["id"]}

    def audit_health(self, days: int = 1) -> dict[str, Any]:
        """输出审计批次健康度统计。"""
        rows = store.rows(self.MODULE)
        abnormal = sum(1 for r in rows if r.get("status") == "解析异常")
        degraded = sum(1 for r in rows if r.get("status") == "命中率下降")
        empty = sum(
            1 for r in rows
            if r.get("报告类型") == "窗口报告" and r.get("样本判定") == EMPTY_STATE
        )
        return {
            "ok": True,
            "message": "审计健康度统计完成",
            "total": len(rows),
            "abnormal": abnormal,
            "degraded": degraded,
            "empty_windows": empty,
        }
