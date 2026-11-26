"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class WindowActionPayload(BaseModel):
    """解析质量审计窗口动作：站点、模板、样本窗口与补报样本一起提交。

    同时接受中文字段名，前端与脚本都按业务字段直接提交即可。
    """

    site: str = ""
    template: str = ""
    window: str = ""
    samples: list[dict[str, Any]] = Field(default_factory=list)
    version: int | None = None
    note: str | None = None
    站点名称: str = ""
    解析模板: str = ""
    样本窗口: str = ""

    def resolved_site(self) -> str:
        return (self.site or self.站点名称).strip()

    def resolved_template(self) -> str:
        return (self.template or self.解析模板).strip()

    def resolved_window(self) -> str:
        return (self.window or self.样本窗口).strip()
