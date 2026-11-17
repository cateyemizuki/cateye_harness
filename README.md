# cateye_harness

MaiBot 宿主只接受真实入站消息驱动。这个插件是开发/调试用的自测试夹具：它构造标准 `MessageDict`，经自带的 receive 网关注入宿主完整入站链，并提供里程碑场景步进、被测插件熔断/恢复、环路守卫、状态快照、适配器与模型探针，以及一个用文件遥控的远程任务通道。

它面向维护者和测试 agent，不是通用功能插件。

插件由四部分组成：

- 9 个 `/h_*` 斜杠命令；
- 1 个 receive 网关 `harness_inject_gateway`，只作注入载体；
- 1 个 `chat.receive.after_process` 钩子 `harness_loop_guard`，做环路计数与自动熔断；
- 1 个后台文件任务通道，轮询自身数据目录的 `tasks/*.json` 并写回执。

## 安装与启用

1. 把整个 `cateye_harness` 目录放进宿主插件目录（例如 `plugins/cateye_harness/`），宿主 Runner 通过 `create_plugin()` 加载。
2. 保持 `plugin.py`、`harness_config.py`、`scenes.py` 文件名不变。插件加载时会把自身目录插到 `sys.path` 首位，再以绝对导入引用后两个模块。
3. 运行期只依赖宿主提供的 `maibot_sdk`，无第三方依赖。manifest 声明：`id=github.cateye.cateye-harness`、`version=0.1.1`、`plugin_type=tool`、`dependencies=[]`，版本区间为 host 1.0.0~1.99.99、sdk 2.6.0~2.99.99。
4. 宿主会在插件目录生成 `config.toml`，分 `[plugin]` 与 `[harness]` 两段。`[plugin]` 段必须带 `config_version`；`[harness]` 全部字段有默认值，默认值即当前测试环境，不改配置也能用。
5. 保持 `plugin.enabled=true` 启动。启用时加载日志会出现 `[harness] 已加载...`，注入网关上报 ready，文件任务通道启动；关闭时组件仍注册，但网关不上报就绪、文件通道不启动，所有 `/h_*` 拒绝执行。

manifest 声明的宿主能力：`send.text`、`chat.get_stream_by_group_id`、`chat.get_stream_by_user_id`、`chat.open_session`、`component.get_all_plugins`、`component.enable`、`component.disable`、`api.call`、`llm.get_available_models`。

### 真实链路前置

`/h_inject`、`/h_seq ... run` 以及文件通道的 `inject`/`seq` 动作，执行前默认检查：

- 适配器插件 `maibot-team.snowluma-adapter` 已装载且未停用；
- 该插件含名为 `snowluma_gateway` 的 `MESSAGE_GATEWAY` 组件；
- 调用 `adapter.napcat.system.get_login_info` 成功，且返回的登录账号等于 `harness.self_id`。

任一不满足就明确报错、不注入。这里只验证真实链路是否在场；注入本身仍走本插件自己的 `harness_inject_gateway`，不借用 SnowLuma 的网关。适配器的黑白名单不会作用于夹具网关，目标白名单与 sender 黑名单由夹具自己执行。把 `harness.verify_adapter_ready` 设为 `false` 可跳过整段适配器检查。

### 操作对象与数据目录

- 熔断、恢复、状态读取、热重载等待默认都指向被测插件 `github.cateye.cateye-suite`，由 `harness.freeze_target_plugin` 决定。
- 文件任务通道读写本插件自己的数据目录（`ctx.paths.data_dir`，通常为 `data/plugins/github.cateye.cateye-harness/`），需要可写；不可写时只记 WARN/ERROR 且通道不启动，命令通道不受影响。
- `/h_state` 读取被测插件数据目录，默认即 `data/plugins/github.cateye.cateye-suite/`。

## 斜杠命令

### 通用规则

- 每条 `/h_*` 先检查 `plugin.enabled`，再检查调用者：本机控制台（`is_local_operator`）放行，或 `user_id` 在 `harness.command_allowed_users` 内，否则拒绝。
- 命令结果写入宿主日志（带 `[harness]` 前缀）；除 `/h_say` 外，插件不调用 `ctx.send.*`，不会主动把输出发到聊天里。
- 参数写法是 `key=value`，键名不区分大小写，值含空格时用双引号包住。命令按整行匹配，前面不能有非空白字符。
- 下文示例中的群号、QQ、API 名均为占位；目标号必须在配置白名单内，否则会被拒绝。

### /h_inject

```text
/h_inject group_id=<群号> text="<消息>" [sender_id=<QQ>] [sender_nickname=<昵称>] [is_at=1] [is_notify=1] [reply_to_msg_id=<消息id>] [group_name=<群名>]
/h_inject private_qq=<QQ> text="<消息>" [sender_id=<QQ>] [...]
```

| 参数 | 说明 |
| --- | --- |
| `group_id` / `private_qq` | 必须且只能给一个。群目标限 `test_group_id` 加 `extra_allowed_group_ids`；私聊目标限 `test_private_qq` 加 `extra_allowed_private_qqs` |
| `text` | 必填且非空；含空格用双引号包住 |
| `sender_id` | 缺省用 `sender_default`；命中 `blocked_senders` 直接拒绝 |
| `sender_nickname` | 缺省为 `测试用户<sender_id>` |
| `is_at` | 真值（`1`/`true`/`yes`/`on`/`y`）时追加 `at` 段指向 `self_id`，并把 `is_mentioned`、`is_at` 置真 |
| `is_notify` | 真值时置 `is_notify=true`，宿主只入库、不触发回复 |
| `reply_to_msg_id` | 追加 `reply` 段，并写入 `reply_to` |
| `group_name` | 群名，缺省 `harness-test-group-<群号>`；私聊忽略 |

构造出的消息平台为 `qq`，`additional_config` 带 `self_id`、`harness_injected=true`、`harness_target=<group|private>:<id>`；去除前导空白后以 `/` 开头时 `is_command` 为真。`message_id` 是合成的 19 位数字（9 开头），宿主 reply 工具可以把它当整数回填。

每次注入都会先跑真实链路前置检查。成功时日志记录消息 id、目标、sender、普通/通知类型和文本；失败时只报错，不注入。

```text
/h_inject group_id=123456 text="在忙吗？今天有什么安排"
/h_inject group_id=123456 text="你好 世界" is_at=1
/h_inject private_qq=10001 text="晚上好" is_notify=1
/h_inject private_qq=10001 text="接着说" reply_to_msg_id=9000000000000000001
```

### /h_seq

```text
/h_seq
/h_seq <场景名>
/h_seq <场景名> run
```

- 无参数：列出全部已登记场景（按名称排序），每个带里程碑、目的和步进条数。
- 只给场景名：dry-run。打印里程碑、目的、逐步的 inject/sleep/note 明细、`manual_notes` 与 `assertions`，并明确标注未执行任何注入。
- 名字后出现 `run`、`execute` 或 `--run`：先做注入前置检查，通过后把步进放到后台 asyncio 任务里执行，命令立即返回；每一步结果继续写宿主日志，走完后打印断言与人工预置提醒。已有场景在跑时拒绝启动新场景。
- 场景定义在执行前会校验结构；步进只支持 `inject`、`sleep`、`note` 三类，`inject` 参数与 `/h_inject` 同构。查不到或结构非法时直接报错，列出可用场景或具体问题。
- 场景执行串行（内部锁），文件通道的 `seq` 动作与命令通道共用同一执行器；插件卸载时后台场景会被取消。

| 场景 | 里程碑 | 目的 | 步进 |
| --- | --- | --- | --- |
| `bus_check` | M0 事件总线 | 注入一问一答，验证事件总线写入与主动触发预算 | 7 步（2 次注入，内置 sleep 合计 40s） |
| `affection_basic` | M1 好感 v2 | 连续 10 轮私聊注入，验证 score 上涨且不越日累计上限 | 22 步（10 次注入，内置 sleep 合计 87s） |
| `affection_review` | M2 裁判/日结 | 时间搬运后触发会话采样裁判与好感日结 | 8 步（2 次注入，内置 sleep 合计 40s） |
| `attribution` | M3 识别/倍率/沉默 | 验证 target 加成与 `is_target` 倍率的识别口径 | 7 步（2 次注入，内置 sleep 合计 30s） |
| `silence` | M3 识别/倍率/沉默 | `is_notify` 注入夯住 `last_user_msg_time`，验证沉默检测配额与间隔 | 6 步（1 次注入，内置 sleep 合计 35s） |
| `mood_effect` | M4 情绪驱动 | 构造低效价状态，验证 qzone 跳过、patrol 乘数与 planner 情绪行 | 4 步（1 次注入，内置 sleep 合计 20s） |

场景里的 `manual_notes` 是仅宿主/真人侧能做的预置（时间参数搬移、放置旧格式 JSON、改配置等），夹具不会代做，只在 dry-run 和成功走完时原样提醒。`assertions` 同样只打印，供逐条核对。

### /h_freeze 与 /h_resume

```text
/h_freeze
/h_resume
```

`/h_freeze` 在 `component.get_all_plugins` 里找到 `freeze_target_plugin`，停用其 `freeze_component_types` 指定类型的组件（大小写不敏感，默认 `tool`、`hook_handler`）。停用顺序是 `HOOK_HANDLER` 先于 `TOOL`，先断事件链再断工具面。已经处于停用状态的同类型组件会被记入待恢复列表，熔断摘要以 ERROR 级别在宿主日志显著标记。

`/h_resume` 优先按内存中的记录逆序恢复；内存里没有记录时，退化为启用目标插件的全部目标类型组件。因为冻结时记录了“本来就停用”的组件，恢复会把它们一并启用。

两个命令都只处理组件。`patrol`、`emotion` 这类后台 asyncio 循环不是组件，`/h_freeze` 停不掉；要停整个被测插件，改它的 `plugin.enabled=false`。

### /h_state

```text
/h_state [条数]
```

读取被测插件数据目录的磁盘快照：

- `emotion_state.json`：VAD、最后更新时间、事件数、是否初始化；
- `affection_memory.json`：score、baseline、tier、version 与日计数字段；
- `qzone_state.json`：今日发帖、评论基线、已回评数、处理清单；
- `decision_logs/decisions_*.jsonl` 尾部若干条（按文件新到旧取，按旧到新展示）。

不写参数时取 `harness.state_tail_lines`（默认 20）；显式参数夹在 1-500 之间，不是整数则报错。文件不存在或解析失败只标记“不可用”，不中断输出。读取的是磁盘文件，受被测插件写盘延迟影响；进程内权威值应看被测插件自己的状态命令。

```text
/h_state
/h_state 50
```

### /h_wait_reload

```text
/h_wait_reload <期望版本> [期望组件数] [超时秒]
```

轮询 `component.get_all_plugins`，直到目标插件版本等于期望版本（字符串精确相等），且组件数满足期望：组件数大于 0 时要求完全相等，省略或不大于 0 则不校验。默认超时取 `harness.reload_timeout_seconds`（60 秒），实际等待不少于 5 秒；轮询间隔取 `harness.reload_poll_seconds`（2 秒），实际不少于 1 秒。成功时报告第几次轮询命中，超时时报告最后观测到的版本、组件数或“未装载”。

```text
/h_wait_reload 0.2.0
/h_wait_reload 0.2.0 12 120
```

### /h_api

```text
/h_api <api_name> [key=value ...]
```

第一个裸词作为 API 名，其余 `key=value` 交给 `ctx.api.call(api_name, **kwargs)`；参数键会转成小写。值会自动转换：纯整数转 `int`，`true`/`false` 转 `bool`，其余保持字符串。返回 `success=false` 视为失败；成功时回显返回值（非字符串会 JSON 序列化），超过 1600 字符截断。

```text
/h_api some_api_name
/h_api some_api_name chat_id=global limit=5 enabled=true
```

### /h_models

```text
/h_models
```

调用 `ctx.llm.get_available_models()`，只列出任务名并排序，不发起模型请求。返回为空或形态不可用时按失败提示。可用来核对 `[judge]`/评估任务是否指向便宜的非思考模型。

### /h_say

```text
/h_say [group|private] <文本>
```

正文以 `private ` 开头就发主测试私聊，以 `group ` 开头就发主测试群，缺省发主测试群。目标固定是 `test_group_id` 或 `test_private_qq`，额外白名单不会改变 `/h_say` 的目标。插件先按群号/QQ 解析 `stream_id`，不存在则 `open_session` 创建，最后用 `ctx.send.text` 直发。

这是插件唯一的主动发送入口；其余命令都不会往聊天里发消息。

```text
/h_say 测试消息
/h_say private 私聊测试消息
```

## 文件任务通道

插件启用且数据目录可写时，后台任务每 2 秒扫描一次本插件数据目录，按文件名排序消费 `tasks/*.json`，并自动创建以下目录：

```text
<本插件数据目录>/
├─ tasks/
│  ├─ *.json          # 投放任务
│  └─ done/           # 处理完的任务归档
└─ results/
   └─ <id>.json       # 任务回执
```

### 任务文件

任务顶层必须是对象：

```json
{
  "id": "inj-001",
  "action": "inject",
  "params": {
    "private_qq": 10001,
    "text": "晚上好",
    "is_notify": true
  }
}
```

- `id` 可省略，缺省用文件名（不含扩展名）；回执文件名优先取 `id`。
- `action` 必填，忽略大小写，只认 `inject`、`seq`、`state`、`freeze`、`resume`。
- `params` 可省略，缺省为空对象，但必须是对象。

| action | params |
| --- | --- |
| `inject` | 与 `/h_inject` 完全相同；回执附带 `message_id`、`target`、`sender`、`is_notify` |
| `seq` | `name`（或 `scene`）指定场景；`run` 缺省 `true`，设 `false` 只返回 dry-run 预览（`steps`、`assertions`、`manual_notes`），不注入 |
| `state` | `limit` 指定 decision_logs 尾部条数，缺省 `state_tail_lines`，显式值夹 1-500 |
| `freeze` | `reason` 写入熔断原因，超 200 字符截断，缺省「文件任务通道 freeze」 |
| `resume` | 忽略参数 |

`inject` 与 `seq` 都复用命令通道的同一批核心函数，目标白名单、sender 黑名单、适配器前置检查全部一致；文件通道没有绕过入口。

文件通道按顺序处理任务：`seq` 动作会等待场景执行完（场景共用串行锁），期间后续任务与下一轮扫描顺延。

### 回执与归档

回执写到 `results/<id>.json`，字段为 `id`、`action`、`status`（`ok` 或 `error`）、`summary`、`ts`、`ts_iso`，有参数时附 `params`，动作另有摘要字段（如 `seq` 的 `steps`）。写入先落 `.tmp` 再原子替换，失败重试一次；仍失败只记 ERROR，通道继续运行。回执名会做安全化处理（拒绝路径分隔符、`.`/`..`、Windows 保留名，超长截到 120 字符）；重复使用同一个 `id` 会覆盖旧回执，任务文件归档则保留历史。

任务文件处理后一律移入 `tasks/done/`；重名时在文件名中加时间戳后缀，不覆盖历史归档。

- 坏 JSON 或顶层不是对象的任务：写错误回执并归档。解析失败会先等 0.2 秒重试一次，仍失败才按坏 JSON 处理。
- 单个文件处理异常：记 WARN，归档该文件，不影响本轮其他任务。
- 单轮扫描异常：记 WARN，下一轮继续，通道不退出。
- 文件在扫描后、读取前被移走：跳过，视为未消费。
- 插件停用或数据目录不可用：通道不启动，命令通道仍可用。

通道的安全边界是动作白名单加纯数据参数：分发用字面量映射，不做动态导入或代码执行，参数只做 `get`/`str`/`int`/`float` 取值。能往 `tasks/` 写文件的人就能提交这些动作，因此数据目录的写权限等同于操作权限；具体目标仍受注入白名单和黑名单约束。

## 自动行为与副作用

### 注入网关

`harness_inject_gateway` 是 `receive` 网关，平台 `qq`，协议 `harness`，组件方法为空实现。插件加载和配置热重载时把它上报为 ready；上报刻意不带 `account_id`/`scope`，避免把夹具登记成某个 QQ 账号的适配器。每条注入通过 `ctx.gateway.route_message` 走本插件网关，`route_metadata.self_id` 取配置的 `self_id`，`external_message_id` 与 `dedupe_key` 都取注入消息的 id。

### 环路守卫

Hook `harness_loop_guard` 绑定 `chat.receive.after_process`，模式 `OBSERVE`、顺序 `NORMAL`、错误策略 `LOG`、超时 20 秒。它只在 `guard_enabled` 和插件启用时工作，并且只统计同时满足以下条件的入站消息：

- 私聊（`group_info` 为空）；
- `additional_config.harness_injected` 不为真；
- `additional_config.self_id` 等于 `self_id`；
- sender 等于 `guard_peer_qq`。

窗口为 `guard_window_minutes` 分钟，只保存 `time.monotonic()` 时间戳，不保存任何消息内容。窗口内来自对端的消息条数达到 `guard_round_trips` 即触发一次自动熔断（调用与 `/h_freeze` 相同的 `_do_freeze`），随后清空窗口；触发后 `guard_cooldown_minutes` 内不重复触发。阈值按“条数”计，不是成对往返。未达阈值时每计数一条记一次 WARN；钩子内部异常只记 WARN，不影响主链路。

### 主动行为边界

插件不做 LLM 生成，不主动发送消息；唯一例外是显式 `/h_say`。`/h_models` 只查询任务名，不发起模型调用。`/h_inject` 和场景执行会把消息注入真实入站链，被测 bot 可能因此真实回复到测试群或主测试私聊，这是被测链路的行为，不是夹具在发言。

## 配置

配置位于插件目录的 `config.toml`，`[plugin]` 与 `[harness]` 两段都是顶层扁平字段。所有字段都有默认值。

### 基础

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `true` | 总开关。关闭后注入网关下线、文件通道停止、所有 `/h_*` 拒绝执行 |
| `plugin.config_version` | `"0.1.1"` | `[plugin]` 段必填；WebUI 中隐藏且只读 |

### 身份与目标

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `harness.self_id` | `1420671909` | 被测 bot 账号。注入的 `additional_config.self_id`、路由与适配器账号比对都用它 |
| `harness.self_nickname` | `"麦麦"` | 仅用于 `is_at` 时 `at` 段的显示名，不参与身份匹配 |
| `harness.sender_default` | `1507674097` | `/h_inject` 未给 `sender_id` 时的发送者 |
| `harness.test_group_id` | `392653174` | 主测试群，注入与 `/h_say` 的群白名单基线 |
| `harness.test_private_qq` | `1507674097` | 主测试私聊，注入与 `/h_say` 的私聊白名单基线 |
| `harness.extra_allowed_group_ids` | `[]` | 额外允许注入的群；只许旁观的群不得加入 |
| `harness.extra_allowed_private_qqs` | `[]` | 额外允许注入的私聊 QQ |
| `harness.blocked_senders` | `[2634571198]` | 注入 sender 黑名单，命中即拒绝 |
| `harness.command_allowed_users` | `[1507674097]` | `/h_*` 调用者白名单；本机控制台不受限 |

### 环路守卫

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `harness.guard_enabled` | `true` | 是否计数并自动熔断 |
| `harness.guard_peer_qq` | `2634571198` | 只统计该 QQ 发来的私聊入站 |
| `harness.guard_window_minutes` | `10` | 滑动窗口长度，范围 1-120 |
| `harness.guard_round_trips` | `2` | 窗口内对端消息达到该条数即触发熔断，范围 1-20 |
| `harness.guard_cooldown_minutes` | `10` | 触发后的冷却时间，范围 1-120 |

### 熔断与适配器

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `harness.freeze_target_plugin` | `"github.cateye.cateye-suite"` | `/h_freeze`、`/h_resume`、`/h_state`、`/h_wait_reload` 共同操作的目标插件 |
| `harness.freeze_component_types` | `["tool", "hook_handler"]` | 熔断时停用的组件类型，大小写不敏感 |
| `harness.adapter_plugin_id` | `"maibot-team.snowluma-adapter"` | 真实链路检查用的适配器插件 id |
| `harness.adapter_gateway_name` | `"snowluma_gateway"` | 只用于检查该适配器是否含同名网关组件 |
| `harness.verify_adapter_ready` | `true` | 注入前是否做真实链路探测；设为 `false` 跳过整段检查 |
| `harness.adapter_probe_api` | `"adapter.napcat.system.get_login_info"` | 就绪探测 API；失败或登录账号不符即拒绝注入 |

### 命令参数

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `harness.reload_timeout_seconds` | `60` | `/h_wait_reload` 默认超时，范围 5-600 |
| `harness.reload_poll_seconds` | `2` | `/h_wait_reload` 轮询间隔，范围 1-30 |
| `harness.state_tail_lines` | `20` | `/h_state` 与文件任务 `state` 的默认尾部条数，范围 1-500 |

配置热重载（`on_config_update`，仅 `scope="self"` 生效）会清空守卫窗口与冷却。`plugin.enabled` 改为 `false` 时注入网关下线并停止文件通道；改回 `true` 时重新上报网关 ready 并重启文件通道。其余参数在每次命令或任务执行时现读，无需重启。

## 注意事项

- 注入的是真实入站消息，被测 bot 会真实回复，可能触发 LLM、工具和主动行为。只想让消息入库时加 `is_notify=1`，它不会触发回复。
- 目标群、目标私聊和 sender 都有白名单/黑名单校验；默认只碰主测试群、主测试私聊，且不允许把 sender 伪造成黑名单里的对端 bot。
- `/h_freeze` 只停组件，停不掉后台 asyncio 循环；`/h_resume` 会把冻结时记录的所有组件（包括本来就停用的）重新启用。对生产环境或非测试插件执行前先确认 `freeze_target_plugin`。
- `/h_seq ... run` 在后台执行，场景里含分钟级 `sleep`，命令返回不代表场景完成；同一时间只允许一个后台场景，结果看宿主日志。
- `/h_wait_reload` 会一直阻塞到匹配或超时（默认 60 秒，最少 5 秒），版本按字符串精确比较。
- `/h_state` 是磁盘快照，受被测插件写盘延迟影响；需要进程内权威值时应看被测插件自己的状态命令。
- `/h_say` 会真实发消息，且只能发主测试群或主测试私聊。
- 文件通道以数据目录写权限为操作边界；投放的任务处理完就归档，检查结果请读 `results/`。
- 场景的 `manual_notes` 不会由夹具执行，时间参数搬移、旧 JSON 放置、配置收紧等需要人工在宿主侧预置。

## 许可证与仓库

MIT，Copyright (c) 2026 cateye，见 [LICENSE](LICENSE)。

仓库：<https://github.com/cateyemizuki/cateye_harness>
