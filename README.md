# cateye_harness

MaiBot 宿主自测试夹具（TEST_PROCEDURE §四）：① /h_inject 构造标准 MessageDict 经夹具自带 receive 网关注入宿主完整入站链；② /h_seq 预置 6 个里程碑场景（bus_check/affection_basic/affection_review/attribution/silence/mood_effect）的步进脚本；③ /h_freeze、/h_resume 停用/恢复被测插件 tool/hook 组件；④ chat.receive.after_process 环路守卫计数 麦麦⇄2634571198 往来；⑤ /h_state 进程内读取被测插件状态与 decision_logs 尾部；⑥ /h_wait_reload、/h_api、/h_models、/h_say；⑦ 文件任务通道（v0.1.1）：后台每 2s 轮询自身数据目录 tasks/*.json，按文件名顺序分发 inject/seq/state/freeze/resume（复用同一批核心函数、动作白名单、参数纯数据），回执写 results/<id>.json、任务归档 tasks/done/。自身不调 LLM、不主动发言，守卫只计数不存内容。

> 本插件为宿主自测试夹具（开发/调试用途），随个人工具链分发；非通用功能插件。

## 斜杠命令

- `/h_inject` —— 构造标准 MessageDict，经夹具自带 receive 网关注入宿主完整入站链
- `/h_seq <场景>` —— 预置 6 个里程碑场景（bus_check / affection_basic / affection_review / attribution / silence / mood_effect）步进脚本
- `/h_freeze` / `/h_resume` —— 停用/恢复被测插件的 tool/hook 组件
- `/h_state` —— 进程内读取被测插件状态与 decision_logs 尾部
- `/h_wait_reload` / `/h_api` / `/h_models` / `/h_say` —— 辅助命令

## 文件任务通道（v0.1.1）

后台每 2 秒轮询自身数据目录 `tasks/*.json`，按文件名顺序分发 inject/seq/state/freeze/resume
（复用同一批核心函数、动作白名单、参数纯数据），回执写 `results/<id>.json`，任务归档 `tasks/done/`。

## 声明

- 自身不调用 LLM、不主动发言；环路守卫只计数、不存储消息内容。
- 许可证：MIT（见 LICENSE）；manifest 声明区间 host 1.0.0~1.99.99、sdk 2.6.0~2.99.99。
