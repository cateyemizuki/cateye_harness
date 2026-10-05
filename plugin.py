"""cateye_harness — MaiBot 宿主自测试夹具（TEST_PROCEDURE §四 全表实现）。

定位：宿主只接受“真实入站消息”驱动，本插件充当测试夹具，替测试 agent 完成
消息注入、场景步进、熔断、状态快照与探针。**自身不调 LLM、不主动 send.*
（唯一例外：显式 /h_say）**；环路守卫只存时间戳，不存任何消息内容。

组件清单（9 命令 + 1 网关 + 1 Hook + 1 后台文件任务通道）：
- ``/h_inject``      构造标准 MessageDict（开发文档 §10.2）经本插件 receive 网关注入（§10.4 route_message）
- ``/h_seq``         6 个里程碑场景：无参列出、有名 dry-run、``run`` 执行步进（复用注入核心）
- ``/h_freeze``      停用被测插件 tool/hook 组件（熔断，宿主日志 ERROR 显著标记）
- ``/h_resume``      恢复 /h_freeze 停用的组件
- ``/h_state``       进程内读取被测插件数据目录摘要 + decision_logs 尾部 N 条
- ``/h_wait_reload`` 轮询 get_all_plugins 直到被测插件 version/组件数符合预期
- ``/h_api``         ctx.api.call 调被测插件公开 API（兼容 dict/异常两种失败形态，§8.0）
- ``/h_models``      ctx.llm.get_available_models() 列任务名（只列不调）
- ``/h_say``         ctx.send.text 直发（目标仅限群 100000003 / 私聊 100000002）
- 网关 ``harness_inject_gateway``（receive，仅作注入载体）
- Hook ``harness_loop_guard``（chat.receive.after_process / OBSERVE，麦麦⇄100000004 环路守卫）
- 文件任务通道（v0.1.1）：后台 asyncio 循环每 2s 轮询**本插件自身数据目录** ``tasks/*.json``，
  按文件名排序逐个消费，分发到上面命令的同一批核心函数（inject/seq/state/freeze/resume），
  回执写 ``results/<id>.json``、任务归档 ``tasks/done/``——供测试 agent 用文件系统遥控宿主侧
  harness（宿主验证日主控链路）。动作白名单 + 参数纯数据（**绝不做动态代码执行/动态导入**）。

⚠ 注入网关的宿主语义（MaiBot 1.3.0 源码核实：``src/plugin_runtime/host/supervisor.py:1662``
与 ``:1673``）——``host.route_message`` 按 ``(调用方 plugin_id, gateway_name)`` 解析网关，
**普通插件无法借别人的网关注入**，因此 /h_inject 用的是本插件自己声明的 receive 网关
（名字 ``harness_inject_gateway``），而不是 snowluma 适配器的 ``snowluma_gateway``；
``snowluma_gateway`` 仅作为「真实链路就绪检查」的目标（get_all_plugins + get_login_info 探测）。
注入消息带 ``additional_config.self_id=100000001``，账号/平台路由与真实链路一致；
但**适配器黑白名单（adapter_policy.toml）不会作用于本夹具网关**，故夹具内置更严格的
目标白名单（默认仅 100000003 / 100000002），见 _resolve_target。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple

# ── 宿主运行时导入引导（勿删）──────────────────────────────────────────
# 宿主 Runner 用 spec_from_file_location 加载本文件，只把「plugins 根目录」和宿主
# 「src」临时放进 sys.path，从不加入本插件目录（src/plugin_runtime/runner/
# plugin_loader.py:567-585）；本插件又用绝对导入引用自身模块，故必须自行把插件
# 目录放到 sys.path 首位，否则自身模块会被宿主 src/ 下的同名模块抢先命中。
# 配置模块命名为 harness_config 而非 config：本插件与 cateye_test 同进程加载，
# 顶层 'config' 会被宿主 src/config 或先加载的兄弟插件占住 sys.modules。
import os as _os
import sys as _sys

_PLUGIN_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PLUGIN_DIR in _sys.path:
    _sys.path.remove(_PLUGIN_DIR)
_sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import Command, HookHandler, MaiBotPlugin, MessageGateway
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from harness_config import CateyeHarnessConfig
from scenes import SCENES, get_scene, scene_names, validate_scene

# 本插件自己的注入网关（receive；名字与宿主组件注册一致）
INJECT_GATEWAY_NAME = "harness_inject_gateway"
PLATFORM = "qq"
LOG_TAG = "[harness]"
_MAX_LOG_CHARS = 1600
# 插件 id / 数据目录名的安全白名单（防路径穿越；配置值也过这道闸）
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# ---- 文件任务通道（v0.1.1）----
TASKS_DIRNAME = "tasks"       # <本插件数据目录>/tasks/*.json
RESULTS_DIRNAME = "results"   # <本插件数据目录>/results/<id>.json
DONE_DIRNAME = "done"         # 处理完的任务归档到 tasks/done/
TASK_POLL_SECONDS = 2.0       # 轮询间隔
# 动作白名单（**只认这五个字符串**；白名单外一律拒绝、写错误回执）。
# 参数一律当纯数据用：只做 .get()/str()/int()/float() 取值，绝不做动态代码执行/动态导入。
TASK_ACTIONS = ("inject", "seq", "state", "freeze", "resume")
# Windows 保留设备名（回执文件名防撞）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}


class HarnessError(RuntimeError):
    """夹具可预期失败：面向测试 agent 的明确报错（命令层转成文本返回，不抛给宿主）。"""


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _fmt(value: Any, limit: int = _MAX_LOG_CHARS) -> str:
    """把任意返回/状态对象压成一行紧凑文本（超长截断）。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=False)
        except Exception:
            text = repr(value)
    if len(text) > limit:
        text = text[:limit] + f"…(截断，共 {len(text)} 字符)"
    return text


def _as_bool(value: Any, default: bool = False) -> bool:
    """解析注入参数里的布尔值（1/true/yes/on 为真，0/false/no/off 为假）。"""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _kv_args(raw: str) -> Dict[str, str]:
    """解析 ``key=value`` 参数串。

    支持双引号包裹带空格的值（``text="你好 世界"``）；裸词按 ``key=value`` 切分；
    不带 ``=`` 的裸词归入 ``_positional`` 列表。
    """
    result: Dict[str, str] = {}
    positional: List[str] = []
    text = str(raw or "")
    token: List[str] = []
    in_quote = False
    tokens: List[str] = []
    for ch in text:
        if ch == '"':
            in_quote = not in_quote
            token.append(ch)
            continue
        if ch.isspace() and not in_quote:
            if token:
                tokens.append("".join(token))
                token = []
            continue
        token.append(ch)
    if token:
        tokens.append("".join(token))
    for item in tokens:
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip().lower()] = value.strip().strip('"')
        else:
            positional.append(item.strip('"'))
    if positional:
        result["_positional"] = " ".join(positional)
    return result


def _coerce_value(value: Any) -> Any:
    """k=v 值轻量转换：全数字 → int，true/false → bool，其余保持字符串。"""
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return text


def _extract_stream_id(payload: Any) -> str:
    """从 chat 能力返回值里取 stream_id（兼容 dict / 嵌套 stream / 异常形态）。"""
    if isinstance(payload, dict):
        stream_id = str(payload.get("stream_id") or "").strip()
        if stream_id:
            return stream_id
        nested = payload.get("stream")
        if isinstance(nested, dict):
            return str(nested.get("stream_id") or "").strip()
    return ""


# ----------------------------------------------------------------------
# 插件
# ----------------------------------------------------------------------
class CateyeHarnessPlugin(MaiBotPlugin):
    """宿主自测试夹具（无 LLM、无主动发言）。"""

    config_model: ClassVar[type] = CateyeHarnessConfig

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def on_load(self) -> None:
        # 环路守卫内存态：只存时间戳（float），不存任何消息内容
        self._guard_window: deque[float] = deque()
        self._last_guard_fire: float = 0.0
        self._inject_ready: bool = False
        self._frozen_components: List[Tuple[str, str]] = []
        self._scene_lock = asyncio.Lock()
        self._scene_task: Optional["asyncio.Task[None]"] = None
        self._scene_name: str = ""
        self._file_tasks: Optional["asyncio.Task[None]"] = None

        cfg = self.config.harness
        if not self.config.plugin.enabled:
            self.ctx.logger.info(
                "%s 插件未启用（plugin.enabled=false）：组件已注册，注入网关不上报就绪", LOG_TAG
            )
            return
        await self._activate_inject_gateway()
        self._start_file_channel()
        self.ctx.logger.info(
            "%s 已加载：注入网关=%s(ready=%s)，测试群=%s 测试私聊=%s 对端守卫=%s，熔断目标=%s，场景=%s",
            LOG_TAG, INJECT_GATEWAY_NAME, self._inject_ready,
            cfg.test_group_id, cfg.test_private_qq, cfg.guard_peer_qq,
            cfg.freeze_target_plugin, ",".join(scene_names()),
        )

    async def on_unload(self) -> None:
        task = getattr(self, "_scene_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._stop_file_channel()
        try:
            await self.ctx.gateway.update_state(INJECT_GATEWAY_NAME, ready=False)
        except Exception as exc:  # 卸载路径不抛异常
            self.ctx.logger.warning("%s 注入网关下线状态上报失败：%s", LOG_TAG, exc)
        self._inject_ready = False
        self._guard_window.clear()
        self.ctx.logger.info("%s 已卸载", LOG_TAG)

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != "self":
            return
        # 配置热重载：清空守卫窗口与冷却，避免旧窗口按新阈值误触发
        self._guard_window.clear()
        self._last_guard_fire = 0.0
        if not self.config.plugin.enabled:
            try:
                await self.ctx.gateway.update_state(INJECT_GATEWAY_NAME, ready=False)
            except Exception:
                pass
            self._inject_ready = False
            await self._stop_file_channel()
            self.ctx.logger.info("%s 配置热重载：插件已停用，注入网关与文件任务通道随之下线", LOG_TAG)
            return
        await self._activate_inject_gateway()
        self._start_file_channel()
        self.ctx.logger.info(
            "%s 配置热重载 v%s：守卫=%s 窗口=%smin 阈值=%s 冷却=%smin；注入网关 ready=%s",
            LOG_TAG, version or "-", self.config.harness.guard_enabled,
            self.config.harness.guard_window_minutes, self.config.harness.guard_round_trips,
            self.config.harness.guard_cooldown_minutes, self._inject_ready,
        )

    # ==================================================================
    # 注入网关（receive，仅作 §10.4 注入载体）
    # ==================================================================
    @MessageGateway(
        route_type="receive",
        name=INJECT_GATEWAY_NAME,
        platform=PLATFORM,
        protocol="harness",
        description="cateye_harness 合成消息注入网关（只收；真实注入由 ctx.gateway.route_message 完成）",
    )
    async def handle_harness_inject_gateway(self, **kwargs: Any) -> None:
        """组件载体：Host 不会向 receive-only 网关投递出站消息，此方法无需实现逻辑。"""
        del kwargs
        return None

    async def _activate_inject_gateway(self) -> None:
        """上报注入网关 ready（route_message 的前置条件，宿主 supervisor.py:1673）。

        刻意**不带 account_id/scope**：注册出的接收路由为 (qq, None, None)，
        既能覆盖带 self_id=100000001 的入站路由键（RouteKey.resolution_order 回退），
        又不会把本夹具登记成 qq/100000001 的“适配器账号”（避免污染 WebUI 适配器面板）。
        """
        try:
            ok = await self.ctx.gateway.update_state(
                INJECT_GATEWAY_NAME, ready=True, platform=PLATFORM
            )
            self._inject_ready = bool(ok)
        except Exception as exc:
            self._inject_ready = False
            self.ctx.logger.error("%s 注入网关就绪上报失败（/h_inject 将拒绝执行）：%s", LOG_TAG, exc)
            return
        if not self._inject_ready:
            self.ctx.logger.error("%s 注入网关就绪上报被宿主拒绝（accepted=False）", LOG_TAG)

    async def _get_all_plugins(self) -> Dict[str, Any]:
        """取 get_all_plugins 结果（SDK 已解包成 {plugin_id: info}）；失败抛 HarnessError。"""
        resp = await self.ctx.component.get_all_plugins()
        if isinstance(resp, dict) and resp.get("success") is False:
            raise HarnessError(f"component.get_all_plugins 失败：{resp.get('error')}")
        if not isinstance(resp, dict):
            raise HarnessError(f"component.get_all_plugins 返回形态异常：{_fmt(resp, 300)}")
        return resp

    async def _preflight_injection(self) -> None:
        """注入前置检查：本插件启用 → 注入网关 ready → 真实 snowluma 链路 ready。

        任一项不满足即抛 HarnessError（明确报错、不注入），对应 §四 实现注意。
        """
        if not self.config.plugin.enabled:
            raise HarnessError("插件未启用（plugin.enabled=false），拒绝注入")
        if not self._inject_ready:
            await self._activate_inject_gateway()
        if not self._inject_ready:
            raise HarnessError(
                f"注入网关 {INJECT_GATEWAY_NAME} 未就绪（update_state ready 上报失败），拒绝注入"
            )
        cfg = self.config.harness
        if not cfg.verify_adapter_ready:
            return
        plugins = await self._get_all_plugins()
        info = plugins.get(cfg.adapter_plugin_id)
        if not isinstance(info, dict):
            raise HarnessError(
                f"真实网关未就绪：适配器插件 {cfg.adapter_plugin_id} 未装载"
                f"（已装载：{', '.join(sorted(plugins)[:8]) or '无'}）；请确认 SnowLuma 侧已启动"
            )
        if info.get("enabled") is False:
            raise HarnessError(f"真实网关未就绪：适配器插件 {cfg.adapter_plugin_id} 处于停用状态")
        components = info.get("components") if isinstance(info.get("components"), list) else []
        gateway_ok = any(
            str(c.get("name") or "") == cfg.adapter_gateway_name
            and str(c.get("type") or "").strip().upper() == "MESSAGE_GATEWAY"
            for c in components
            if isinstance(c, dict)
        )
        if not gateway_ok:
            raise HarnessError(
                f"真实网关未就绪：{cfg.adapter_plugin_id} 未包含网关组件 {cfg.adapter_gateway_name}"
            )
        probe_error = await self._probe_adapter()
        if probe_error:
            raise HarnessError(f"真实网关未就绪：{probe_error}")

    async def _probe_adapter(self) -> str:
        """调适配器只读 API 探测链路；返回空串 = 就绪，否则返回失败原因。"""
        cfg = self.config.harness
        try:
            resp = await self.ctx.api.call(cfg.adapter_probe_api)
        except Exception as exc:
            return f"{cfg.adapter_probe_api} 调用异常（{exc}）"
        if resp is None:
            return f"{cfg.adapter_probe_api} 无响应"
        if isinstance(resp, dict):
            if resp.get("success") is False:
                return f"{cfg.adapter_probe_api} 调用失败（{resp.get('error')}）"
            status = resp.get("status")
            retcode = resp.get("retcode")
            if status == "failed" or (isinstance(retcode, int) and retcode not in (0, 1)):
                return (
                    f"{cfg.adapter_probe_api} 返回未连接状态"
                    f"（status={status!r}, retcode={retcode!r}, {resp.get('wording') or resp.get('msg') or ''}）"
                )
            data = resp.get("data")
            if isinstance(data, dict):
                login_id = str(data.get("user_id") or data.get("self_id") or "").strip()
                if login_id and login_id != str(cfg.self_id):
                    return (
                        f"适配器登录账号为 {login_id}，与配置 self_id={cfg.self_id} 不一致，"
                        "拒绝把消息注入到未知账号"
                    )
        return ""

    # ==================================================================
    # MessageDict 构造与注入核心（§10.2 / §10.4）
    # ==================================================================
    def _resolve_target(self, params: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, str]]]:
        """解析并校验注入目标：返回 (target_kind, target_id, group_info)。

        目标白名单（比 §四 的 sender 校验更严，直接落实 §一 红线）：
        群只允许 test_group_id + extra_allowed_group_ids，私聊只允许 test_private_qq + 额外项；
        100000005 / 100000004 等默认一律拒绝。
        """
        cfg = self.config.harness
        group_id = params.get("group_id")
        private_qq = params.get("private_qq")
        has_group = group_id is not None and str(group_id).strip() != ""
        has_private = private_qq is not None and str(private_qq).strip() != ""
        if has_group == has_private:
            raise HarnessError("必须且只能指定 group_id 或 private_qq 之一")
        allowed_groups = {str(cfg.test_group_id)} | {str(x) for x in cfg.extra_allowed_group_ids}
        allowed_private = {str(cfg.test_private_qq)} | {str(x) for x in cfg.extra_allowed_private_qqs}
        if has_group:
            gid = str(group_id).strip()
            if gid not in allowed_groups:
                self.ctx.logger.warning("%s 拒绝注入：群 %s 不在目标白名单（§一 只许旁观群禁激活）", LOG_TAG, gid)
                raise HarnessError(
                    f"目标群 {gid} 不在白名单（仅 {', '.join(sorted(allowed_groups))}）；"
                    "100000005 为只许旁观群，禁止主动激活"
                )
            group_name = str(params.get("group_name") or "").strip() or f"harness-test-group-{gid}"
            return "group", gid, {"group_id": gid, "group_name": group_name}
        qq = str(private_qq).strip()
        if qq not in allowed_private:
            self.ctx.logger.warning("%s 拒绝注入：私聊 %s 不在目标白名单（§一 默认不碰 100000004）", LOG_TAG, qq)
            raise HarnessError(
                f"目标私聊 {qq} 不在白名单（仅 {', '.join(sorted(allowed_private))}）；"
                "100000004 是另一台 bot，默认完全不碰"
            )
        return "private", qq, None

    def _build_message_dict(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """按开发文档 §10.2 构造标准 MessageDict。"""
        cfg = self.config.harness
        text = str(params.get("text") or "")
        if not text.strip():
            raise HarnessError("text 不能为空")

        sender_id = str(params.get("sender_id") or cfg.sender_default).strip()
        blocked = {str(x) for x in cfg.blocked_senders}
        if sender_id in blocked:
            self.ctx.logger.warning("%s 拒绝注入：sender=%s 命中黑名单（§一 红线①）", LOG_TAG, sender_id)
            raise HarnessError(
                f"sender {sender_id} 命中黑名单（禁止注入成另一台 bot，§一 红线①）"
            )
        sender_nickname = str(params.get("sender_nickname") or "").strip() or f"测试用户{sender_id}"

        target_kind, target_id, group_info = self._resolve_target(params)

        is_at = _as_bool(params.get("is_at"), False)
        is_notify = _as_bool(params.get("is_notify"), False)
        reply_to = str(params.get("reply_to_msg_id") or "").strip()

        # 消息段：text（+ 可选 at / reply）
        raw_message: List[Dict[str, Any]] = [{"type": "text", "data": text}]
        if is_at:
            raw_message.append({
                "type": "at",
                "data": {
                    "target_user_id": str(cfg.self_id),
                    "target_user_nickname": cfg.self_nickname,
                },
            })
        if reply_to:
            raw_message.append({
                "type": "reply",
                "data": {"target_message_id": reply_to},
            })

        message_id = f"harness-{uuid.uuid4().hex}"
        additional_config = {
            "self_id": str(cfg.self_id),          # §10.2：多账号定位/私聊路由依赖
            "harness_injected": True,             # 自带标记：被测插件/守卫可据此跳过自身注入
            "harness_target": f"{target_kind}:{target_id}",
        }
        message: Dict[str, Any] = {
            "message_id": message_id,
            "timestamp": str(time.time()),
            "platform": PLATFORM,
            "message_info": {
                "user_info": {
                    "user_id": sender_id,
                    "user_nickname": sender_nickname,
                    "user_cardname": None,
                },
                "group_info": group_info,          # 群聊必填、私聊为 None（§10.2）
                "additional_config": additional_config,
            },
            "raw_message": raw_message,
            "is_mentioned": is_at,                # is_mentioned 与 is_at 分开（§10.2）
            "is_at": is_at,
            "is_emoji": False,
            "is_picture": False,
            "is_command": text.lstrip().startswith("/"),  # 命令消息可直接注入（§四 实现注意）
            "is_notify": is_notify,               # True：只入库不触发回复（§10.4）
            "session_id": "",                     # 可选，Host 重新计算
            "reply_to": reply_to or None,
            "processed_plain_text": text,         # 可选，Host 会按 raw_message 重新生成
        }
        return message

    async def _inject_built_message(self, message: Dict[str, Any]) -> None:
        """经本插件 receive 网关注入（§10.4 route_message）。"""
        await self._preflight_injection()
        cfg = self.config.harness
        try:
            accepted = await self.ctx.gateway.route_message(
                INJECT_GATEWAY_NAME,
                message,
                route_metadata={"self_id": str(cfg.self_id)},
                external_message_id=message["message_id"],
                dedupe_key=message["message_id"],
            )
        except Exception as exc:
            raise HarnessError(f"route_message 调用失败：{exc}") from exc
        if not accepted:
            raise HarnessError(
                "Host 拒绝注入（accepted=False）：可能是网关未就绪、适配器策略未放行或去重命中"
            )

    async def _inject_from_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """构造 + 注入，返回构造出的 MessageDict（供命令/场景复用）。"""
        message = self._build_message_dict(params)
        await self._inject_built_message(message)
        return message

    @staticmethod
    def _describe_message(message: Dict[str, Any]) -> str:
        """一句话描述注入消息（供日志）。"""
        info = message.get("message_info") or {}
        user = info.get("user_info") or {}
        group = info.get("group_info") or {}
        target = f"群{group.get('group_id')}" if group else f"私聊{user.get('user_id')}"
        kind = "通知(不触发回复)" if message.get("is_notify") else "普通消息"
        return (
            f"id={message.get('message_id')} → {target} sender={user.get('user_id')}"
            f"({user.get('user_nickname')}) {kind} text={message.get('processed_plain_text')!r}"
        )

    async def _core_inject(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """注入核心（``/h_inject`` 命令与文件通道 ``inject`` 动作共用，行为完全一致）。

        返回 (ok, 文本, 构造出的 MessageDict 或 None)；白名单/黑名单校验全部复用
        ``_build_message_dict`` → ``_resolve_target``，文件通道拿不到任何绕过入口。
        """
        try:
            message = await self._inject_from_params(params)
        except HarnessError as exc:
            return False, f"注入失败：{exc}", None
        except Exception as exc:
            self.ctx.logger.exception("%s 注入异常", LOG_TAG)
            return False, f"注入异常：{exc}", None
        return True, f"注入成功：{self._describe_message(message)}", message

    # ==================================================================
    # 命令公共设施
    # ==================================================================
    async def _prepare_command(self, kwargs: Dict[str, Any]) -> Optional[Tuple[bool, str, bool]]:
        """命令入口公共检查：启用状态 + 调用者白名单。返回 None 表示放行。"""
        if not self.config.plugin.enabled:
            return False, "插件未启用（plugin.enabled=false）", True
        user_id = str(kwargs.get("user_id") or "")
        is_local = bool(kwargs.get("is_local_operator"))
        allowed = {str(x) for x in self.config.harness.command_allowed_users}
        if not is_local and user_id not in allowed:
            self.ctx.logger.warning(
                "%s 拒绝命令：调用者 %s 不在 command_allowed_users（本机控制台除外）", LOG_TAG, user_id
            )
            return False, f"拒绝：{user_id or '未知调用者'} 不在命令白名单", True
        return None

    def _finish(self, ok: bool, text: str) -> Tuple[bool, str, bool]:
        """命令统一出口：写宿主日志（插件 logger 自动 IPC 转发）+ 返回 response。

        刻意不调用 ctx.send.*：本夹具不主动发言（§四），命令输出走宿主日志，
        agent 用日志/WebUI 读取；需要真实消息请显式 /h_say。
        """
        if ok:
            self.ctx.logger.info("%s %s", LOG_TAG, text)
        else:
            self.ctx.logger.error("%s %s", LOG_TAG, text)
        return ok, text, True

    # ==================================================================
    # /h_inject
    # ==================================================================
    @Command(
        "h_inject",
        description="构造 MessageDict 经注入网关注入宿主（核心命令）",
        pattern=r"(?<!\S)/h_inject(?:\s+(?P<args>.*))?$",
    )
    async def cmd_h_inject(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        args = str((kwargs.get("matched_groups") or {}).get("args") or "").strip()
        params = _kv_args(args)
        if not params:
            return self._finish(
                False,
                "用法：/h_inject group_id=100000003 text=\"...\" [sender_id=100000002] "
                "[sender_nickname=..] [is_at=1] [is_notify=1] [reply_to_msg_id=..]  "
                "或 private_qq=100000002；text 带空格请用双引号",
            )
        ok, text, _ = await self._core_inject(params)   # 与文件通道 inject 动作同一核心
        return self._finish(ok, text)

    # ==================================================================
    # /h_seq
    # ==================================================================
    def _scene_plan_lines(self, name: str, scene: Dict[str, Any]) -> List[str]:
        """把场景定义渲染成 dry-run 计划文本。"""
        lines = [
            f"场景 {name}｜里程碑：{scene.get('milestone')}｜目的：{scene.get('goal')}",
            f"步进 {len(scene.get('steps') or [])} 条：",
        ]
        for idx, step in enumerate(scene.get("steps") or [], 1):
            kind = str(step.get("type") or "")
            if kind == "inject":
                target = f"群{step.get('group_id')}" if step.get("group_id") is not None else f"私聊{step.get('private_qq')}"
                flags = []
                if step.get("is_at"):
                    flags.append("is_at")
                if step.get("is_notify"):
                    flags.append("is_notify(只入库)")
                if step.get("reply_to_msg_id"):
                    flags.append(f"reply={step.get('reply_to_msg_id')}")
                flag_text = f" [{','.join(flags)}]" if flags else ""
                lines.append(
                    f"  {idx}. inject {target} sender={step.get('sender_id') or self.config.harness.sender_default}"
                    f"{flag_text} text={step.get('text')!r}"
                    + (f"　# {step.get('desc')}" if step.get("desc") else "")
                )
            elif kind == "sleep":
                lines.append(f"  {idx}. sleep {step.get('seconds')}s" + (f"　# {step.get('desc')}" if step.get("desc") else ""))
            else:
                lines.append(f"  {idx}. note {step.get('text')}")
        manual = scene.get("manual_notes") or []
        if manual:
            lines.append("仅宿主/真人侧预置（夹具不代做，执行时原样提醒）：")
            lines.extend(f"  - {item}" for item in manual)
        assertions = scene.get("assertions") or []
        if assertions:
            lines.append("关键断言（§六）：")
            lines.extend(f"  ✔ {item}" for item in assertions)
        return lines

    @Command(
        "h_seq",
        description="列出/预览/执行预置测试场景（里程碑场景脚本）",
        pattern=r"(?<!\S)/h_seq(?:\s+(?P<args>.*))?$",
    )
    async def cmd_h_seq(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        args = str((kwargs.get("matched_groups") or {}).get("args") or "").strip()
        if not args:
            lines = [f"已登记场景 {len(SCENES)} 个（/h_seq <场景名> 预览；/h_seq <场景名> run 执行）："]
            for name in scene_names():
                scene = SCENES[name]
                lines.append(
                    f"  - {name}｜{scene.get('milestone')}｜{scene.get('goal')}｜步进 {len(scene.get('steps') or [])} 条"
                )
            return self._finish(True, "\n".join(lines))

        parts = args.split()
        name = parts[0]
        execute = any(p.lower() in {"run", "execute", "--run"} for p in parts[1:])
        scene, lookup_error = self._core_seq_lookup(name)   # 与文件通道 seq 动作同一查找/校验
        if scene is None:
            return self._finish(False, lookup_error)

        plan = self._scene_plan_lines(name, scene)
        if not execute:
            plan.insert(0, "【dry-run】只打印计划，未执行任何注入；确认无误后加 `run` 执行。")
            return self._finish(True, "\n".join(plan))

        # 执行模式：命令立刻返回，步进在后台任务里跑（长场景含分钟级 sleep，
        # 放前台会撞组件 RPC 默认 60s 超时，宿主 component_timeout.py:9）。
        if self._scene_task is not None and not self._scene_task.done():
            return self._finish(False, f"已有场景在后台执行中（{self._scene_name}），请等它结束或 /h_freeze")
        try:
            await self._preflight_injection()   # 宿主/网关不在场 → 明确报错、不执行
        except HarnessError as exc:
            return self._finish(False, f"场景未执行（前置检查失败）：{exc}")
        except Exception as exc:
            return self._finish(False, f"场景未执行（前置检查异常）：{exc}")
        self._scene_name = name
        self._scene_task = asyncio.create_task(self._run_scene_bg(name, scene))
        self._scene_task.add_done_callback(self._on_scene_done)
        plan.insert(0, "【执行】前置检查通过，步进已在后台启动；每条结果以 [harness] 前缀写入宿主日志。")
        return self._finish(True, "\n".join(plan))

    async def _run_scene_steps(self, name: str, scene: Dict[str, Any]) -> Dict[str, Any]:
        """步进执行器（命令后台任务与文件通道 ``seq`` 动作共用）：

        inject 复用注入核心，sleep 用 asyncio.sleep，逐条写宿主日志；
        返回分步结果摘要（文件通道拿它落回执），日志文本与 v0.1.0 完全一致。
        """
        async with self._scene_lock:  # 场景串行，避免互相干扰
            total = len(scene.get("steps") or [])
            started = time.time()
            steps: List[Dict[str, Any]] = []
            self.ctx.logger.info("%s ▶ 场景 %s 开始（%s 步）", LOG_TAG, name, total)

            def _abort(idx: int, kind: str, desc: Any, detail: str) -> Dict[str, Any]:
                steps.append({"index": idx, "type": kind, "status": "error",
                              "desc": str(desc or ""), "summary": detail})
                return {
                    "ok": False, "total": total, "completed": len(steps) - 1, "aborted_at": idx,
                    "steps": steps, "elapsed_seconds": round(time.time() - started, 3),
                    "error": f"第 {idx} 步失败：{detail}",
                }

            for idx, step in enumerate(scene.get("steps") or [], 1):
                kind = str(step.get("type") or "")
                try:
                    if kind == "inject":
                        message = await self._inject_from_params(step)
                        self.ctx.logger.info(
                            "%s ▶ [%s %s/%s] ✅ 注入成功：%s",
                            LOG_TAG, name, idx, total, self._describe_message(message),
                        )
                        info = message.get("message_info") or {}
                        steps.append({
                            "index": idx, "type": "inject", "status": "ok",
                            "desc": str(step.get("desc") or ""),
                            "summary": self._describe_message(message),
                            "message_id": message.get("message_id"),
                            "target": (info.get("additional_config") or {}).get("harness_target"),
                        })
                    elif kind == "sleep":
                        seconds = float(step.get("seconds") or 0)
                        self.ctx.logger.info("%s ▶ [%s %s/%s] … sleep %ss", LOG_TAG, name, idx, total, seconds)
                        await asyncio.sleep(seconds)
                        steps.append({"index": idx, "type": "sleep", "status": "ok",
                                      "desc": str(step.get("desc") or ""), "summary": f"sleep {seconds}s"})
                    else:
                        self.ctx.logger.info("%s ▶ [%s %s/%s] 📝 %s", LOG_TAG, name, idx, total, step.get("text"))
                        steps.append({"index": idx, "type": kind or "note", "status": "ok",
                                      "desc": "", "summary": str(step.get("text") or "")})
                except HarnessError as exc:
                    self.ctx.logger.error("%s ▶ [%s %s/%s] ❌ 步进失败，场景中止：%s", LOG_TAG, name, idx, total, exc)
                    return _abort(idx, kind, step.get("desc"), str(exc))
                except asyncio.CancelledError:
                    self.ctx.logger.warning("%s ▶ [%s %s/%s] 场景被取消（插件卸载/熔断）", LOG_TAG, name, idx, total)
                    raise
                except Exception as exc:
                    self.ctx.logger.exception("%s 场景 %s 第 %s 步异常，场景中止", LOG_TAG, name, idx)
                    self.ctx.logger.error("%s ▶ [%s %s/%s] ❌ 异常，场景中止：%s", LOG_TAG, name, idx, total, exc)
                    return _abort(idx, kind, step.get("desc"), f"{type(exc).__name__}: {exc}")
            self.ctx.logger.info("%s ▶ 场景 %s 步进结束，请逐条核对关键断言（§六）：", LOG_TAG, name)
            for item in scene.get("assertions") or []:
                self.ctx.logger.info("%s ▶   ✔ %s", LOG_TAG, item)
            for item in scene.get("manual_notes") or []:
                self.ctx.logger.info("%s ▶   预置提醒：%s", LOG_TAG, item)
            return {
                "ok": True, "total": total, "completed": len(steps), "aborted_at": None,
                "steps": steps, "elapsed_seconds": round(time.time() - started, 3),
                "assertions": list(scene.get("assertions") or []),
                "manual_notes": list(scene.get("manual_notes") or []),
            }

    async def _run_scene_bg(self, name: str, scene: Dict[str, Any]) -> None:
        """命令通道后台步进（/h_seq ... run）：复用同一执行器，结果只进宿主日志。"""
        await self._run_scene_steps(name, scene)

    @staticmethod
    def _core_seq_lookup(name: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """场景查找 + schema 校验（``/h_seq`` 命令与文件通道 ``seq`` 动作共用）。

        返回 (场景定义, 错误文本)；查不到/非法时场景为 None，错误文本与 v0.1.0 命令逐字一致。
        """
        scene = get_scene(name)
        if scene is None:
            return None, f"未登记场景 {name!r}；可选：{', '.join(scene_names())}"
        problems = validate_scene(scene)
        if problems:
            return None, f"场景 {name} 定义非法：" + "；".join(problems)
        return scene, ""

    async def _core_seq_execute(self, name: str, scene: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """场景执行核心（文件通道 ``seq`` 动作）：前置检查 + 真步进，返回分步结果。

        与 ``/h_seq <名> run`` 的前置检查/执行器完全一致，区别只在：前置失败不启动后台任务、
        步进结果直接进回执（命令通道由后台任务写日志）。
        """
        try:
            await self._preflight_injection()   # 宿主/网关不在场 → 明确报错、不执行
        except HarnessError as exc:
            return False, f"场景未执行（前置检查失败）：{exc}", {"scene": name, "steps": []}
        except Exception as exc:
            return False, f"场景未执行（前置检查异常）：{exc}", {"scene": name, "steps": []}
        result = await self._run_scene_steps(name, scene)
        extra = {"scene": name, "steps": result.get("steps") or [],
                 "total": result.get("total"), "completed": result.get("completed"),
                 "aborted_at": result.get("aborted_at"),
                 "elapsed_seconds": result.get("elapsed_seconds")}
        if result.get("ok"):
            injected = sum(1 for s in extra["steps"] if s.get("type") == "inject" and s.get("status") == "ok")
            summary = (
                f"场景 {name} 步进完成：{extra['completed']}/{extra['total']} 步全部成功"
                f"（注入 {injected} 条，耗时 {extra['elapsed_seconds']}s）；"
                f"分步结果见 steps 字段，关键断言请对照 §六"
            )
            return True, summary, extra
        summary = (
            f"场景 {name} 中止于第 {extra['aborted_at']} 步：{result.get('error')}"
            f"（已完成 {extra['completed']}/{extra['total']} 步，耗时 {extra['elapsed_seconds']}s）；"
            f"分步结果见 steps 字段"
        )
        return False, summary, extra

    @staticmethod
    def _scene_plan_steps(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把场景步进渲染成结构化预览（文件通道 dry-run 的 steps 字段）。"""
        preview: List[Dict[str, Any]] = []
        for idx, step in enumerate(scene.get("steps") or [], 1):
            kind = str(step.get("type") or "")
            if kind == "inject":
                target = (f"群{step.get('group_id')}" if step.get("group_id") is not None
                          else f"私聊{step.get('private_qq')}")
                detail = f"inject → {target} text={step.get('text')!r}"
            elif kind == "sleep":
                detail = f"sleep {step.get('seconds')}s"
            else:
                detail = str(step.get("text") or "")
            preview.append({"index": idx, "type": kind, "status": "planned",
                            "desc": str(step.get("desc") or ""), "summary": detail})
        return preview

    def _on_scene_done(self, task: "asyncio.Task[None]") -> None:
        """场景后台任务回收：记录未捕获异常，避免静默丢失。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.ctx.logger.error("%s 场景后台任务异常退出：%s", LOG_TAG, exc)
        self._scene_name = ""

    # ==================================================================
    # /h_freeze 与 /h_resume（熔断/恢复）
    # ==================================================================
    async def _do_freeze(self, reason: str) -> str:
        """停用被测插件的 tool/hook 组件；返回结果文本（守卫与命令共用）。"""
        cfg = self.config.harness
        plugins = await self._get_all_plugins()
        info = plugins.get(cfg.freeze_target_plugin)
        if not isinstance(info, dict):
            return (
                f"熔断未执行：未找到目标插件 {cfg.freeze_target_plugin}"
                f"（已装载：{', '.join(sorted(plugins)[:8]) or '无'}）"
            )
        wanted = {str(t).strip().upper() for t in cfg.freeze_component_types}
        # HOOK_HANDLER 先于 TOOL：先断事件链，再断工具面
        order = {"HOOK_HANDLER": 0, "TOOL": 1}
        targets = [
            c for c in (info.get("components") or [])
            if isinstance(c, dict) and str(c.get("type") or "").strip().upper() in wanted
        ]
        targets.sort(key=lambda c: (order.get(str(c.get("type") or "").strip().upper(), 9),
                                    str(c.get("full_name") or c.get("name") or "")))
        disabled: List[Tuple[str, str]] = []
        failed: List[str] = []
        for comp in targets:
            name = str(comp.get("full_name") or comp.get("name") or "").strip()
            ctype = str(comp.get("type") or "").strip().upper()
            if not name:
                continue
            if comp.get("enabled") is False:
                disabled.append((name, ctype))  # 记录现状，便于 /h_resume 统一恢复
                continue
            try:
                resp = await self.ctx.component.disable_component(name, ctype)
            except Exception as exc:
                failed.append(f"{name}({ctype}): {exc}")
                continue
            if isinstance(resp, dict) and resp.get("success") is False:
                failed.append(f"{name}({ctype}): {resp.get('error')}")
            else:
                disabled.append((name, ctype))
        for item in disabled:
            if item not in self._frozen_components:
                self._frozen_components.append(item)
        summary = (
            f"【熔断】原因：{reason}；目标插件：{cfg.freeze_target_plugin}；"
            f"已停用 {len(disabled)}/{len(targets)} 个组件"
            + (f"；失败 {len(failed)} 个：{'; '.join(failed)}" if failed else "")
            + "。注意：patrol/emotion 等后台 asyncio 循环不是组件，必须把被测插件 "
              "plugin.enabled=false 才会停；本命令不调 LLM、不发送任何消息。"
        )
        self.ctx.logger.error("%s ⛔ %s", LOG_TAG, summary)
        return summary

    async def _do_resume(self) -> str:
        """恢复熔断停用的组件（优先内存记录，退化到“全量启用目标类型”）。"""
        cfg = self.config.harness
        # 逆序恢复：后停的先恢复（与 _do_freeze 的 HOOK→TOOL 停用顺序相反）
        candidates: List[Tuple[str, str]] = list(reversed(self._frozen_components))
        if not candidates:
            plugins = await self._get_all_plugins()
            info = plugins.get(cfg.freeze_target_plugin)
            if not isinstance(info, dict):
                return f"恢复未执行：未找到目标插件 {cfg.freeze_target_plugin}"
            wanted = {str(t).strip().upper() for t in cfg.freeze_component_types}
            candidates = [
                (str(c.get("full_name") or c.get("name") or ""), str(c.get("type") or "").strip().upper())
                for c in (info.get("components") or [])
                if isinstance(c, dict) and str(c.get("type") or "").strip().upper() in wanted
            ]
        enabled: List[str] = []
        failed: List[str] = []
        for name, ctype in candidates:
            if not name:
                continue
            try:
                resp = await self.ctx.component.enable_component(name, ctype)
            except Exception as exc:
                failed.append(f"{name}({ctype}): {exc}")
                continue
            if isinstance(resp, dict) and resp.get("success") is False:
                failed.append(f"{name}({ctype}): {resp.get('error')}")
            else:
                enabled.append(f"{name}({ctype})")
        self._frozen_components = [
            item for item in self._frozen_components if item not in set(candidates)
        ]
        summary = (
            f"【恢复】目标插件：{cfg.freeze_target_plugin}；已启用 {len(enabled)} 个组件"
            + (f"；失败 {len(failed)} 个：{'; '.join(failed)}" if failed else "")
            + ("" if enabled else "（无非内存记录可用项）")
        )
        self.ctx.logger.info("%s ✅ %s", LOG_TAG, summary)
        return summary

    @Command("h_freeze", description="熔断：停用被测插件 tool/hook 组件", pattern=r"(?<!\S)/h_freeze\s*$")
    async def cmd_h_freeze(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        try:
            summary = await self._do_freeze("手动 /h_freeze")
        except HarnessError as exc:
            return self._finish(False, f"熔断失败：{exc}")
        return self._finish(True, summary)

    @Command("h_resume", description="恢复：启用被测插件被停用的 tool/hook 组件", pattern=r"(?<!\S)/h_resume\s*$")
    async def cmd_h_resume(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        try:
            summary = await self._do_resume()
        except HarnessError as exc:
            return self._finish(False, f"恢复失败：{exc}")
        return self._finish(True, summary)

    # ==================================================================
    # 环路守卫（chat.receive.after_process / OBSERVE）
    # ==================================================================
    @HookHandler(
        "chat.receive.after_process",
        name="harness_loop_guard",
        description="麦麦⇄100000004 私聊环路守卫：只计数时间戳，窗口内达阈值自动熔断",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.LOG,
        # 计数路径亚毫秒级；20s 只为触发时那一次熔断 RPC 留足余量，
        # 避免超时被宿主 hook_dispatcher 记进插件熔断器（连续超时会摘掉本处理器）。
        timeout_ms=20000,
    )
    async def handle_loop_guard(self, **kwargs: Any) -> None:
        """观察入站消息并计数（OBSERVE 只读旁路：不改 kwargs、不中止链路）。"""
        try:
            cfg = self.config.harness
            if not cfg.guard_enabled or not self.config.plugin.enabled:
                return
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return
            info = message.get("message_info")
            if not isinstance(info, dict):
                return
            if info.get("group_info"):          # 只盯私聊
                return
            user_info = info.get("user_info")
            if not isinstance(user_info, dict):
                return
            additional = info.get("additional_config")
            additional = additional if isinstance(additional, dict) else {}
            if additional.get("harness_injected"):   # 夹具自己的注入不计
                return
            self_id = str(additional.get("self_id") or "").strip()
            sender = str(user_info.get("user_id") or "").strip()
            if self_id != str(cfg.self_id) or sender != str(cfg.guard_peer_qq):
                return

            now = time.monotonic()
            window_seconds = max(1, int(cfg.guard_window_minutes)) * 60.0
            self._guard_window.append(now)
            while self._guard_window and now - self._guard_window[0] > window_seconds:
                self._guard_window.popleft()
            turns = len(self._guard_window)
            if turns < max(1, int(cfg.guard_round_trips)):
                self.ctx.logger.warning(
                    "%s 环路守卫：窗口内收到对端 %s 第 %s 条私聊消息（阈值 %s），继续观察",
                    LOG_TAG, sender, turns, cfg.guard_round_trips,
                )
                return
            cooldown = max(1, int(cfg.guard_cooldown_minutes)) * 60.0
            if now - self._last_guard_fire < cooldown:
                return
            self._last_guard_fire = now
            self.ctx.logger.error(
                "%s 🚨 环路守卫触发：%s 分钟内收到对端 %s 的私聊消息 %s 条（阈值 %s），"
                "判定 bot↔bot 环路风险，立即执行熔断",
                LOG_TAG, cfg.guard_window_minutes, sender, turns, cfg.guard_round_trips,
            )
            summary = await self._do_freeze(
                f"环路守卫自动触发（{cfg.guard_window_minutes} 分钟窗口内对端私聊 {turns} 条 ≥ 阈值 "
                f"{cfg.guard_round_trips}）"
            )
            self._guard_window.clear()
            self.ctx.logger.error("%s 🚨 %s", LOG_TAG, summary)
        except Exception as exc:  # OBSERVE 钩子绝不影响主链路
            self.ctx.logger.warning("%s 环路守卫异常（已忽略）：%s", LOG_TAG, exc)

    # ==================================================================
    # /h_state
    # ==================================================================
    def _target_data_dir(self) -> Path:
        """被测插件数据目录 <data>/plugins/<plugin_id>/（只读、不碰宿主目录）。

        路径来自 ctx.paths.data_dir 的父目录 + 白名单化的插件 id；
        §8.14 的“不要绕出隔离目录”针对写路径，本处为只读快照且 id 经过正则白名单。
        """
        cfg = self.config.harness
        plugin_id = str(cfg.freeze_target_plugin).strip()
        if not _SAFE_ID_RE.fullmatch(plugin_id):
            raise HarnessError(f"非法插件 id：{plugin_id!r}（拒绝拼路径）")
        root = Path(self.ctx.paths.data_dir).parent
        target = root / plugin_id
        if target.parent != root:
            raise HarnessError(f"目标目录越界：{target}")
        return target

    @staticmethod
    def _read_json(path: Path) -> Tuple[Any, str]:
        """读 JSON：返回 (数据, 错误文本)；不存在/损坏均不抛异常。"""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return json.load(handle), ""
        except FileNotFoundError:
            return None, "不可用（文件不存在）"
        except Exception as exc:
            return None, f"不可用（解析失败：{exc}）"

    @staticmethod
    def _summarize_emotion(data: Any) -> str:
        if not isinstance(data, dict):
            return "不可用（顶层不是对象）"
        vad = data.get("vad_raw") if isinstance(data.get("vad_raw"), dict) else {}
        log = data.get("emotion_log") if isinstance(data.get("emotion_log"), list) else []
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        return (
            f"VAD(V={vad.get('valence')}, A={vad.get('arousal')}, D={vad.get('dominance')})"
            f" last_update={data.get('last_update')}"
            f" 事件数={len(log)} 已初始化={params.get('initialized')}"
        )

    @staticmethod
    def _summarize_affection(data: Any) -> str:
        if not isinstance(data, dict):
            return "不可用（顶层不是对象）"
        return (
            f"score={data.get('score')} baseline={data.get('baseline')} tier={data.get('tier')}"
            f" version={data.get('version')}"
            f" 日计数字段={sorted(k for k in data.keys() if 'date' in str(k) or 'count' in str(k))[:6]}"
        )

    @staticmethod
    def _summarize_qzone(data: Any) -> str:
        if not isinstance(data, dict):
            return "不可用（顶层不是对象）"
        posted = data.get("posted_today") if isinstance(data.get("posted_today"), dict) else {}
        baseline = data.get("comment_baseline") if isinstance(data.get("comment_baseline"), dict) else {}
        replied = data.get("replied_comment_ids") if isinstance(data.get("replied_comment_ids"), list) else []
        processed = data.get("processed_list") if isinstance(data.get("processed_list"), dict) else {}
        return (
            f"今日发帖={posted.get('count')}（date={posted.get('date')}）"
            f" 评论基线={len(baseline)} 已回评={len(replied)} 处理清单={len(processed)}"
        )

    def _decision_tail(self, target_dir: Path, limit: int) -> List[str]:
        """读 decision_logs/decisions_*.jsonl 尾部 N 条（新→旧取，返回旧→新）。"""
        log_dir = target_dir / "decision_logs"
        if not log_dir.is_dir():
            return ["不可用（decision_logs 目录不存在）"]
        files = sorted(log_dir.glob("decisions_*.jsonl"))
        if not files:
            return ["不可用（无 decisions_*.jsonl）"]
        lines: List[str] = []
        for path in reversed(files):
            try:
                content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                lines.append(f"{path.name}: 读取失败（{exc}）")
                continue
            for line in reversed(content):
                if len(lines) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(f"{path.name}: {_fmt(json.loads(line), 320)}")
                except Exception:
                    lines.append(f"{path.name}: {line[:320]}")
            if len(lines) >= limit:
                break
        return list(reversed(lines))

    @Command("h_state", description="进程内读取被测插件状态快照 + decision_logs 尾部", pattern=r"(?<!\S)/h_state(?:\s+(?P<args>.*))?$")
    async def cmd_h_state(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        args = str((kwargs.get("matched_groups") or {}).get("args") or "").strip()
        limit = self.config.harness.state_tail_lines
        if args:
            try:
                limit = max(1, min(500, int(args.split()[0])))
            except ValueError:
                return self._finish(False, f"/h_state 参数非法：{args!r}（应为尾部条数整数）")
        ok, text, _ = await self._core_state(limit)   # 与文件通道 state 动作同一核心
        return self._finish(ok, text)

    async def _core_state(self, limit: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """状态快照核心（``/h_state`` 命令与文件通道 ``state`` 动作共用）。"""
        try:
            target_dir = self._target_data_dir()
        except HarnessError as exc:
            return False, f"状态读取失败：{exc}", None

        lines = [
            f"被测插件数据目录：{target_dir}（存在={target_dir.is_dir()}）",
            "== 状态文件摘要（只读，缺失即不可用）==",
        ]
        for filename, summarizer in (
            ("emotion_state.json", self._summarize_emotion),
            ("affection_memory.json", self._summarize_affection),
            ("qzone_state.json", self._summarize_qzone),
        ):
            data, err = self._read_json(target_dir / filename)
            lines.append(f"- {filename}: {err or summarizer(data)}")
        lines.append(f"== decision_logs 尾部 {limit} 条（旧→新）==")
        lines.extend(self._decision_tail(target_dir, limit))
        lines.append("提示：文件读取受写盘延迟影响；进程内权威值请对照被测插件 /cateye_test_* 状态命令。")
        return True, "\n".join(lines), {"target_dir": str(target_dir), "tail_lines": limit}

    # ==================================================================
    # /h_wait_reload
    # ==================================================================
    @Command("h_wait_reload", description="轮询等待被测插件热重载到指定版本", pattern=r"(?<!\S)/h_wait_reload(?:\s+(?P<args>.*))?$")
    async def cmd_h_wait_reload(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        args = str((kwargs.get("matched_groups") or {}).get("args") or "").strip()
        parts = args.split()
        if not parts:
            return self._finish(False, "用法：/h_wait_reload <期望版本> [期望组件数] [超时秒]")
        expected_version = parts[0]
        expected_count = 0
        timeout = float(self.config.harness.reload_timeout_seconds)
        extras = parts[1:]
        if extras:
            try:
                expected_count = int(extras[0])
            except ValueError:
                return self._finish(False, f"期望组件数非法：{extras[0]!r}")
        if len(extras) > 1:
            try:
                timeout = float(extras[1])
            except ValueError:
                return self._finish(False, f"超时秒数非法：{extras[1]!r}")

        plugin_id = self.config.harness.freeze_target_plugin
        poll = max(1.0, float(self.config.harness.reload_poll_seconds))
        deadline = time.monotonic() + max(5.0, timeout)
        last_seen = "尚未查询"
        attempts = 0
        while True:
            attempts += 1
            try:
                plugins = await self._get_all_plugins()
            except HarnessError as exc:
                last_seen = f"查询失败：{exc}"
                plugins = {}
            info = plugins.get(plugin_id)
            if isinstance(info, dict):
                version = str(info.get("version") or "")
                count = len(info.get("components") or [])
                last_seen = f"version={version} 组件数={count}"
                version_ok = version == expected_version
                count_ok = expected_count <= 0 or count == expected_count
                if version_ok and count_ok:
                    return self._finish(
                        True,
                        f"热重载完成（第 {attempts} 次轮询）：{plugin_id} {last_seen}"
                        f"（期望 version={expected_version}"
                        + (f", 组件数={expected_count}" if expected_count > 0 else "")
                        + "）",
                    )
            else:
                last_seen = f"未装载（已装载：{', '.join(sorted(plugins)[:8]) or '无'}）"
            if time.monotonic() >= deadline:
                return self._finish(
                    False,
                    f"热重载等待超时（{timeout:.0f}s，轮询 {attempts} 次）：{plugin_id} 最后观测 {last_seen}，"
                    f"期望 version={expected_version}"
                    + (f", 组件数={expected_count}" if expected_count > 0 else ""),
                )
            await asyncio.sleep(poll)

    # ==================================================================
    # /h_api
    # ==================================================================
    @Command("h_api", description="调用被测插件公开 API（集成断言）", pattern=r"(?<!\S)/h_api(?:\s+(?P<args>.*))?$")
    async def cmd_h_api(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        args = str((kwargs.get("matched_groups") or {}).get("args") or "").strip()
        params = _kv_args(args)
        positional = str(params.pop("_positional", "") or "").strip()
        api_name = positional.split()[0] if positional else ""
        if not api_name:
            return self._finish(
                False,
                "用法：/h_api <api_name> [k=v ...]；例：/h_api get_current_activity chat_id=global "
                "或 /h_api companion_get_affection_level（k=v 值按字符串/整数自动转换）",
            )
        call_kwargs = {k: _coerce_value(v) for k, v in params.items()}
        try:
            resp = await self.ctx.api.call(api_name, **call_kwargs)
        except Exception as exc:
            return self._finish(False, f"API 调用异常（{api_name}）：{exc}")
        # §8.0：成功=目标 API 原始返回值；失败={"success": False, "error": ...}
        if isinstance(resp, dict) and resp.get("success") is False:
            return self._finish(False, f"API 调用失败（{api_name}）：{resp.get('error')}")
        return self._finish(True, f"API {api_name} 返回：{_fmt(resp)}")

    # ==================================================================
    # /h_models
    # ==================================================================
    @Command("h_models", description="列举可用模型任务名（只列不调）", pattern=r"(?<!\S)/h_models\s*$")
    async def cmd_h_models(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        try:
            models = await self.ctx.llm.get_available_models()
        except Exception as exc:
            return self._finish(False, f"get_available_models 调用异常：{exc}")
        if isinstance(models, dict) and models.get("success") is False:
            return self._finish(False, f"get_available_models 失败：{models.get('error')}")
        names = [str(x) for x in models] if isinstance(models, (list, tuple, set)) else []
        if not names:
            return self._finish(False, f"未取到任务名（返回：{_fmt(models, 400)}）")
        return self._finish(
            True,
            f"可用模型任务名 {len(names)} 个（只列不调；请核对 [judge]/评估任务指向便宜非思考任务）：\n  "
            + "\n  ".join(sorted(names)),
        )

    # ==================================================================
    # /h_say
    # ==================================================================
    @Command("h_say", description="直发一条消息到测试群/测试私聊（唯一主动发送入口）", pattern=r"(?<!\S)/h_say\s+(?P<body>.+)$")
    async def cmd_h_say(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        denied = await self._prepare_command(kwargs)
        if denied is not None:
            return denied
        # 注意：kwargs["text"] 是命中的 processed_plain_text（整条命令），正文取命名捕获组
        raw = str((kwargs.get("matched_groups") or {}).get("body") or "").strip()
        cfg = self.config.harness
        # 形如 "/h_say private 文本" / "/h_say group 文本"；缺省发主测试群
        target_kind = "group"
        body = raw
        for prefix, kind in (("private ", "private"), ("group ", "group")):
            if raw.lower().startswith(prefix):
                target_kind = kind
                body = raw[len(prefix):].strip()
                break
        if not body:
            return self._finish(False, "用法：/h_say [group|private] <文本>（默认 group=主测试群）")

        try:
            if target_kind == "group":
                await self._assert_say_target("group", str(cfg.test_group_id))
                stream_id = await self._resolve_stream("group", str(cfg.test_group_id))
            else:
                await self._assert_say_target("private", str(cfg.test_private_qq))
                stream_id = await self._resolve_stream("private", str(cfg.test_private_qq))
            if not stream_id:
                return self._finish(False, f"/h_say 失败：未解析到 {target_kind} stream_id（会话可能尚未创建）")
            sent = await self.ctx.send.text(body, stream_id)
        except HarnessError as exc:
            return self._finish(False, f"/h_say 失败：{exc}")
        except Exception as exc:
            return self._finish(False, f"/h_say 异常：{exc}")
        return self._finish(
            bool(sent),
            f"/h_say {'成功' if sent else '被拒绝'} → {target_kind}"
            f"({'群' + str(cfg.test_group_id) if target_kind == 'group' else '私聊' + str(cfg.test_private_qq)})"
            f" stream={stream_id} text={body!r}",
        )

    async def _assert_say_target(self, kind: str, target_id: str) -> None:
        """/h_say 目标白名单：仅测试群 100000003 / 测试私聊 100000002（§四）。"""
        cfg = self.config.harness
        if kind == "group":
            allowed = {str(cfg.test_group_id)} | {str(x) for x in cfg.extra_allowed_group_ids}
        else:
            allowed = {str(cfg.test_private_qq)} | {str(x) for x in cfg.extra_allowed_private_qqs}
        if target_id not in allowed:
            self.ctx.logger.warning("%s /h_say 拒绝：目标 %s:%s 不在白名单", LOG_TAG, kind, target_id)
            raise HarnessError(
                f"目标 {kind}:{target_id} 不在白名单（仅 {', '.join(sorted(allowed))}）"
            )

    async def _resolve_stream(self, kind: str, target_id: str) -> str:
        """解析目标会话的 stream_id；不存在则 open_session 创建。"""
        if kind == "group":
            payload = await self.ctx.chat.get_stream_by_group_id(group_id=target_id)
        else:
            payload = await self.ctx.chat.get_stream_by_user_id(user_id=target_id)
        stream_id = _extract_stream_id(payload)
        if stream_id:
            return stream_id
        if kind == "group":
            created = await self.ctx.chat.open_session(platform=PLATFORM, chat_type="group", group_id=target_id)
        else:
            created = await self.ctx.chat.open_session(platform=PLATFORM, chat_type="private", user_id=target_id)
        return _extract_stream_id(created)


# ==================================================================
    # 后台文件任务通道（v0.1.1）：tasks/*.json → 核心函数 → results/<id>.json → done/
    # ==================================================================
    # 安全契约（与 §一 红线同级，改动前先读）：
    # 1) 动作白名单 TASK_ACTIONS 之外一律拒绝 + 错误回执；分发用**字面量 dict**，
    #    不存在任何由任务内容驱动的属性查找/动态导入；
    # 2) 任务内容只当纯数据：仅 .get()/str()/int()/float() 取值，绝不做动态代码执行/
    #    动态导入/反序列化可执行对象；inject 完整复用命令的目标白名单与 sender 黑名单；
    # 3) 回执文件名经白名单化（防路径穿越/Windows 保留名），任务文件一律归档不留死文件。

    def _channel_root(self) -> Optional[Path]:
        """**本插件自身**的数据目录（与 cateye_test 同款：ctx.paths.data_dir）。

        注意与 ``_target_data_dir()``（被测插件目录）区分：任务/回执读写自己的目录。
        不可用（未注入/非路径）时返回 None，通道自动不启动。
        """
        data_dir = getattr(getattr(self.ctx, "paths", None), "data_dir", None)
        if data_dir is None or str(data_dir).strip() == "":
            return None
        try:
            return Path(data_dir)
        except Exception:
            return None

    def _start_file_channel(self) -> None:
        """启动后台轮询任务（幂等）：插件停用或数据目录不可用时不上报、不启动。"""
        if not self.config.plugin.enabled:
            return
        current = getattr(self, "_file_tasks", None)
        if current is not None and not current.done():
            return
        root = self._channel_root()
        if root is None:
            self.ctx.logger.warning(
                "%s 文件任务通道未启动：ctx.paths.data_dir 不可用（命令通道不受影响）", LOG_TAG
            )
            return
        try:
            (root / TASKS_DIRNAME / DONE_DIRNAME).mkdir(parents=True, exist_ok=True)
            (root / RESULTS_DIRNAME).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.ctx.logger.error("%s 文件任务通道未启动：数据目录不可写（%s）", LOG_TAG, exc)
            return
        self._file_tasks = asyncio.create_task(self._file_channel_loop())
        self.ctx.logger.info(
            "%s 文件任务通道已启动：每 %ss 轮询 %s；回执 %s/<id>.json；归档 %s",
            LOG_TAG, TASK_POLL_SECONDS, root / TASKS_DIRNAME / "*.json",
            root / RESULTS_DIRNAME, root / TASKS_DIRNAME / DONE_DIRNAME,
        )

    async def _stop_file_channel(self) -> None:
        """停止后台轮询任务（幂等；卸载/停用/热重载共用）。"""
        task = getattr(self, "_file_tasks", None)
        self._file_tasks = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.ctx.logger.warning("%s 文件任务通道停止时异常（已忽略）：%s", LOG_TAG, exc)

    async def _file_channel_loop(self) -> None:
        """后台轮询循环：每 TASK_POLL_SECONDS 消费一轮 tasks/*.json。

        异常隔离：单轮任何异常只记 WARN 并继续下一轮，**绝不退出**（仅卸载时被取消）。
        """
        while True:
            try:
                await asyncio.sleep(TASK_POLL_SECONDS)
                await self._poll_tasks_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.warning("%s 文件任务通道本轮异常（已隔离，继续轮询）：%s", LOG_TAG, exc)

    async def _poll_tasks_once(self) -> List[str]:
        """扫描一轮：按文件名排序逐个消费 tasks/*.json；返回本轮取走的文件名。"""
        root = self._channel_root()
        if root is None:
            return []
        tasks_dir = root / TASKS_DIRNAME
        try:
            paths = sorted(tasks_dir.glob("*.json"), key=lambda p: p.name)
        except OSError as exc:
            self.ctx.logger.warning("%s 文件任务通道扫描 %s 失败：%s", LOG_TAG, tasks_dir, exc)
            return []
        consumed: List[str] = []
        for path in paths:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            try:
                handled = await self._consume_task_file(path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # 单文件异常不拖垮整轮
                self.ctx.logger.warning("%s 任务文件 %s 消费异常（已隔离）：%s", LOG_TAG, path.name, exc)
                try:
                    self._archive_task_file(path)
                except Exception as move_exc:
                    self.ctx.logger.error("%s 任务文件 %s 归档失败：%s", LOG_TAG, path.name, move_exc)
                handled = True
            if handled:
                consumed.append(path.name)
        return consumed

    async def _consume_task_file(self, path: Path) -> bool:
        """消费单个任务文件：读 → 分发 → 回执 → 归档（坏 JSON/未知动作同样归档+错误回执）。"""
        stem = path.stem
        data: Any = None
        parse_error = ""
        for attempt in (1, 2):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                parse_error = ""
                break
            except FileNotFoundError:
                self.ctx.logger.warning("%s 任务文件 %s 读取前已被移走，跳过", LOG_TAG, path.name)
                return False
            except Exception as exc:
                parse_error = f"任务文件不可解析（{type(exc).__name__}: {exc}）"
                if attempt == 1:
                    await asyncio.sleep(0.2)   # 可能是写入中/被占用：短暂等待后重试一次
        if not isinstance(data, dict):
            if data is not None:
                parse_error = f"任务文件顶层必须是对象（实际 {type(data).__name__}）"
            receipt = self._build_receipt(stem, "", "error", parse_error or "任务文件为空或不可解析", None)
            await self._emit_receipt(receipt, stem)
            self._archive_task_file(path)
            return True
        receipt = await self._dispatch_task(data, stem)
        await self._emit_receipt(receipt, stem)
        self._archive_task_file(path)
        return True

    async def _dispatch_task(self, task: Dict[str, Any], stem: str) -> Dict[str, Any]:
        """把任务数据分发到白名单动作；返回回执 dict（除取消外不抛异常）。"""
        task_id = str(task.get("id") or "").strip() or stem
        action = str(task.get("action") or "").strip().lower()
        params = task.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._build_receipt(task_id, action, "error",
                                       f"params 必须是对象（实际 {type(params).__name__}）", None)
        if not self.config.plugin.enabled:
            return self._build_receipt(task_id, action, "error",
                                       "插件未启用（plugin.enabled=false），拒绝执行", params)
        if action not in TASK_ACTIONS:
            self.ctx.logger.warning(
                "%s 文件任务 %s 动作 %r 不在白名单（%s），拒绝执行",
                LOG_TAG, task_id, action, "/".join(TASK_ACTIONS),
            )
            return self._build_receipt(
                task_id, action, "error",
                f"动作 {action!r} 不在白名单（仅 {'/'.join(TASK_ACTIONS)}）；"
                "任务内容一律只作数据，不执行任何代码",
                params,
            )
        handlers = {                       # 字面量映射：绝不用任务内容做 getattr/导入
            "inject": self._task_inject,
            "seq": self._task_seq,
            "state": self._task_state,
            "freeze": self._task_freeze,
            "resume": self._task_resume,
        }
        try:
            ok, summary, extra = await handlers[action](params)
        except HarnessError as exc:
            ok, summary, extra = False, str(exc), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.exception("%s 文件任务 %s（%s）执行异常", LOG_TAG, task_id, action)
            ok, summary, extra = False, f"执行异常：{type(exc).__name__}: {exc}", None
        return self._build_receipt(task_id, action, "ok" if ok else "error", summary, params, extra)

    # ---- 文件通道动作：逐一动到命令同款核心函数（行为一致，无第二套实现）----
    async def _task_inject(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        ok, summary, message = await self._core_inject(params)   # = /h_inject
        if not ok or not isinstance(message, dict):
            return ok, summary, None
        info = message.get("message_info") or {}
        return True, summary, {
            "message_id": message.get("message_id"),
            "target": (info.get("additional_config") or {}).get("harness_target"),
            "sender": (info.get("user_info") or {}).get("user_id"),
            "is_notify": bool(message.get("is_notify")),
        }

    async def _task_seq(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        name = str(params.get("name") or params.get("scene") or "").strip()
        if not name:
            raise HarnessError("seq 缺少场景名（params.name）")
        run = _as_bool(params.get("run"), True)   # 缺省执行；run=false 等价 dry-run 预览
        scene, lookup_error = self._core_seq_lookup(name)   # = /h_seq 的查找与校验
        if scene is None:
            return False, lookup_error, {"scene": name, "steps": []}
        if not run:
            steps = self._scene_plan_steps(scene)
            extra = {"scene": name, "dry_run": True, "steps": steps, "total": len(steps),
                     "completed": 0, "aborted_at": None,
                     "assertions": list(scene.get("assertions") or []),
                     "manual_notes": list(scene.get("manual_notes") or [])}
            return True, "【dry-run】场景 {} 未执行任何注入（run=false）：\n{}".format(
                name, "\n".join(self._scene_plan_lines(name, scene))
            ), extra
        return await self._core_seq_execute(name, scene)   # = /h_seq <名> run

    async def _task_state(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        return await self._core_state(self._task_limit(params))   # = /h_state

    def _task_limit(self, params: Dict[str, Any]) -> int:
        """state 动作的尾部条数：缺省取配置值，显式给值时与命令同口径夹到 1-500。"""
        raw = params.get("limit")
        if raw is None or str(raw).strip() == "":
            return self.config.harness.state_tail_lines
        try:
            return max(1, min(500, int(str(raw).strip())))
        except (TypeError, ValueError):
            raise HarnessError(f"limit 参数非法：{raw!r}（应为 1-500 的整数）")

    async def _task_freeze(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        reason = str(params.get("reason") or "").strip()[:200] or "文件任务通道 freeze"
        summary = await self._do_freeze(reason)   # = /h_freeze
        return (not summary.startswith("熔断未执行")), summary, None

    async def _task_resume(self, params: Dict[str, Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        del params
        summary = await self._do_resume()   # = /h_resume
        return (not summary.startswith("恢复未执行")), summary, None

    # ---- 回执与归档 ----
    @staticmethod
    def _build_receipt(task_id: str, action: str, status: str, summary: str,
                       params: Optional[Dict[str, Any]] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """回执：{"id","action","status","summary","ts",...} + 动作摘要字段。"""
        receipt: Dict[str, Any] = {
            "id": task_id,
            "action": action,
            "status": status,
            "summary": str(summary),
            "ts": time.time(),
            "ts_iso": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if isinstance(params, dict) and params:
            receipt["params"] = params
        if extra:
            receipt.update(extra)
        return receipt

    async def _emit_receipt(self, receipt: Dict[str, Any], stem: str) -> None:
        """回执原子落盘 results/<id>.json（先写 .tmp 再替换；失败重试一次不中断通道）。"""
        root = self._channel_root()
        if root is None:
            self.ctx.logger.error("%s 回执无法写入（数据目录不可用）：%s", LOG_TAG, _fmt(receipt))
            return
        results_dir = root / RESULTS_DIRNAME
        name = self._receipt_name(str(receipt.get("id") or ""), stem)
        target = results_dir / f"{name}.json"
        payload = json.dumps(receipt, ensure_ascii=False, indent=2, default=str)
        last_error = ""
        for attempt in (1, 2):
            try:
                results_dir.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(target)
                self.ctx.logger.info(
                    "%s 📄 任务 %s（%s）%s → %s",
                    LOG_TAG, receipt.get("id"), receipt.get("action"),
                    receipt.get("status"), target,
                )
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    await asyncio.sleep(0.2)
        self.ctx.logger.error(
            "%s 回执写入失败（%s）：%s；回执内容=%s", LOG_TAG, target, last_error, _fmt(receipt)
        )

    @staticmethod
    def _receipt_name(task_id: str, stem: str) -> str:
        """回执文件名：id 安全时用 id，否则退回文件名 stem（防穿越/非法字符/保留名）。"""

        def _ok(candidate: str) -> bool:
            if not candidate or not _SAFE_ID_RE.fullmatch(candidate):
                return False
            if candidate in {".", ".."}:
                return False
            return candidate.split(".")[0].upper() not in _WINDOWS_RESERVED

        if _ok(task_id):
            return task_id[:120]
        if _ok(stem):
            return stem[:120]
        cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", stem or "").strip("._")[:120]
        return cleaned if _ok(cleaned) else f"task-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _archive_task_file(path: Path) -> Optional[Path]:
        """任务文件移入 tasks/done/；同名冲突加时间戳后缀，不覆盖历史归档。"""
        done_dir = path.parent / DONE_DIRNAME
        done_dir.mkdir(parents=True, exist_ok=True)
        target = done_dir / path.name
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            target = done_dir / f"{path.stem}.{stamp}{path.suffix}"
        path.replace(target)
        return target


def create_plugin() -> MaiBotPlugin:
    """Runner 加载入口。"""
    return CateyeHarnessPlugin()