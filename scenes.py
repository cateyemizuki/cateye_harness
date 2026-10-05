"""cateye_harness — 场景库（TEST_PROCEDURE §六 里程碑 ↔ 测试场景映射）。

纯数据模块：不 import maibot_sdk、不持有 ctx；插件侧只做「解析/校验/dry-run/步进执行」。

每个场景：
- ``milestone``     对应里程碑（§六 左列）；
- ``goal``          一句话目的；
- ``manual_notes``  仅宿主/真人侧能做的预置（L4 时间搬运、旧 JSON 放置、配置收紧等），
                    夹具不代做、执行时原样打印提醒；
- ``assertions``    §六 右列关键断言，dry-run 与执行结束时打印，供 agent 逐条核对；
- ``steps``         步进序列，三类：
    ``{"type": "inject", ...注入参数, "desc": ...}``  复用 /h_inject 注入核心；
    ``{"type": "sleep", "seconds": N, "desc": ...}``   asyncio.sleep 间隔；
    ``{"type": "note", "text": ...}``                  只打印不回显。

注入参数与 /h_inject 完全同构：group_id / private_qq 二选一，sender_id、text、
is_at、is_notify、reply_to_msg_id 可选。
"""

from __future__ import annotations

from typing import Any, Dict, List

# §〇 环境事实（与 config.py 默认值一致；场景内显式写出，便于 dry-run 直接读懂）
GROUP = 100000003        # 主测试群
PRIVATE = 100000002     # 主测试私聊（= 默认注入 sender 本人）
SENDER = 100000002      # 注入 sender 默认值


def _inject(text: str, *, group_id: int | None = None, private_qq: int | None = None,
            is_at: bool = False, is_notify: bool = False, reply_to_msg_id: str = "",
            sender_id: int = SENDER, desc: str = "") -> Dict[str, Any]:
    """构造一个 inject 步进（group_id / private_qq 二选一）。"""
    step: Dict[str, Any] = {"type": "inject", "text": text}
    if group_id is not None:
        step["group_id"] = group_id
    if private_qq is not None:
        step["private_qq"] = private_qq
    if sender_id != SENDER:
        step["sender_id"] = sender_id
    if is_at:
        step["is_at"] = True
    if is_notify:
        step["is_notify"] = True
    if reply_to_msg_id:
        step["reply_to_msg_id"] = reply_to_msg_id
    if desc:
        step["desc"] = desc
    return step


def _sleep(seconds: float, desc: str = "") -> Dict[str, Any]:
    step: Dict[str, Any] = {"type": "sleep", "seconds": seconds}
    if desc:
        step["desc"] = desc
    return step


def _note(text: str) -> Dict[str, Any]:
    return {"type": "note", "text": text}


SCENES: Dict[str, Dict[str, Any]] = {
    # ------------------------------------------------------------------
    "bus_check": {
        "milestone": "M0 事件总线",
        "goal": "注入一问一答，验证事件总线写入与主动触发预算",
        "manual_notes": [
            "前置：/h_wait_reload <版本> 确认 cateye-suite 已装载；decision_logs 无上轮残留。",
            "前置：patrol.daily_max_speak ≤ 3（§一 Token 保护三件套）。",
            (
                "若本轮要验“主动触发写入 proactive.used + 全渠道全局上限”，"
                "需把 patrol 检查间隔临时调小（[patrol] check_interval_minutes，观察后恢复）。"
            ),
        ],
        "assertions": [
            "decision_logs 出现 bus:relation.* / bus:schedule.* 事件条目；",
            "主动触发后写入 proactive.used；",
            "同日全渠道触发不超全局上限（decision_logs 记 skip 原因）。",
        ],
        "steps": [
            _note("步骤 1/4：群聊一问一答（bot 回复会真实发到测试群）"),
            _inject("在忙吗？今天有什么安排", group_id=GROUP,
                    desc="群聊注入第 1 问 → 观察回复与 decision_logs"),
            _sleep(20, "等 bot 走完回复决策链"),
            _note("核对：decision_logs 出现 bus:schedule.* / bus:relation.* 订阅事件"),
            _inject("那晚上呢", group_id=GROUP, desc="群聊注入第 2 问 → 验证连续轮次"),
            _sleep(20, "等回复与事件写入"),
            _note("核对：主动触发条目写入 proactive.used；当日全局触发上限未被突破"),
        ],
    },
    # ------------------------------------------------------------------
    "affection_basic": {
        "milestone": "M1 好感 v2",
        "goal": "连续 10 轮私聊注入，验证 score 上涨且不越日累计上限",
        "manual_notes": [
            (
                "v1 迁移子项：装载前在 data/plugins/github.cateye.cateye-suite/affection_memory.json "
                "放旧格式 JSON（无 version 字段、含 affection_level），再触发热重载，观察迁移日志与写回。"
            ),
            (
                "档位迟滞子项（升档需越界 +3 且满 24h）：属 L4，需临时把 [affection] 周期/迟滞参数 "
                "搬到几分钟后，观察后恢复——夹具不代做。"
            ),
            "前置：确认 target_qq = 100000002（§一）。",
        ],
        "assertions": [
            "10 轮后 score 上涨，但涨幅 ≤ 日累计上限（刷 100 条也 +1.0 封顶）；",
            "decision_logs 记录增量（裁剪前/后 + multiplier）；",
            "affection_memory.json 出现 v2 字段：score / baseline / tier。",
        ],
        "steps": [
            _note("步骤 1/12：10 轮私聊注入（每轮间隔 8s，bot 每轮会真实回复到主测试私聊）"),
            _inject("今天过得怎么样呀", private_qq=PRIVATE, desc="第 1 轮"),
            _sleep(8, "第 1 轮等待"),
            _inject("刚刚在写代码，有点累", private_qq=PRIVATE, desc="第 2 轮"),
            _sleep(8, "第 2 轮等待"),
            _inject("你晚上一般做什么", private_qq=PRIVATE, desc="第 3 轮"),
            _sleep(8, "第 3 轮等待"),
            _inject("我最近在学做饭", private_qq=PRIVATE, desc="第 4 轮"),
            _sleep(8, "第 4 轮等待"),
            _inject("今天天气不错", private_qq=PRIVATE, desc="第 5 轮"),
            _sleep(8, "第 5 轮等待"),
            _inject("有点想你了", private_qq=PRIVATE, desc="第 6 轮"),
            _sleep(8, "第 6 轮等待"),
            _inject("晚饭吃了什么", private_qq=PRIVATE, desc="第 7 轮"),
            _sleep(8, "第 7 轮等待"),
            _inject("明天要早起", private_qq=PRIVATE, desc="第 8 轮"),
            _sleep(8, "第 8 轮等待"),
            _inject("陪我聊会儿天吧", private_qq=PRIVATE, desc="第 9 轮"),
            _sleep(8, "第 9 轮等待"),
            _inject("晚安，早点休息", private_qq=PRIVATE, desc="第 10 轮"),
            _sleep(15, "最后一轮等待落盘"),
            _note("结束：用 /h_state 读 affection score/baseline/tier，核对日累计上限与 decision_logs 增量"),
        ],
    },
    # ------------------------------------------------------------------
    "affection_review": {
        "milestone": "M2 裁判/日结",
        "goal": "L4 时间搬运触发会话采样裁判与好感日结",
        "manual_notes": [
            (
                "L4 预置（仅宿主侧可做）：把 [schedule] auto_generate_time/wake_time/sleep_time 或 "
                "[emotion]/[affection] 周期参数临时搬到“几分钟后”，热重载生效，观察后恢复。"
            ),
            "退化子项：把 [judge]/评估任务故意配错（指向不存在的任务名），验证纯规则退化不崩。",
            "前置：patrol.daily_max_speak ≤ 3；judge 任务指向便宜非思考模型（/h_models 核对）。",
        ],
        "assertions": [
            "会话采样裁判触发 ≤ 2 次/日；",
            "日结 JSON 合法（可被 json.load 解析、字段齐全）；",
            "impressions 滚动更新（旧条目被裁、新条目进入）；",
            "LLM 任务配错时走纯规则退化，不抛异常、不中断。",
        ],
        "steps": [
            _note("步骤 1/4：先制造可被采样的会话素材（私聊 2 轮）"),
            _inject("今天遇到一件挺开心的事", private_qq=PRIVATE, desc="采样素材 1"),
            _sleep(10, "等待"),
            _inject("不过也有点小烦恼", private_qq=PRIVATE, desc="采样素材 2"),
            _sleep(10, "等待"),
            _note("步骤 3/4：等 L4 时间锚点到达后，观察裁判触发与日结写盘"),
            _sleep(20, "给裁判/日结留出触发窗口（真实等待取决于 L4 搬移的分钟数）"),
            _note("结束：/h_state 读 affection_memory 与 decision_logs，核对裁判次数、日结 JSON、impressions 滚动"),
        ],
    },
    # ------------------------------------------------------------------
    "attribution": {
        "milestone": "M3 识别/倍率/沉默",
        "goal": "验证 target 加成与 is_target 倍率的识别口径",
        "manual_notes": [
            (
                "工具子项：群聊里让 bot 主动调用带 is_target 的工具（如活动/日程类），"
                "再核对 decision_logs 的 K 值——注入无法伪造工具调用，需真人或已有工具链路。"
            ),
            "is_target 垃圾值：由工具参数传入垃圾字符串，预期静默按 1.0（同属工具链路子项）。",
        ],
        "assertions": [
            "私聊注入 → target 加成生效，decision_logs 记 K=1+加成；",
            "群聊注入 → K=×1.0；",
            "群聊 + 工具 is_target=true → 加成生效；",
            "is_target 传垃圾值 → 静默按 1.0，不报错。",
        ],
        "steps": [
            _note("步骤 1/5：私聊注入（期望 K=1+加成）"),
            _inject("今天有点累，想和你说说话", private_qq=PRIVATE, desc="私聊 → target 加成"),
            _sleep(15, "等待回复与 decision_logs 写入"),
            _note("核对：decision_logs 该轮 K > 1.0（记 K=1+加成）"),
            _inject("大家晚上好呀", group_id=GROUP, desc="群聊 → 期望 ×1.0"),
            _sleep(15, "等待回复与 decision_logs 写入"),
            _note("核对：群聊该轮 K = 1.0；随后按 manual_notes 做「群聊 + 工具 is_target=true」子项"),
        ],
    },
    # ------------------------------------------------------------------
    "silence": {
        "milestone": "M3 识别/倍率/沉默",
        "goal": "is_notify 注入夯住 last_user_msg_time，验证沉默检测配额与间隔",
        "manual_notes": [
            "L4 预置：临时调小 [patrol]/[affection] 的 miss_hours（沉默判定阈值），观察后恢复。",
            "前置：patrol.daily_max_speak ≤ 3，避免静置期被主动发言打扰。",
        ],
        "assertions": [
            "silence_detected 每日 ≤ 2 次；",
            "两次 silence_detected 间隔 ≥ 4h；",
            "is_notify 注入只更新 last_user_msg_time，不触发回复（可先验证零回复）。",
        ],
        "steps": [
            _note("步骤 1/3：is_notify 注入最后一条消息（只入库，不触发回复）"),
            _inject("我下午要去开会，可能晚点回你", private_qq=PRIVATE, is_notify=True,
                    desc="is_notify 注入 → 只更新 last_user_msg_time"),
            _sleep(15, "核对：本群/私聊无新回复（is_notify 不触发回复链）"),
            _note("步骤 3/3：静置观察（真实时长取决于 L4 调小的 miss_hours）"),
            _sleep(20, "给沉默判定留出窗口"),
            _note("结束：decision_logs 核对 silence_detected 次数 ≤2/日、间隔 ≥4h"),
        ],
    },
    # ------------------------------------------------------------------
    "mood_effect": {
        "milestone": "M4 情绪驱动",
        "goal": "构造低效价 V<−0.6 状态，验证 qzone 跳过与 patrol 乘数、planner 情绪行",
        "manual_notes": [
            (
                "低情绪构造（仅宿主侧可做）：改 data/plugins/github.cateye.cateye-suite/emotion_state.json "
                "的 vad_raw.valence < -0.6（或经情绪初始化/命令构造），再触发热重载。"
            ),
            "qzone 子项需 qzone 启用且当日有发帖计划；patrol 乘数需 patrol 检查间隔已临时调小。",
        ],
        "assertions": [
            "qzone 跳过发帖（decision_logs 记情绪原因）；",
            "patrol 概率乘数生效（decision_logs 记录乘后概率）；",
            "planner 注入出现情绪行（【当前情绪底色】），经推理过程页/请求快照核对。",
        ],
        "steps": [
            _note("步骤 1/3：确认 emotion_state.json 的 vad_raw.valence < −0.6（先按 manual_notes 构造）"),
            _inject("今晚想做点什么好呢", private_qq=PRIVATE, desc="触发一轮回复，观察情绪行与乘数"),
            _sleep(20, "等待回复决策链与 planner 请求生成"),
            _note("结束：查 planner 请求（推理过程页）是否含【当前情绪底色】；decision_logs 核对乘后概率与 qzone skip"),
        ],
    },
}


def scene_names() -> List[str]:
    """已登记场景名（稳定排序，供 /h_seq 无参列出）。"""
    return sorted(SCENES)


def get_scene(name: str) -> Dict[str, Any] | None:
    """按名取场景定义；不存在返回 None。"""
    return SCENES.get(str(name or "").strip())


def validate_scene(scene: Dict[str, Any]) -> List[str]:
    """校验场景定义结构，返回问题列表（空 = 合法）。"""
    problems: List[str] = []
    if not isinstance(scene, dict):
        return ["场景定义不是 dict"]
    steps = scene.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps 缺失或为空")
        return problems
    for idx, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            problems.append(f"第 {idx} 步不是 dict")
            continue
        kind = str(step.get("type") or "")
        if kind == "inject":
            has_group = step.get("group_id") is not None
            has_private = step.get("private_qq") is not None
            if has_group == has_private:
                problems.append(f"第 {idx} 步 inject 必须且只能指定 group_id 或 private_qq")
            if not str(step.get("text") or "").strip():
                problems.append(f"第 {idx} 步 inject 缺少 text")
        elif kind == "sleep":
            try:
                if float(step.get("seconds") or 0) <= 0:
                    problems.append(f"第 {idx} 步 sleep 秒数必须 > 0")
            except (TypeError, ValueError):
                problems.append(f"第 {idx} 步 sleep 秒数非法")
        elif kind == "note":
            if not str(step.get("text") or "").strip():
                problems.append(f"第 {idx} 步 note 缺少 text")
        else:
            problems.append(f"第 {idx} 步 type 非法：{kind!r}（只支持 inject/sleep/note）")
    return problems