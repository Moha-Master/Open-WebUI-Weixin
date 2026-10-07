"""按模型 meta 推导本次请求的能力字段（``features`` / ``tool_ids`` / ``filter_ids`` / ``terminal_id``）。

为什么需要这一层：OWUI 后端把 ``info.meta`` 当**否决权**（默认放行），把
``features``/``tool_ids``/``filter_ids``/``terminal_id`` 当**请求意愿**，并且**不替调用方
把 meta 翻译成意愿** —— chat 路径对这些默认值零回落，唯一的翻译实现是服务端自己发起
生成时用的 ``utils/automations.py:_resolve_model_defaults``（定时任务/频道助手）。
网页端则在发送前由 ``Chat.svelte:1029-1115`` 完成同样的翻译。适配器属于"无前端调用方"，
所以必须自己翻译，否则模型管理员配的默认功能一个都不会生效。

分层：``decide``/``resolve`` 是纯函数（可单测、不碰网络），``fetch_and_resolve``
负责并发取材料后调用它 —— 线上路径与只读探测走同一个函数，避免"测的不是跑的"。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import CapabilityConfig
from .owui import OwuiClient, OwuiError

log = logging.getLogger(__name__)

# 模型 meta.capabilities / meta.builtinTools 的布尔判定默认值：
# 与后端 tools.py:533-540 一致 —— 没配置就视为放行
ALLOW = True

#: 可以出现在 meta.defaultFeatureIds 里的特性（DefaultFeatures.svelte:12-30 只有这三项）
FEATURE_KEYS = ("web_search", "image_generation", "code_interpreter")

#: 特性 → /api/config 的 features 里的管理员总闸键名
ADMIN_GATE = {
    "web_search": "enable_web_search",
    "image_generation": "enable_image_generation",
    "code_interpreter": "enable_code_interpreter",
    "memory": "enable_memories",
}


#: 特性中文名，仅用于 /status 展示
_FEATURE_LABELS = {
    "web_search": "联网搜索",
    "image_generation": "图像生成",
    "code_interpreter": "代码解释器",
    "memory": "记忆",
}

#: 能在服务端真正执行代码的解释器引擎。默认引擎 pyodide 是在**浏览器**里跑的
#: （tools/builtin.py:689-706 需要 ``__event_call__`` 回调），适配器虽然有自己的
#: socket、但无法替浏览器跑 Python，开了只会让模型拿到一句执行失败。
#: 官方自己的 headless 实现同样排除它（automations.py:184-185 的注释）。
SERVER_SIDE_ENGINES = frozenset({"jupyter"})


@dataclass(frozen=True)
class Ref:
    """一个带名字的引用（工具 / 终端）：请求体用 id，回显用名字。"""

    id: str
    name: str

    @property
    def label(self) -> str:
        # 名字缺失时宁可说"未命名"，也不把内部 id 甩给用户
        return self.name or "未命名"


@dataclass(frozen=True)
class RequestCaps:
    """一次生成请求要带的能力字段。"""

    tools: tuple[Ref, ...] = ()
    filter_ids: tuple[str, ...] = ()
    terminal: Ref | None = None
    features: dict[str, bool] = field(default_factory=dict)

    @property
    def tool_id_list(self) -> list[str]:
        return [t.id for t in self.tools]

    def summary(self) -> str:
        """一行人话摘要，供 /status 与 /model use 展示。只出现名字，不出现 id。"""
        on = [name for name, value in self.features.items() if value]
        parts = ["、".join(_FEATURE_LABELS.get(n, n) for n in on) or "无"]
        if self.tools:
            parts.append("工具：" + "、".join(t.label for t in self.tools))
        if self.terminal:
            parts.append(f"终端：{self.terminal.label}")
        if self.filter_ids:
            parts.append(f"筛选器 {len(self.filter_ids)} 个")
        return " · ".join(parts)

    def body_fields(self) -> dict[str, Any]:
        """转成请求体字段；空值一律省略（与网页端 ``length > 0 ? … : undefined`` 一致）。

        ``memory`` 为假时整个键省略而不是发 false：网页端就是"为真才追加"
        （Chat.svelte:3407-3409），后端判定式 ``'memory' in features and features['memory']``
        也把缺键与 false 同等对待。
        """
        features = dict(self.features)
        if not features.get("memory"):
            features.pop("memory", None)
        out: dict[str, Any] = {"features": features}
        if self.tools:
            out["tool_ids"] = self.tool_id_list
        if self.filter_ids:
            out["filter_ids"] = list(self.filter_ids)
        if self.terminal:
            out["terminal_id"] = self.terminal.id
        return out


def model_meta(model: dict[str, Any] | None) -> dict[str, Any]:
    info = (model or {}).get("info")
    info = info if isinstance(info, dict) else {}
    meta = info.get("meta")
    return meta if isinstance(meta, dict) else {}


def _capabilities(meta: dict[str, Any]) -> dict[str, Any]:
    caps = meta.get("capabilities")
    return caps if isinstance(caps, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _layered(meta: dict[str, Any], feature: str) -> bool:
    """模型侧的三道闸门：``defaultFeatureIds`` 含该项 → 模型能力未否决 → 内置工具类别未否决。

    管理员总闸与适配器上限在 ``resolve`` 里叠加。判定式对齐网页端
    ``Chat.svelte:1083-1107`` 与服务端 ``automations.py:191-195``。
    刻意不看 ``builtinTools`` 的网页端等价性：后端会用它强制否决
    （tools.py:538-540），我们提前判断只是避免白要一个必然被剥离的工具。
    """
    wanted = meta.get("defaultFeatureIds")
    if not isinstance(wanted, list) or feature not in wanted:
        return False
    if not bool(_capabilities(meta).get(feature, ALLOW)):
        return False
    builtin = meta.get("builtinTools")
    builtin = builtin if isinstance(builtin, dict) else {}
    return bool(builtin.get(feature, ALLOW))


def resolve(
    model: dict[str, Any] | None,
    *,
    cfg: CapabilityConfig,
    available_tools: dict[str, str],
    admin_features: dict[str, Any],
    terminals: dict[str, str],
    user_memory: bool | None,
    engine: str | None = None,
) -> RequestCaps:
    """推导能力字段。

    :param model: ``GET /api/models`` 里该模型的条目（含 ``info.meta``）
    :param cfg: 适配器侧配置（是否跟随模型默认 + 各特性的允许上限）
    :param available_tools: 该用户可用工具的 ``id → 名字``（``GET /api/v1/tools/``）
    :param admin_features: ``GET /api/config`` 的 ``features`` 段（管理员总闸）
    :param terminals: 该用户可访问终端连接的 ``id → 名字``（``GET /api/v1/terminals/``）
    :param user_memory: 用户个人设置里的记忆开关，``None`` 表示没设过
    :param engine: 代码解释器引擎（``/api/config`` 的 ``code.interpreter_engine``）
    """
    meta = model_meta(model)

    if not cfg.follow_model_defaults:
        # 完全无视模型 meta：配置里开着什么就发什么，工具全带
        tools = tuple(Ref(i, n) for i, n in sorted(available_tools.items())) if cfg.tools else ()
        return RequestCaps(tools=tools, features=dict(cfg.fixed_features()))

    tools = tuple(
        Ref(tid, available_tools[tid])
        for tid in (meta.get("toolIds") or ())
        if isinstance(tid, str) and tid in available_tools
    )
    filter_ids = tuple(f for f in (meta.get("defaultFilterIds") or ()) if isinstance(f, str))

    # 终端：模型默认终端必须"适配器允许 + 该模型 terminal 能力未否决 + 确实在可访问清单里"。
    # 最后一条等价于网页端 Chat.svelte:1110-1115 的 isTerminalAvailable —— /api/v1/terminals/
    # 服务端已按 enabled 与 access_grants 过滤（routers/terminals.py:87-103），列出来就是能用
    caps = _capabilities(meta)
    wanted_terminal = meta.get("terminalId")
    terminal: Ref | None = None
    if (
        cfg.allow_terminal
        and isinstance(wanted_terminal, str)
        and wanted_terminal in terminals
        and bool(caps.get("terminal", ALLOW))
    ):
        terminal = Ref(wanted_terminal, terminals[wanted_terminal])

    features: dict[str, bool] = {}
    for feature in FEATURE_KEYS:
        # cfg.<feature> 是适配器侧的允许上限：默认全开，但仍允许本机一票否决某项
        on = getattr(cfg, feature) and _layered(meta, feature)
        on = on and bool(admin_features.get(ADMIN_GATE[feature], True))
        if feature == "code_interpreter" and (engine or "") not in SERVER_SIDE_ENGINES:
            # 默认引擎 pyodide 由浏览器执行，适配器答不了 execute:python 回调；
            # 而且 middleware.py:2751-2763 只要该字段为真就会往 system prompt 里
            # 塞 pyodide 文件系统说明（这一条没有 capability 闸门），所以更要关。
            on = False
        # 网页端在有终端时强制关掉代码解释器（MessageInput.svelte:843-846）。
        # 后端两条管线彼此独立，不这么做就会同时注入 run_command 与注定失败的
        # execute_code —— 模型面对两个功能重叠的工具，其中一个必然报错。
        if feature == "code_interpreter" and terminal:
            on = False
        features[feature] = on

    # memory 不在 defaultFeatureIds 体系内：网页端取 用户设置 ?? 管理员总闸（Chat.svelte:3407）
    features["memory"] = bool(cfg.memory) and bool(
        user_memory if user_memory is not None else admin_features.get(ADMIN_GATE["memory"], False)
    )

    return RequestCaps(tools=tools, filter_ids=filter_ids, terminal=terminal, features=features)


async def fetch_and_resolve(
    owui: OwuiClient, jwt_token: str, model_id: str, cfg: CapabilityConfig
) -> RequestCaps:
    """并发取齐材料再翻译。

    五个接口都必须现取：缓存任何一个都可能让能力静默降格（管理员把某模型的默认功能改了、
    终端下线了、工具新装了一个，本来就该下一条消息就生效）。
    """
    models, tools, terminals, config, settings = await asyncio.gather(
        owui.list_models(jwt_token),
        owui.list_tools(jwt_token),
        owui.list_terminals(jwt_token),
        owui.get_app_config(jwt_token),
        owui.get_user_settings(jwt_token),
    )
    model = next((m for m in models if str(m.get("id") or "") == model_id), None)
    if model is None:
        log.warning("模型 %s 不在 /api/models 里，能力按「该模型无默认配置」处理", model_id)

    ui = _as_dict(settings.get("ui"))
    admin = _as_dict(config.get("features"))
    code_cfg = _as_dict(config.get("code"))
    engine = code_cfg.get("interpreter_engine")
    # authenticated=false 表示该工具需要 OAuth 而用户尚未授权：网页端会单独挑出来去浏览器
    # 跳授权（Chat.svelte:1041-1054），适配器做不到，带着也只会失败
    tool_names = {
        str(t.get("id")): str(t.get("name") or "")
        for t in tools
        if isinstance(t, dict) and t.get("id") and t.get("authenticated", True) is not False
    }
    caps = resolve(
        model,
        cfg=cfg,
        available_tools=tool_names,
        admin_features=admin,
        terminals={
            str(t.get("id")): str(t.get("name") or "")
            for t in terminals
            if isinstance(t, dict) and t.get("id")
        },
        user_memory=ui.get("memory") if isinstance(ui.get("memory"), bool) else None,
        engine=engine if isinstance(engine, str) else None,
    )
    log.info(
        "能力解析 model=%s engine=%s %s",
        model_id,
        engine,
        caps.summary(),
    )
    return caps


#: 能力探测里可恢复的故障类型：只有"接口/网络不通"值得当作可重试的失败。
#: 写错字段名之类的程序缺陷必须照常抛出 —— 否则会被静默降级成"这条消息没工具"，
#: 开发阶段就真把一处 KeyError 吞成了降级，很难查。
PROBE_ERRORS = (OwuiError, httpx.HTTPError)
