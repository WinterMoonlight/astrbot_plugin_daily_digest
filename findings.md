# 项目发现

## 2026-11-12 初步探索
- 当前只有原始需求、服务器资料及本地技能；未发现插件实现或已有 Git 仓库。
- 需求文件：AstrBot多群日报插件开发需求.md。文档称部署版本 AstrBot v4.29.4，NapCat + OneBot v11，Linux Docker Compose；尚未实机核实。
- 已确认：多群合并、仅私聊投递、可编辑总结提示词、动态读取人格提示词、日/周分钟级调度、插件独立调用已有模型。
- 建议项尚待用户确认为首版范围：独立回看范围、WebUI 预览、执行状态、文本/链接整理、去重、补发及持久化投递记录。
- 风险待核实：群历史条数与覆盖范围、WebUI 插件扩展能力、超时后投递结果未知、跨群消息去重及模型上下文限制。
- 本地 grill-me/SKILL.md 内容为运行 /grilling 会话；未附具体问卷或实现。采用逐项问题确认方式，不把建议当成用户决定。
- 服务器资料含公网地址文本及 PEM 私钥；不记录其内容、不上传。
- Git 可用，已有全局提交身份；未发现 gh。GitHub 连接器可查询账号及仓库，但没有创建仓库工具。
- GitHub 连接器确认当前账号 WinterMoonlight。本地 Git 使用 manager 凭据助手。
- 已初始化 main 分支；git check-ignore 验证服务器地址文件和私钥均被忽略。
- 未发现本机 SSH config；资料未包含 SSH 用户名/端口，已向用户询问。
- 已访问官方插件开发、插件配置、AI 文档；指定版本的 GitHub 源码页面抓取失败，后续改用原始源码或实机源码核实。

## 参考资料
- 用户确认：仓库 astrbot_plugin_daily_digest（WinterMoonlight 私有仓库）；SSH 用户 root、端口 22；原文全部首版建议及调度投递建议均纳入首版。
- 本地 Git 凭据助手未返回可用 GitHub 凭据；Edge 已登录正确账号，可通过网页创建仓库。
- 成功读取 v4.29.4 原始源码：Context 包含 llm_generate、send_message、register_web_api、persona_manager、message_history_manager；仅验证接口存在，尚未确认实际部署与完整语义。
- 官方配置文档支持 text 多行输入及 select_persona/select_provider；交互式预览、执行状态的页面接入仍需核实。
- 已创建并通过 GitHub 连接器验证仓库： https://github.com/WinterMoonlight/astrbot_plugin_daily_digest ，visibility=private。
- SSH 只读核实：Ubuntu 24.04.5 LTS，AstrBot 4.29.4，容器内 Python 3.12.16，AstrBot/NapCat 各一容器且运行正常。
- 实际主配置已开启 group_message_history_enable，group_message_history_max_cnt=999。这是当前配置保留条数，不能当作完整覆盖一天/一周的保证，也尚未证明是系统硬上限。
- 指定版本历史管理器支持 page/page_size 分页读取及 count；分页只能读取仍被保留的数据。
- 原 PEM 文件 Windows ACL 过宽导致 OpenSSH 拒绝；在被忽略的 .local 内创建仅当前用户可访问的副本后成功连接，原文件未修改。
- 用户确认允许插件在服务器本地持续保存所选群消息，并提供可配置保留天数。具体默认保留期限尚待确认。
- https://docs.astrbot.app/dev/star/plugin-new.html
- https://docs.astrbot.app/dev/star/guides/plugin-config.html
- https://docs.astrbot.app/dev/star/guides/ai.html

## 2026-11-12 管理页面可行性核查
- 用户授权：先核对开发文档，若专用管理页方案可行则直接采纳。
- 官方《插件可视化视图》文档提供 views/<view_name>/index.html、插件详情入口、受限 iframe 与 AstrBotPluginView bridge；后端使用 register_web_api 和 astrbot.api.web。
- 已只读检查运行容器：存在 plugin_page_bridge.js、plugin_page_service.py、api/plugins.py 及已编译 PluginViewPage 前端资源，说明不是仅有新版本文档、当前部署也具备相关代码。
- 下一步核验实际鉴权、静态资源与 API 路由是否沿用 Dashboard 服务，再作出采用结论。
- 文档：https://docs.astrbot.app/dev/star/guides/plugin-pages.html
- 最终结论：方案可行，按用户“可行就采纳”授权确定使用原生 Plugin Views，专用页复用 AstrBot 登录与现有 Dashboard 服务端口。
- 后端证据（实机 /AstrBot 下）：api/plugins.py:398-440 的插件扩展 API 使用 require_plugin_scope；api/auth.py:167-195 接受现有 Dashboard JWT；plugins.py:1159-1220 提供视图入口与受限资源令牌鉴权。静态页面和业务 API 均是现有 Dashboard 路由，无需插件另启监听服务。
- 视图资源令牌限定插件名和页面名，页面通过父 WebUI 的 bridge 访问 API；不要求用户复制登录令牌或另建账号。
- 未带凭据向运行中 Dashboard 的 /api/v1/plugins/astrbot_plugin_daily_digest/views 和 /api/v1/plugins/extensions/astrbot_plugin_daily_digest/status 发送只读 GET，均返回 401，确认鉴权在插件安装前即生效。此检查不代表插件已安装或页面已完成联调。
- 页面计划放在 views/digest/index.html；管理任务、预览和执行记录在同一专用视图中组织。任务数量和默认消息保留期限仍待确认。
- 验证边界：完成文档、实机代码和匿名请求检查；尚未实现视图，登录后交互与实际业务接口联调留到开发验收。
- 可引用的版本源码：https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/dashboard/api/plugins.py

## 2026-11-12 多任务与保留期限问询
- 用户选择 A：支持多个独立任务，各自设置源群、提示词、执行时间与私聊收件人。
- 接下来仅确认原始群消息的默认保留期限，建议默认 30 天并可配置，到期清理；用于在日/周回看之外留出补发时间。保留期不能保证启动前或断连期间的消息完整性。
- 总结、执行记录和防重复投递凭据不等同于原始群消息，不能未经确认一起按该期限删除。具体清理和回看范围的兼容规则留在方案阶段明确。

## 2026-11-12 历史优先策略修订
- 用户提出低消息量优先复用 AstrBot 群历史，按目标时间范围筛选；容量不足时再讨论扩展。不再推进默认 30 天独立存储建议。
- 实机 builtin_stars/astrbot/main.py:138-182 的 persist_group_message 按群配置开关写入普通群事件，本函数不要求 @ 唤醒或主动回复；但未验证所有上游过滤和运行期间断连情况，不能据此保证绝不漏消息。
- 实机 core/db/sqlite.py:781-822 每次插入按 platform_id + user_id（UMO）仅保留最近 max_messages 条；get 支持分页并按 created_at/id 排序。当前 999 是配置值，代码此处没有固定 999 的上限。
- 收到的消息与部分机器人发出的群消息共用保留范围，不能只按人类发言条数估算。
- 每日 70–80 条、记录正常的前提下，999 条通常足够昨天或最近 24 小时；周报与补发仍需检查各自时间范围。达到 999 条不必然意味着当天缺失；未达到 999 条也不能证明采集完整。
- 覆盖检查应分别呈现可见最早/最新记录、所选时间范围、是否触及容量及已知采集中断；未知覆盖不得误报为完整或无消息。历史不足时如何处理待确认，不自动改配置或启用新存储。
- 官方实现：https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.29.4/astrbot/core/db/sqlite.py
- 时间语义补充：core/db/po.py:11-20 的 created_at 默认为入库时 UTC 时间，PlatformMessageHistory 没有单独的原始平台发送时间字段。筛选必须正确转换 Asia/Shanghai 边界，并说明按已记录时间筛选；不能声称对延迟到达消息保证原始发送日期精确归属。
- 历史消息序列化将图片/语音/视频/文件保存为类型占位符，通常没有附件文件名；复用历史时只能保留已有信息，不补造文件名。

## 2026-11-12 可配置时间筛选
- 用户提出增加选项，筛选指定多久前的消息。方向为每个任务独立的时间范围，与日/周执行频率分开。
- 本轮问询用“最近 N 小时/天”举例，并澄清是否还需要“48 小时前到 24 小时前”等起止区间；尚未确认后者。
- 沿用已确认的调度原则：定时执行以计划时点为锚，延迟执行不移动范围；补发沿用原计划时点。扩大回看范围不能恢复已被 AstrBot 清理的记录。

## 2026-11-12 确认回看模式并新增自动提醒
- 用户选择 A：每个任务只需最近 N 小时/天的回看时长。
- 新增需求：从总结内容中提取有时间的重要事项，自动加入 AstrBot 定时任务来提醒用户。尚未确认提醒筛选提示词、提前量、日期范围及变更/取消策略。
- 已只读核实实机 Context.cron_manager。core/cron/manager.py:183-212 的 add_active_job 支持 run_once/run_at、持久化和 payload；执行时读取 payload.session 作为目标会话。该方式会运行 Agent，不能描述为不再调用模型的直接消息投递。
- manager.py:156-181 的 add_basic_job 可注册插件 handler；140-150 表明持久化 basic 任务重启后缺少 handler 会跳过，不能只设置 persistent=True 就宣称恢复可靠。其创建参数不直接提供 run_at/run_once，具体采用路线需方案阶段核实。
- 定时后端可列出任务，API 与管理服务共用 CronJobManager；具体类型在原生未来任务页面的呈现、重启、禁用/卸载行为还需验证。
- 拟议产品规则（待确认）：每个日报任务可开关自动提醒；模型提取后程序校验日期时间与来源；跨群/多次汇总避免重复创建；模糊或过期时间不自动猜测；预览不创建真实提醒。初次问询优先确认哪些事项应该自动提醒。
- 官方文档：https://docs.astrbot.app/use/proactive-agent.html
- 指定版本源码：https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.29.4/astrbot/core/cron/manager.py

## 2026-11-12 改为用户选择后创建日程提醒
- 用户进一步要求：根据简报列出的重要日程，在相应时间询问哪些需要加入，用户用自然语言回复并自主选择。
- 最新产品流程：生成候选日程 → 向该任务私聊收件人询问 → 解析收件人的选择 → 创建所选事项的 AstrBot 定时提醒 → 反馈创建结果。替代默认无确认自动创建；仍指 AstrBot 内部提醒，不推定接入外部日历。
- “相应时间”尚有歧义，需要确认随简报发出后询问，还是每天另设时间；未推定为事项开始时询问。
- 拟议交互示例（未逐项确认）：用户可按编号或事项名称选择，也可回复全部/都不要；若指定提前量则一起解析，缺少必要信息或无法确定所指事项时追问，不据猜测创建。
- 实现边界待方案核实：只接受配置收件人的选择，将候选批次与简报任务关联，防止多任务混淆、重复回复重复创建；原始群消息不能当作收件人的确认。
- 预览仍沿用原需求：仅 WebUI 展示，不因预览发出私聊询问或创建真实定时任务。
- 最新确认：用户选择 A，简报成功发出后紧接询问，不另设询问时间。此前关于询问时间的待确认状态已被此决定替代。
- 用户要求将全部讨论同步到主需求并整理上下文后暂停；主需求已按已确认/待确认分开更新，续接入口为“续接摘要.md”。

