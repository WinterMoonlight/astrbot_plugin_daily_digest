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

