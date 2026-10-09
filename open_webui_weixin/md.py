"""微信侧 Markdown 文案小工具。

微信客户端对 iLink 文本条目（`item_list[].type=1`）原生渲染 Markdown，无需开关。
支持的子集（参照 openclaw-weixin 的 `StreamingMarkdownFilter`，它剥掉的就是我们不该用的）：

- 渲染：H1–H4 标题、表格、有序/无序列表、**加粗**、行内代码、代码块、引用块 `> `、分隔线
- 不渲染：包中文的斜体 `*…*` / `_…_`（会露出裸标记）、H5/H6、图片 `![alt](url)`

因此本项目的文案约定：分节用 `##`，名字用行内代码，补充说明用引用块，强调只加粗不倾斜。
"""

from __future__ import annotations

from typing import Any


def code_span(value: Any) -> str:
    """把显示名包成行内代码；本身含反引号时原样返回，免得把代码段撑破。"""
    text = str(value or "")
    return f"`{text}`" if text and "`" not in text else text
