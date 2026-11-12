# AstrBot 多群日报插件开发计划

## Goal
按原始需求开发适配现有 Linux Docker Compose 部署的 AstrBot 多群日报插件；本轮先完成项目探索、Git 初始化、私有 GitHub 仓库及需求澄清。

## Next Step
初始化同步已核验；下一步确认 WebUI 接入、任务数量、保留期限与补发策略。

## Current Phase
Phase 2 — 需求确认与方案（本轮先完成初始化文档同步）

## Phases
### Phase 1: 项目探索与初始化
- [x] 阅读需求和 planning-with-files、grill-me 技能
- [x] 创建计划、发现和进度记录
- [x] 初始化 Git 并排除服务器凭据
- [x] 创建私有 GitHub 仓库并验证
- [x] 只读核对服务器部署和核心接口（UI 扩展及完整性策略留待方案阶段）
- **Status:** complete

### Phase 2: 需求确认与方案
- [ ] 使用 grill-me 确认首版范围、WebUI 形态、历史完整性及补发策略
- [ ] 依据部署源码验证可用接口并提交方案供用户确认
- **Status:** in_progress

### Phase 3: 插件实现
- [ ] 按确认方案实现配置、历史采集、总结、调度、预览及私聊投递
- **Status:** pending

### Phase 4: 验证与部署
- [ ] 完成针对性自动化检查和需求验收
- [ ] 确认实际源群和私聊目标后进行受控试跑
- **Status:** pending

## Decisions Made
| 决定 | 依据 |
|---|---|
| 仓库暂时私有 | 用户明确要求 |
| 全过程使用三个 planning-with-files 文件 | 用户明确要求 |
| 未明确的产品选择向用户确认 | 用户要求使用 grill-me，不擅自决定 |
| 服务器资料和密钥不进入版本库 | 连接凭据仅用于授权服务器访问 |

## Questions
- 已确认仓库名称 astrbot_plugin_daily_digest，账号 WinterMoonlight，私有。
- 已确认所有首版及调度投递建议均纳入首版。
- 已确认 SSH root / 22。
- 已确认允许持久化所选群消息，并提供可配置保留天数；默认天数待确认。

## Errors Encountered
| 问题 | 次数 | 处理 |
|---|---|---|
| git rev-parse 报告当前不是 Git 仓库 | 1 | 符合新项目现状，准备初始化 |
| 未发现 gh；GitHub 连接器无创建仓库工具 | 1 | 检查可用登录方式后选择创建路径 |
| GitHub 版本源码页面抓取 Cache miss | 1 | 后续改用 raw 源码或实机读取 |
| 本地 Git 凭据助手无可用 GitHub 凭据 | 1 | Edge 已登录，改用网页创建仓库；推送路径待验证 |
| OpenSSH 拒绝原密钥：权限过宽 | 1 | 创建权限收紧的忽略目录内副本，连接成功 |
| GIT_SSH_COMMAND 触发 Git 附带 sh 的 MSYS 访问拒绝 | 1 | 改用原生 OpenSSH 路径，连接未及时返回；不重复该方式 |
| 远程配置含 UTF-8 BOM 导致 JSONDecodeError | 1 | 改用 utf-8-sig，读取成功 |
| CIM 进程查询不可访问 | 1 | 不再使用 CIM，改用执行会话状态检查 |
| 本地 Git 远端读取未及时返回 | 1 | 使用已连接 GitHub 接口同步初始化文件，避免依赖不可用的本地认证 |
| 沙箱禁止写 .git/objects | 1 | 使用已授权初始化任务的提权执行路径，自动审核通过 |
| 重建提交时哈希不匹配 | 1 | 通过哈希验证定位 GitHub 提交原时区为 +0800 |
| Windows Python 输出编码使首次文档同步中文损坏 | 1 | 本地原文件完好；改用 ASCII 转义 JSON 传输，并逐项验证 Git 对象哈希，修复远端文件 |
| apply_patch 不允许在同一补丁删除并新增同一路径 | 1 | 改为 Update File，已完成 |

