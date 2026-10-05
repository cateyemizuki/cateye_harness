"""cateye_harness（宿主自测试夹具）— 配置模型。

- 文件名刻意不叫 ``config.py``：宿主 Runner 加载插件时 sys.path 含宿主 ``src``，
  且 cateye_test 与本插件同进程加载，顶层 ``config`` 会被宿主 ``src/config`` 或
  兄弟插件抢占（L1 实证），故改名并由 plugin.py 顶部引导头接管自身模块解析。
- `[plugin]` 段是宿主硬性要求（缺 `config_version` 即加载失败，开发文档 §5.2）；
- `[harness]` 段为夹具参数，**全部带默认值**，默认值即 TEST_PROCEDURE §〇 已核实的
  测试环境事实（测试群 100000003 / 测试私聊 100000002 / 账号 100000001 /
  危险对端 100000004），测试 agent 不改配置即可用；
- 配置热重载：`on_config_update(scope="self")` 触发（无需重启），守卫窗口在重载时清空。
"""

from __future__ import annotations

from typing import ClassVar, Dict, List

from maibot_sdk import Field, PluginConfigBase

# 与 _manifest.json 的 version 保持同步。
SUPPORTED_CONFIG_VERSION = "0.1.1"


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（宿主硬性要求：必须有 config_version）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件（关闭后注入网关不再上报就绪，/h_* 命令不响应）",
        json_schema_extra={
            "label": "启用插件",
            **_ui_i18n("Enable plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "label": "配置版本",
            "hidden": True,
            "disabled": True,
            **_ui_i18n("Config version"),
        },
    )


class HarnessSectionConfig(PluginConfigBase):
    """测试夹具参数（默认值 = TEST_PROCEDURE §〇 环境事实）。"""

    __ui_label__ = "测试夹具"
    __ui_icon__ = "flask-conical"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Test harness",
            "description": "Synthetic message injection, milestone scenes, circuit breaker and loop guard.",
        }
    }

    # ---- 身份与测试流（§〇 环境事实）----
    self_id: int = Field(
        default=100000001,
        description="被测 bot 账号（snowluma 适配器登录号）；注入消息 additional_config.self_id 用它定位账号与私聊路由",
        json_schema_extra={"label": "被测账号 self_id", **_ui_i18n("Bot self_id")},
    )
    self_nickname: str = Field(
        default="麦麦",
        description="被测 bot 昵称（仅用于 is_at 注入的 at 段显示名，不做任何身份匹配）",
        json_schema_extra={"label": "被测 bot 昵称", **_ui_i18n("Bot nickname")},
    )
    sender_default: int = Field(
        default=100000002,
        description="注入消息的默认发送者 QQ（§一 主测试私聊本人）；也是 /h_* 命令的默认允许调用者",
        json_schema_extra={"label": "默认注入发送者", **_ui_i18n("Default inject sender")},
    )
    test_group_id: int = Field(
        default=100000003,
        description="主测试群（§一：可注入、可执行命令）；/h_inject 与 /h_say 群目标白名单",
        json_schema_extra={"label": "主测试群", **_ui_i18n("Primary test group")},
    )
    test_private_qq: int = Field(
        default=100000002,
        description="主测试私聊（§一：可注入、可执行命令）；/h_inject 与 /h_say 私聊目标白名单",
        json_schema_extra={"label": "主测试私聊", **_ui_i18n("Primary test private chat")},
    )
    extra_allowed_group_ids: List[int] = Field(
        default_factory=list,
        description="额外允许注入的群号（默认空；100000005 为只许旁观群，禁止任何主动激活）",
        json_schema_extra={"label": "额外允许群", **_ui_i18n("Extra allowed groups", "Empty by default; observe-only groups must never be added.")},
    )
    extra_allowed_private_qqs: List[int] = Field(
        default_factory=list,
        description="额外允许注入的私聊 QQ（默认空；100000004 为另一台 bot，默认完全不碰）",
        json_schema_extra={"label": "额外允许私聊", **_ui_i18n("Extra allowed private chats")},
    )
    blocked_senders: List[int] = Field(
        default_factory=lambda: [100000004],
        description="注入 sender 黑名单（§一 红线①：注入消息的 sender 永远不得是 100000004）",
        json_schema_extra={"label": "注入 sender 黑名单", **_ui_i18n("Blocked inject senders")},
    )
    command_allowed_users: List[int] = Field(
        default_factory=lambda: [100000002],
        description="/h_* 命令允许的调用者 QQ（本机控制台天然放行）；防止测试群成员误触熔断/直发",
        json_schema_extra={"label": "命令允许调用者", **_ui_i18n("Allowed command callers")},
    )

    # ---- 环路守卫（§一 红线③ / §四 守卫行）----
    guard_enabled: bool = Field(
        default=True,
        description="启用 chat.receive.after_process 环路守卫：10 分钟窗口内收到对端 bot 消息达阈值即自动熔断",
        json_schema_extra={"label": "环路守卫", **_ui_i18n("Loop guard")},
    )
    guard_peer_qq: int = Field(
        default=100000004,
        description="危险对端（另一台服务器上的 bot）；仅匹配该 QQ 的私聊入站消息",
        json_schema_extra={"label": "警戒对端 QQ", **_ui_i18n("Guarded peer QQ")},
    )
    guard_window_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="滑动窗口长度（分钟）；只保留时间戳，不存任何消息内容",
        json_schema_extra={"label": "守卫窗口（分钟）", **_ui_i18n("Guard window (minutes)")},
    )
    guard_round_trips: int = Field(
        default=2,
        ge=1,
        le=20,
        description="窗口内达到多少个对端入站“来回”即触发自动熔断（§一：≥2 个来回立即 /h_freeze）",
        json_schema_extra={"label": "熔断阈值（来回数）", **_ui_i18n("Freeze threshold (turns)")},
    )
    guard_cooldown_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="触发后冷却（分钟），防止重复熔断与日志刷屏",
        json_schema_extra={"label": "触发冷却（分钟）", **_ui_i18n("Trigger cooldown (minutes)")},
    )

    # ---- 熔断目标 ----
    freeze_target_plugin: str = Field(
        default="github.cateye.cateye-suite",
        description="熔断目标插件 id（被测插件）；/h_freeze 停用其 tool/hook 组件，/h_resume 恢复",
        json_schema_extra={"label": "熔断目标插件", **_ui_i18n("Freeze target plugin")},
    )
    freeze_component_types: List[str] = Field(
        default_factory=lambda: ["tool", "hook_handler"],
        description="熔断时停用的组件类型（大小写不敏感）；patrol/emotion 等 asyncio 后台循环不是组件，须改 plugin.enabled=false",
        json_schema_extra={"label": "熔断组件类型", **_ui_i18n("Freeze component types")},
    )

    # ---- 适配器就绪前置（§四 实现注意：route_message 前置条件是网关 ready）----
    adapter_plugin_id: str = Field(
        default="maibot-team.snowluma-adapter",
        description="真实网关所属适配器插件 id；注入前用 get_all_plugins 确认其已装载且含下方网关组件",
        json_schema_extra={"label": "适配器插件 id", **_ui_i18n("Adapter plugin id")},
    )
    adapter_gateway_name: str = Field(
        default="snowluma_gateway",
        description="真实网关组件名（宿主按 (调用方插件, 网关名) 解析网关，本夹具只能用自己的 receive 网关注入；此名仅用于就绪检查）",
        json_schema_extra={"label": "适配器网关名", **_ui_i18n("Adapter gateway name")},
    )
    verify_adapter_ready: bool = Field(
        default=True,
        description="注入前做真实链路探测（调用适配器 get_login_info）；失败即明确报错不注入",
        json_schema_extra={"label": "注入前探测适配器", **_ui_i18n("Probe adapter before inject")},
    )
    adapter_probe_api: str = Field(
        default="adapter.napcat.system.get_login_info",
        description="就绪探测 API（短名或 plugin_id.api_name 全名）；返回失败或账号不符即拒绝注入",
        json_schema_extra={"label": "就绪探测 API", **_ui_i18n("Readiness probe API")},
    )

    # ---- 命令参数 ----
    reload_timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="/h_wait_reload 默认超时（秒）",
        json_schema_extra={"label": "热重载等待超时（秒）", **_ui_i18n("Reload wait timeout (s)")},
    )
    reload_poll_seconds: int = Field(
        default=2,
        ge=1,
        le=30,
        description="/h_wait_reload 轮询间隔（秒）",
        json_schema_extra={"label": "热重载轮询间隔（秒）", **_ui_i18n("Reload poll interval (s)")},
    )
    state_tail_lines: int = Field(
        default=20,
        ge=1,
        le=500,
        description="/h_state 读取 decision_logs 尾部条数（默认 20）",
        json_schema_extra={"label": "状态日志尾部条数", **_ui_i18n("State log tail lines")},
    )


class CateyeHarnessConfig(PluginConfigBase):
    """测试夹具完整配置。

    注意：子分组必须扁平挂为顶层字段（嵌套段 WebUI 不渲染），
    对应 config.toml 的 `[plugin]` 与 `[harness]` 两段。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    harness: HarnessSectionConfig = Field(default_factory=HarnessSectionConfig)