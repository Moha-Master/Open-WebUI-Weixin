"""能力翻译层测试：把模型 meta 翻译成 features / tool_ids / filter_ids / terminal_id。

用例里的 meta 与工具/终端名字取自真实实例（GET /api/models、/api/v1/tools/、
/api/v1/terminals/），不是编的。

运行: venv/bin/python tests/test_caps.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from open_webui_weixin.capabilities import Ref, RequestCaps, resolve
from open_webui_weixin.config import CapabilityConfig

FAILS: list[str] = []

# 真实实例上的管理员总闸（GET /api/config -> features）
ADMIN_ON = {
    "enable_web_search": True,
    "enable_image_generation": True,
    "enable_code_interpreter": True,
    "enable_memories": True,
}
# 工具/终端都带名字：请求体用 id，回显用名字
TOOLS = {
    "image_describer": "Image Describer",
    "image_fetcher": "Image Fetcher",
    "server:mcp:amap-mcp": "高德地图",
    "server:mcp:github-mcp": "GitHub",
}
TERM = "e9cee153-0f23-42c2-837b-0e35c20cfa54"
TERMINALS = {TERM: "Sandbox", "2626b7c1-2b98-44cc-a076-f3ade63d1818": "JIAHUI"}


def check(name: str, cond: bool, detail: Any = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'  ' + str(detail)[:240] if detail and not cond else ''}")
    if not cond:
        FAILS.append(name)


def deepseek_model(**override: Any) -> dict:
    """真实实例上 deepseek-flash 的条目（含默认终端）。"""
    meta: dict[str, Any] = {
        "builtinTools": {
            "time": True, "memory": True, "web_search": True,
            "image_generation": True, "code_interpreter": True, "chats": False,
        },
        "toolIds": ["image_fetcher", "server:mcp:github-mcp"],
        "defaultFeatureIds": ["web_search", "code_interpreter", "image_generation"],
        "terminalId": TERM,
        "capabilities": {
            "vision": True, "file_upload": True, "web_search": True, "image_generation": True,
            "code_interpreter": True, "builtin_tools": True, "terminal": True, "memory": True,
        },
    }
    meta.update(override)
    return {"id": "deepseek-flash", "name": "DeepSeek Flash", "info": {"meta": meta}}


_UNSET = object()


def run(model: dict | object | None = _UNSET, cfg: CapabilityConfig | None = None, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "cfg": cfg or CapabilityConfig(),
        "available_tools": dict(TOOLS),
        "admin_features": dict(ADMIN_ON),
        "terminals": dict(TERMINALS),
        "user_memory": None,
        "engine": "jupyter",  # 服务端可执行的引擎，让 code_interpreter 的其它闸门先通过
    }
    args.update(kw)
    resolved_model = deepseek_model() if model is _UNSET else model
    return resolve(resolved_model, **args)  # type: ignore[arg-type]


def test_tool_ids_intersection() -> None:
    print("\n[1] 工具只取模型默认 ∩ 用户实际可用")
    caps = run()
    check("只带模型选的 2 个", caps.tool_id_list == ["image_fetcher", "server:mcp:github-mcp"], caps.tools)
    check("不被全带（amap 未被该模型选中）", "server:mcp:amap-mcp" not in caps.tool_id_list)
    check("带上名字供回显", [t.label for t in caps.tools] == ["Image Fetcher", "GitHub"], caps.tools)

    caps = run(deepseek_model(toolIds=["ghost_tool", "image_fetcher"]))
    check("过滤掉不存在的工具", caps.tool_id_list == ["image_fetcher"], caps.tools)

    caps = run(deepseek_model(toolIds=None))
    check("模型没配 toolIds 则为空（不自作主张全带）", caps.tools == (), caps.tools)
    check("空则请求体省略该键", "tool_ids" not in caps.body_fields(), caps.body_fields())

    caps = run(deepseek_model(toolIds=["image_fetcher"]))
    check("缺名字时不拿 id 顶替", Ref("image_fetcher", "").label == "未命名")
    check("名字保留原样", caps.tools[0].label == "Image Fetcher", caps.tools[0])


def test_features_layers() -> None:
    print("\n[2] features 的多层闸门")
    caps = run()
    check(
        "挂终端时三项按默认、代码解释器被互斥",
        caps.features == {
            "web_search": True, "image_generation": True, "code_interpreter": False, "memory": True,
        },
        caps.features,
    )
    caps = run(deepseek_model(terminalId=None))
    check("无终端且引擎可服务端执行时代码解释器开", caps.features["code_interpreter"] is True, caps.features)

    caps = run(deepseek_model(defaultFeatureIds=["web_search"]))
    check("不在 defaultFeatureIds 里就关", caps.features["image_generation"] is False, caps.features)

    caps = run(deepseek_model(capabilities={"image_generation": False}))
    check("模型 capability 否决", caps.features["image_generation"] is False, caps.features)

    caps = run(deepseek_model(builtinTools={"image_generation": False}))
    check("builtinTools 否决", caps.features["image_generation"] is False, caps.features)

    caps = run(admin_features={**ADMIN_ON, "enable_web_search": False})
    check("管理员总闸否决", caps.features["web_search"] is False, caps.features)

    caps = run(deepseek_model(capabilities=None, builtinTools=None))
    check("未配置=放行（与后端默认一致）", caps.features["web_search"] is True, caps.features)


def test_memory_follows_user_setting() -> None:
    print("\n[3] memory 取 用户设置 ?? 管理员总闸（Chat.svelte:3407）")
    check("用户显式 true", run(user_memory=True).features["memory"] is True)
    check("用户显式 false 可压过管理员", run(user_memory=False).features["memory"] is False)
    check("未设置则回落管理员总闸 true", run(user_memory=None).features["memory"] is True)
    caps = run(user_memory=None, admin_features={**ADMIN_ON, "enable_memories": False})
    check("管理员关掉则 false", caps.features["memory"] is False, caps.features)


def test_terminal_rules() -> None:
    print("\n[4] 终端的可用性与互斥")
    caps = run()
    check("默认终端可用则带上", caps.terminal is not None and caps.terminal.id == TERM, caps.terminal)
    check("带的是名字不是 id", caps.terminal is not None and caps.terminal.label == "Sandbox", caps.terminal)
    check("不在可访问清单里则丢", run(terminals={"other": "别的"}).terminal is None)
    rejected = run(deepseek_model(capabilities={"terminal": False}))
    check("模型 terminal 能力否决则丢", rejected.terminal is None, rejected.terminal)
    caps = run(deepseek_model(capabilities=None, builtinTools=None))
    check("capabilities 缺省时 terminal 默认放行", caps.terminal is not None and caps.terminal.id == TERM)
    check("带终端 ⇒ 强制关 code_interpreter（复刻网页端互斥）", run().features["code_interpreter"] is False)
    check("省略时字段不出现在请求体", "terminal_id" not in run(terminals={}).body_fields())


def test_filter_ids() -> None:
    print("\n[5] filter_ids 只取 defaultFilterIds（模型挂载的 filterIds 后端自己套）")
    caps = run(deepseek_model(defaultFilterIds=["vision_bridge_filter"], filterIds=["other_filter"]))
    check("带 defaultFilterIds", caps.filter_ids == ("vision_bridge_filter",), caps.filter_ids)
    check("不重复后端已处理的 filterIds", "other_filter" not in caps.filter_ids)


def test_fixed_mode() -> None:
    print("\n[6] follow_model_defaults=false 时无视 meta，完全按配置发")
    caps = run(cfg=CapabilityConfig(follow_model_defaults=False, image_generation=False))
    check("工具退回全带", len(caps.tools) == 4, caps.tools)
    check("全带时也带上名字", [t.label for t in caps.tools] == [
        "Image Describer", "Image Fetcher", "高德地图", "GitHub",
    ], [t.label for t in caps.tools])
    check(
        "features 用配置值",
        caps.features["image_generation"] is False and caps.features["memory"] is True,
        caps.features,
    )
    check("固定模式不挂终端", caps.terminal is None)
    caps = run(cfg=CapabilityConfig(follow_model_defaults=False, tools=False))
    check("tools=false 时不带工具", caps.tools == (), caps.tools)


def test_cfg_is_ceiling() -> None:
    print("\n[6b] 跟随模式下，配置是允许上限而非生效值")
    caps = run(cfg=CapabilityConfig(web_search=False))
    check("被上限否决", caps.features["web_search"] is False, caps.features)
    check("其余照模型默认", caps.features["image_generation"] is True, caps.features)

    caps = run(cfg=CapabilityConfig(memory=False))
    check("memory 上限", caps.features["memory"] is False, caps.features)
    check("memory 为假时省略该键", "memory" not in caps.body_fields()["features"], caps.body_fields())

    caps = run(cfg=CapabilityConfig(allow_terminal=False))
    check(
        "摘掉终端后 code_interpreter 才可能生效",
        caps.terminal is None and caps.features["code_interpreter"] is True,
        caps,
    )


def test_engine_gate() -> None:
    print("\n[8] 代码解释器只在服务端引擎下开启")
    no_term = deepseek_model(terminalId=None)
    check("jupyter 引擎可开", run(no_term, engine="jupyter").features["code_interpreter"] is True)
    check(
        "pyodide（默认，要浏览器执行）不开",
        run(no_term, engine="pyodide").features["code_interpreter"] is False,
    )
    check("引擎未知/读不到也不开", run(no_term, engine=None).features["code_interpreter"] is False)
    caps = run(no_term, cfg=CapabilityConfig(follow_model_defaults=False))
    check("固定模式下由配置决定（不做引擎判断）", caps.features["code_interpreter"] is True, caps.features)


def test_missing_model_meta() -> None:
    print("\n[7] 拿不到模型条目时不得凭空开能力")
    caps = run(None)
    check(
        "三项默认功能全关",
        caps.features["web_search"] is False
        and caps.features["image_generation"] is False
        and caps.features["code_interpreter"] is False,
        caps.features,
    )
    check("memory 仍按 用户设置??管理员总闸", caps.features["memory"] is True, caps.features)
    check("无工具无终端", caps.tools == () and caps.terminal is None, caps)
    caps = run({"id": "x"})
    check("无 info/meta 也不炸", caps.features["web_search"] is False)


def test_display_strings() -> None:
    print("\n[9] 回显文案与请求体形状")
    caps = run()
    summary = caps.summary()
    check("列出能力名", "联网搜索" in summary and "图像生成" in summary, summary)
    check("列出工具名字", "Image Fetcher" in summary and "GitHub" in summary, summary)
    check("列出终端名字", "Sandbox" in summary, summary)
    check("一个 id 都不出现", TERM not in summary and "image_fetcher" not in summary, summary)

    body = RequestCaps(features={"web_search": True, "memory": False}).body_fields()
    check("memory 为假时省略该键", "memory" not in body["features"], body)
    empty = RequestCaps(features={"web_search": False}).body_fields()
    check("空列表字段省略", "tool_ids" not in empty and "terminal_id" not in empty, empty)
    ids_only = RequestCaps(
        tools=(Ref("server:mcp:amap-mcp", "高德地图"),), terminal=Ref(TERM, "Sandbox")
    ).body_fields()
    check("请求体里仍是 id", ids_only["tool_ids"] == ["server:mcp:amap-mcp"], ids_only)
    check("terminal_id 是 id 不是名字", ids_only["terminal_id"] == TERM, ids_only)


def main() -> None:
    test_tool_ids_intersection()
    test_features_layers()
    test_memory_follows_user_setting()
    test_terminal_rules()
    test_filter_ids()
    test_fixed_mode()
    test_cfg_is_ceiling()
    test_engine_gate()
    test_missing_model_meta()
    test_display_strings()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        sys.exit(1)
    print("能力翻译全部通过")


if __name__ == "__main__":
    main()
