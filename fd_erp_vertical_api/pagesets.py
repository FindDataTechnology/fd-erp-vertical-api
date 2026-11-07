"""页面集授权（GET /erp/supply-chain/v1/pagesets 数据源 + 端点闸）。

与 paas 客户端精确对齐（paas/miniapp/src/pagesets/authorize.ts parsePagesetAuth）：
- 线格式：{"pagesets": [{"key": "supply-chain.ue", "grants": ["catalog.read", ...]}, ...]}
- 客户端严格解析：key 必须在其 KNOWN_KEYS 白名单（supply-chain.ue/se/sp/cp）、
  grants 必须全为字符串，任一元素形态非法 → 整个 payload 作废（fail-closed）。
  因此本端点只可能输出白名单内的 key 与封闭集内的 grant 码 —— 绝不输出会让
  客户端整体作废的形态。

授权配置源（本包简单配置面，fail-closed）：
- Settings.pageset_auth_json（内联 JSON）优先；
- 否则 Settings.pageset_auth_file（JSON 文件，形如 {"supply-chain.ue": ["catalog.read", ...]}）；
- 缺失/不可解析/形态非法 → 空授权（无页面集可见，所有 /ue/* 端点 403）。
fail-closed 双向：服务端配置缺失 → 不可见；客户端解析失败 → 不渲染（paas 侧）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import ApiError

#: 页面集 key 白名单（与 paas/miniapp/src/pagesets/authorize.ts KNOWN_KEYS 一字不差；
#: 输出白名单外的 key 会让客户端整体 fail-closed 作废 —— 绝不输出）
KNOWN_PAGESET_KEYS: tuple[str, ...] = (
    "supply-chain.ue",
    "supply-chain.se",
    "supply-chain.sp",
    "supply-chain.cp",
)

#: grant 码封闭集（paas/miniapp/src/pagesets/registry.ts 四页面集 requiredGrants
#: 与页级 requiredGrants 的并集；配置中出现集外码一律丢弃 —— 封闭集纪律）
KNOWN_GRANTS: frozenset[str] = frozenset({
    # UE 订货台（supply-chain.ue）
    "catalog.read", "order.write", "statement.confirm",
    "invoice.write", "aftersale.write", "ai.chat",
    # SE 业务员（supply-chain.se）
    "crm.read", "crm.visit.write", "order.write.proxy",
    "alert.handle", "statement.confirm.proxy",
    # SP 供应商（supply-chain.sp）
    "po.read", "po.accept", "delivery.feedback",
    "statement.read", "invoice.upload",
    # CP 承运商（supply-chain.cp）
    "waybill.read", "waybill.accept", "dock.reserve",
    "pickup.checkin", "pod.report", "freight.statement.read",
})


@dataclass(frozen=True)
class PagesetAuth:
    """租户页面集授权：key → 已授权 grant 码集（只含白名单 key 与封闭集 grant）。"""

    grants_by_key: dict[str, frozenset[str]]

    def grants_of(self, pageset_key: str) -> frozenset[str] | None:
        """未授权 key 返回 None（不可见）；授权返回 grant 集（可为空集）。"""
        return self.grants_by_key.get(pageset_key)

    def wire_payload(self) -> dict[str, Any]:
        """GET /pagesets 响应 data：与 paas parsePagesetAuth 逐字段对齐。"""
        return {"pagesets": [
            {"key": key, "grants": sorted(self.grants_by_key[key])}
            for key in KNOWN_PAGESET_KEYS if key in self.grants_by_key
        ]}


def _normalize(raw: Any) -> PagesetAuth:
    """配置 → PagesetAuth：非白名单 key、封闭集外 grant、非字符串一律丢弃。"""
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw, dict):
        return PagesetAuth({})
    for key, grants in raw.items():
        if key not in KNOWN_PAGESET_KEYS or not isinstance(grants, list):
            continue
        cleaned = sorted({g for g in grants if isinstance(g, str) and g in KNOWN_GRANTS})
        out[key] = frozenset(cleaned)
    return PagesetAuth(out)


def load_pageset_auth(*, inline_json: str | None = None,
                      file_path: str | None = None) -> PagesetAuth:
    """加载页面集授权配置（fail-closed：任何缺失/损坏 → 空授权，绝不猜默认）。"""
    payload: Any = None
    if inline_json:
        try:
            payload = json.loads(inline_json)
        except json.JSONDecodeError:
            return PagesetAuth({})
    elif file_path:
        try:
            with open(file_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return PagesetAuth({})
    return _normalize(payload)


def require_grant(auth: PagesetAuth, pageset_key: str, grant: str) -> frozenset[str]:
    """端点闸：页面集未授权 → PAGESET_FORBIDDEN；已授权缺 grant → GRANT_REQUIRED。

    返回该页面集授权 grant 集（调用方可继续做页级细粒度判断）。
    """
    grants = auth.grants_of(pageset_key)
    if grants is None:
        raise ApiError("PAGESET_FORBIDDEN",
                       f"当前租户未授权页面集：{pageset_key}",
                       detail={"pageset_key": pageset_key})
    if grant not in grants:
        raise ApiError("GRANT_REQUIRED",
                       f"当前账号缺少所需权限码：{grant}",
                       detail={"pageset_key": pageset_key, "grant": grant})
    return grants
