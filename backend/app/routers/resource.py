"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    EntryPayload,
    PageResult,
    WindowActionPayload,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    allowed = set(service.filter_fields()) if hasattr(service, "filter_fields") else set(service.LIST_FIELDS)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in allowed and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    allowed = set(service.filter_fields()) if hasattr(service, "filter_fields") else set(service.LIST_FIELDS)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in allowed and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


# ---- 解析质量审计：站点模板样本窗口工作流 ----
# 固定路径必须注册在 /{module}/{entry_id} 之前，否则 "windows" 会被当成 entry_id。

def audit_window_service():
    service = service_for("parse_audit")
    if service is None or not hasattr(service, "open_window"):
        raise HTTPException(status_code=404, detail="解析质量审计服务不可用")
    return service


def _window_params(
    site: str, template: str, window: str, *aliases: str
) -> tuple[str, str, str]:
    # 兼容中文字段名查询：site/解析模板/样本窗口 两种写法取其一。
    site = site.strip() or (aliases[0].strip() if len(aliases) > 0 else "")
    template = template.strip() or (aliases[1].strip() if len(aliases) > 1 else "")
    window = window.strip() or (aliases[2].strip() if len(aliases) > 2 else "")
    if not site:
        raise HTTPException(status_code=422, detail="站点名称不能为空")
    if not template:
        raise HTTPException(status_code=422, detail="解析模板不能为空")
    if not window:
        raise HTTPException(status_code=422, detail="样本窗口不能为空")
    return site, template, window


@router.get("/parse_audit/windows", response_model=dict)
def list_audit_windows() -> dict[str, Any]:
    """列出全部窗口报告，恢复待发布报告时先看这里。"""
    return audit_window_service().list_windows()


@router.get("/parse_audit/windows/progress", response_model=dict)
def audit_window_progress(
    site: str = Query(default=""),
    template: str = Query(default=""),
    window: str = Query(default=""),
    _site: str = Query(default="", alias="站点名称"),
    _template: str = Query(default="", alias="解析模板"),
    _window: str = Query(default="", alias="样本窗口"),
) -> dict[str, Any]:
    """只读恢复同一窗口的原进度，不会改变计数。"""
    s, t, w = _window_params(site, template, window, _site, _template, _window)
    return audit_window_service().window_progress(s, t, w)


@router.post("/parse_audit/windows/open", response_model=dict)
def open_audit_window(payload: WindowActionPayload) -> dict[str, Any]:
    """打开或恢复站点模板的样本窗口。"""
    s, t, w = _window_params(
        payload.resolved_site(), payload.resolved_template(), payload.resolved_window()
    )
    return audit_window_service().open_window(
        s, t, w, note=(payload.note or "").strip()
    )


@router.post("/parse_audit/windows/backfill", response_model=dict)
def backfill_audit_window(payload: WindowActionPayload) -> dict[str, Any]:
    """向所属站点模板窗口补报迟到样本，跨窗口样本会被拒收。"""
    s, t, w = _window_params(
        payload.resolved_site(), payload.resolved_template(), payload.resolved_window()
    )
    return audit_window_service().backfill_window(
        s, t, w, payload.samples, note=(payload.note or "").strip()
    )


@router.post("/parse_audit/windows/publish", response_model=dict)
def publish_audit_window(payload: WindowActionPayload) -> dict[str, Any]:
    """发布或重试发布窗口报告，每次追加回执。"""
    s, t, w = _window_params(
        payload.resolved_site(), payload.resolved_template(), payload.resolved_window()
    )
    return audit_window_service().publish_window(
        s, t, w, note=(payload.note or "").strip()
    )


@router.post("/parse_audit/windows/retry-publish", response_model=dict)
def retry_publish_audit_window(payload: WindowActionPayload) -> dict[str, Any]:
    """重试原发布：与发布走同一流程，旧回执保留可查。"""
    s, t, w = _window_params(
        payload.resolved_site(), payload.resolved_template(), payload.resolved_window()
    )
    return audit_window_service().publish_window(
        s, t, w, note=(payload.note or "").strip() or "retry"
    )


@router.get("/parse_audit/windows/receipt", response_model=dict)
def read_audit_receipt(
    site: str = Query(default=""),
    template: str = Query(default=""),
    window: str = Query(default=""),
    _site: str = Query(default="", alias="站点名称"),
    _template: str = Query(default="", alias="解析模板"),
    _window: str = Query(default="", alias="样本窗口"),
    version: int | None = Query(default=None),
) -> dict[str, Any]:
    """按原窗口（和版本）读取发布回执。"""
    s, t, w = _window_params(site, template, window, _site, _template, _window)
    return audit_window_service().window_receipt(s, t, w, version)


@router.get("/parse_audit/windows/verify", response_model=dict)
def verify_audit_caliber(
    site: str = Query(default=""),
    template: str = Query(default=""),
    window: str = Query(default=""),
    _site: str = Query(default="", alias="站点名称"),
    _template: str = Query(default="", alias="解析模板"),
    _window: str = Query(default="", alias="样本窗口"),
) -> dict[str, Any]:
    """用发布回执核对指标页采用的口径。"""
    s, t, w = _window_params(site, template, window, _site, _template, _window)
    return audit_window_service().verify_window(s, t, w)


# ---- 采集量指标：按窗口读取解析质量口径 ----


def metrics_service():
    service = service_for("collect_metrics")
    if service is None or not hasattr(service, "audit_report_view"):
        raise HTTPException(status_code=404, detail="采集量指标服务不可用")
    return service


@router.get("/collect_metrics/windows", response_model=dict)
def list_window_metrics() -> dict[str, Any]:
    """列出各窗口的指标口径，旁边时段独立成行。"""
    return metrics_service().list_window_metrics()


@router.get("/collect_metrics/windows/view", response_model=dict)
def view_window_metrics(
    site: str = Query(default=""),
    template: str = Query(default=""),
    window: str = Query(default=""),
    _site: str = Query(default="", alias="站点名称"),
    _template: str = Query(default="", alias="解析模板"),
    _window: str = Query(default="", alias="样本窗口"),
) -> dict[str, Any]:
    """按原窗口读取指标页的分子分母与失败类别。"""
    s, t, w = _window_params(site, template, window, _site, _template, _window)
    return metrics_service().audit_report_view(s, t, w)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


# 单条读取放在固定路径之后，避免 parse_audit/windows 这类字面路径被当成 entry_id。
@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry
