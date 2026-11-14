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

#: **对外发布**的页集 key（与 paas/miniapp/src/pagesets/authorize.ts KNOWN_KEYS 一字不差；
#: GET /pagesets 的输出只会遍历这一层。多吐一个客户端不认识的 key，客户端会整体
#: fail-closed 作废——连带把四个业务页集一起打掉，所以这一层不许随手加。
PUBLISHED_PAGESET_KEYS: tuple[str, ...] = (
    "supply-chain.ue",
    "supply-chain.se",
    "supply-chain.sp",
    "supply-chain.cp",
)

#: **内部**页集 key：只用于服务端端点闸，**绝不进 /pagesets 载荷**。
#: 存在的理由：公司自己的财报属于财务后台（桌面/工具），不属于客户订货小程序；
#: 但它同样需要「页集授权 + grant + 身份头」三件齐备，而不是只凭租户头裸开。
INTERNAL_PAGESET_KEYS: tuple[str, ...] = (
    "supply-chain.fi",
)

#: 契约锚：与 paas 客户端 `KNOWN_KEYS` **一字不差**（`tests/test_authz_contract.py` 逐项对拍）。
#: 只含对外发布层——内部页集进这个元组会立刻让那条跨仓契约闸变红，那是对的：
#: 客户端不认识它，服务端就不该把它算进「客户端契约」。
KNOWN_PAGESET_KEYS: tuple[str, ...] = PUBLISHED_PAGESET_KEYS

#: 服务端实际接受的全部页集 key（对外 + 内部）。授权配置按这一层收敛。
ALL_PAGESET_KEYS: tuple[str, ...] = PUBLISHED_PAGESET_KEYS + INTERNAL_PAGESET_KEYS

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

#: **内部** grant 码（只服务内部页集，不进客户端契约）。
#: 与页集 key 同样分两层：`KNOWN_GRANTS` 是契约锚（等于 paas 四个页集 requiredGrants 并集），
#: 内部面的权限码放这里，`ALL_GRANTS` 才是服务端实际接受的集合。
INTERNAL_GRANTS: frozenset[str] = frozenset({
    # FI 财务后台（supply-chain.fi）：读三张法定报表与增值税申报口径
    "finance.read",
    # FI 财务后台：登记申报数、发票与过账（写）。读权限 MUST NOT 覆盖它。
    "finance.write",
    # FI 财务后台：确认/退回提案（拍板）。读与写都 MUST NOT 覆盖它——
    # 「能登记一张票」与「能让这笔分录成为事实」是两件事，后者是账本不可逆的一步。
    "finance.confirm",
})

#: 服务端实际接受的全部 grant 码（契约锚 + 内部）；授权配置按这一层收敛。
ALL_GRANTS: frozenset[str] = KNOWN_GRANTS | INTERNAL_GRANTS


@dataclass(frozen=True)
class PagesetAuth:
    """租户页面集授权：key → 已授权 grant 码集（只含白名单 key 与封闭集 grant）。"""

    grants_by_key: dict[str, frozenset[str]]

    def grants_of(self, pageset_key: str) -> frozenset[str] | None:
        """未授权 key 返回 None（不可见）；授权返回 grant 集（可为空集）。"""
        return self.grants_by_key.get(pageset_key)

    def wire_payload(self) -> dict[str, Any]:
        """GET /pagesets 响应 data：与 paas parsePagesetAuth 逐字段对齐。"""
        # 只遍历对外发布层：内部页集（supply-chain.fi）即使已授权也不能出现在这里，
        # 否则客户端解析遇到未知 key 会整包作废。
        return {"pagesets": [
            {"key": key, "grants": sorted(self.grants_by_key[key])}
            for key in PUBLISHED_PAGESET_KEYS if key in self.grants_by_key
        ]}


def _normalize(raw: Any) -> PagesetAuth:
    """配置 → PagesetAuth：非白名单 key、封闭集外 grant、非字符串一律丢弃。"""
    out: dict[str, frozenset[str]] = {}
    if not isinstance(raw, dict):
        return PagesetAuth({})
    for key, grants in raw.items():
        if key not in ALL_PAGESET_KEYS or not isinstance(grants, list):
            continue
        cleaned = sorted({g for g in grants if isinstance(g, str) and g in ALL_GRANTS})
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
