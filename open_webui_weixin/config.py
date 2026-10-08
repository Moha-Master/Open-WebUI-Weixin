"""配置加载：config.yaml -> 带类型的 dataclass。"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# 配置模板随包分发：既是文档示例，也是首次运行时写出的默认配置（单一事实来源）
EXAMPLE_CONFIG_PATH = Path(__file__).with_name("config.yaml.example")

# 入口 --dir 缺省值：状态与配置脱离代码目录，重装/搬迁都不影响已绑定的账号
DEFAULT_WORK_DIR = Path("~/.config/open-webui-weixin")


def resolve_work_dir(value: str | Path | None = None) -> Path:
    """解析工作目录（与入口 ``--dir`` 同一套规则，探针脚本复用）。"""
    return Path(value).expanduser() if value else DEFAULT_WORK_DIR.expanduser()


@dataclasses.dataclass
class OwuiConfig:
    base_url: str = "http://127.0.0.1:8901"


@dataclasses.dataclass
class WeixinConfig:
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    bot_type: str = "3"
    channel_version: str = "2.4.9"
    bot_agent: str = "owux/0.1.0"
    long_poll_timeout_ms: int = 35000
    api_timeout_ms: int = 15000


@dataclasses.dataclass
class StateConfig:
    path: str = "data/state.db"


@dataclasses.dataclass
class LoggingConfig:
    level: str = "INFO"
    to_file: bool = True


@dataclasses.dataclass
class ReplyConfig:
    max_length: int = 1024
    segment_interval: float = 0.4


@dataclasses.dataclass
class ReasoningDisplay:
    """模型思考内容的呈现方式。"""

    enable: bool = False  # 关 = 完全不外泄
    detailed: bool = False  # 开 = 推送全文；关 = 每个思考块只提示一次「正在思考」


@dataclasses.dataclass
class ToolStatusDisplay:
    """工具调用与检索进度的呈现方式。"""

    enable: bool = True  # 关 = 正文之外一律不出声
    detailed: bool = False  # 开 = 每个工具一条（名称 + 入参 + 返回，均截断）
    # 关 = 每段正文开始前汇总一条「使用了 N 个工具：a、b×2」


@dataclasses.dataclass
class DisplayConfig:
    """事件呈现策略。按讨论结论，这些不做成运行时命令，改配置即可。"""

    reasoning: ReasoningDisplay = dataclasses.field(default_factory=ReasoningDisplay)
    tool_status: ToolStatusDisplay = dataclasses.field(default_factory=ToolStatusDisplay)
    citations: bool = True  # 回复末尾附引用来源
    typing: bool = True  # 生成期间显示微信原生「正在输入」


@dataclasses.dataclass
class CapabilityConfig:
    """能力开关的来源策略。

    OWUI 后端**不会**把模型自带的默认功能翻译成请求字段：``tool_ids`` 不传则一行 MCP
    代码都不执行（middleware.py:2974），``features`` 是裸 dict、缺键即关闭
    （middleware.py:2684）。后端只把 ``meta.capabilities`` / ``meta.builtinTools`` 当
    **否决权**（默认放行，tools.py:533-540）。所以"模型管理员配的默认功能"必须由调用方
    自己翻译 —— 网页端在 ``Chat.svelte:1029-1115`` 做，服务端自己的 headless 路径在
    ``utils/automations.py:166-199`` 做。

    ``follow_model_defaults=true`` 即复刻网页端的翻译；关掉时退回下面这几个显式开关。
    """

    follow_model_defaults: bool = True  # 跟随模型 meta（与网页端等价）
    allow_terminal: bool = True  # 允许挂模型默认终端（等于允许服务器命令执行）
    tools: bool = True  # 固定模式下：带上该用户全部可用工具
    web_search: bool = True
    image_generation: bool = True
    code_interpreter: bool = True
    memory: bool = True

    def fixed_features(self) -> dict[str, bool]:
        """固定模式下的 features。刻意不含 voice（服务端会产出语音，微信侧无落点）。"""
        return {
            "web_search": self.web_search,
            "image_generation": self.image_generation,
            "code_interpreter": self.code_interpreter,
            "memory": self.memory,
        }


@dataclasses.dataclass
class AppConfig:
    owui: OwuiConfig
    weixin: WeixinConfig
    state: StateConfig
    logging: LoggingConfig
    reply: ReplyConfig
    display: DisplayConfig
    capabilities: CapabilityConfig
    base_dir: Path

    @property
    def state_path(self) -> Path:
        p = Path(self.state.path)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def log_path(self) -> Path:
        return self.state_path.parent / "adapter.log"


def _apply(obj: Any, data: dict[str, Any], name: str) -> None:
    valid = {f.name for f in dataclasses.fields(obj)}
    for key, value in data.items():
        if key not in valid:
            log.warning("配置 %s 中存在未知字段 %r，已忽略", name, key)
            continue
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            # 段落值：旧配置里 reasoning/tool_status 是裸布尔（只表达开关），
            # 迁移成映射时保留该开关，detailed 走默认值
            if isinstance(value, bool):
                current.enable = value
            elif isinstance(value, dict):
                _apply(current, value, f"{name}.{key}")
            else:
                log.warning("配置 %s.%s 需要映射，收到 %r，已忽略", name, key, value)
            continue
        setattr(obj, key, value)


def load_config(path: Path) -> AppConfig:
    """读取 YAML 配置；缺失的段落与字段使用默认值。"""
    raw: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
        elif loaded is not None:
            raise ValueError(f"配置文件格式错误，顶层必须是映射: {path}")
    else:
        log.warning("配置文件不存在，使用默认配置: %s", path)

    cfg = AppConfig(
        owui=OwuiConfig(),
        weixin=WeixinConfig(),
        state=StateConfig(),
        logging=LoggingConfig(),
        reply=ReplyConfig(),
        display=DisplayConfig(),
        capabilities=CapabilityConfig(),
        # 落成绝对路径：入口会 chdir 到工作目录，但日志与相对路径拼接都该看到真实位置
        base_dir=path.parent.resolve(),
    )
    for field in dataclasses.fields(cfg):
        if field.name == "base_dir":
            continue
        section = raw.get(field.name)
        if isinstance(section, dict):
            _apply(getattr(cfg, field.name), section, field.name)

    cfg.owui.base_url = cfg.owui.base_url.rstrip("/")
    cfg.weixin.base_url = cfg.weixin.base_url.rstrip("/")
    cfg.weixin.cdn_base_url = cfg.weixin.cdn_base_url.rstrip("/")
    return cfg


def ensure_default_config(path: Path) -> bool:
    """配置文件不存在时，从包内的 config.yaml.example 复制一份，返回是否新建。"""
    if path.exists():
        return False
    if not EXAMPLE_CONFIG_PATH.exists():
        # 模板缺失说明安装不完整，宁可报错也不要凭空生成一份没有凭据说明的配置
        raise FileNotFoundError(f"未找到配置模板 {EXAMPLE_CONFIG_PATH}，请参考 README 手工创建 {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return True
