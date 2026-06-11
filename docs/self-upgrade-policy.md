# AI / 协同引擎自我升级同步策略

本文定义 Hermes Collab Engine 相关 AI 规则、协同流程和可迁移配置模板的自我升级同步要求。目标是确保稳定规则可备份、可审计、可迁移，并避免把本机敏感状态提交到 GitHub。

## 适用范围

凡是会影响后续 AI 或协同引擎行为的稳定规则变化，都必须按本文策略同步到 GitHub，包括但不限于：

- 记忆、经验、长期偏好或稳定操作规范；
- Hermes / Claude / worker 的技能、提示词、协同流程和执行约束；
- 任务拆解、验证、提交、推送、回滚等协作流程；
- 可迁移的配置模板、安装说明、运行手册和安全策略；
- 其他需要在新机器、新 profile 或灾备环境中恢复的规则性内容。

临时会话信息、一次性调试记录、运行日志和本机私有配置不属于可同步升级内容。

## GitHub 同步要求

每次完成 AI / 协同引擎自我升级后，必须把可迁移、可公开的规则变更同步到 GitHub，作为：

1. 规则备份：避免本机环境丢失后无法恢复；
2. 迁移能力：支持在新服务器或新 profile 中重建同等协同能力；
3. 审计记录：通过 Git 历史追踪规则演进；
4. 多 Agent 协作基线：确保 parent、worker 和后续维护者共享同一套稳定规范。

## 协同职责

- **协同引擎 worker 的定义**：本文中的 worker 指通过 `hermes-collab run` / `hermes-collab worker` 启动、在面板中可见、承接 WBS 节点并可按授权直接操作工作区的协同执行进程；它不同于 Hermes `delegate_task` 产生的子代理，后者只是在一次 Hermes 会话内被委派的临时代理，不默认具备独立提交、push 或长期同步职责。
- **delegate_task 是辅助分析层**：Hermes `delegate_task` 可以使用，但它只适合作为 parent 的辅助层，用于预分析、预拆解、风险审查、方案对比、WBS 优化等，帮助 parent 更准确地调用和分发本地 `/root/hermes-collab-engine/hermes-collab`；它不是面板可见的正式协同执行层，不得冒充或包装成 `hermes-collab` worker。
- **正式协同执行层必须来自 hermes-collab**：只有通过 `hermes-collab run` / `hermes-collab worker` 启动、在面板中可见、承接 WBS 节点并按授权操作工作区的进程，才可称为正式协同执行层或协同引擎 worker。
- **Worker 可以执行修改、提交和 push**：在 parent 明确授权的任务范围内，worker 可负责编辑文档、生成模板、执行验证、准备 commit，并在需要时执行 push。
- **执行身份必须披露**：结果报告必须明确说明实际使用了哪种执行层。当任务由协同引擎 worker 执行时，应说明 worker 身份、WBS 节点或授权范围，以及实际修改、验证、提交或推送的文件；不得笼统以“Claude 已完成”替代授权链说明。若使用 Hermes `delegate_task` 子代理，只能描述为辅助预分析、预拆解、审阅或方案建议等，不得描述为面板可见 worker 的执行结果。
- **授权边界必须可核对**：worker 只能在 parent 下发的 WBS 节点、能力范围和文件 allowlist 内执行；若发现需要修改未授权文件、执行提交 / push，或扩大权限，应停止并回报 parent 重新授权。
- **Parent 必须监督和验证**：parent 必须在准备提交或推送前后检查变更范围、diff、敏感内容和验证结果；不得只依赖 worker 的口头结论。
- **提交 / push 前验证**：确认只包含本次任务允许的文件，运行必要检查，并审阅是否误带本机状态或密钥。
- **提交 / push 后验证**：确认远端分支、commit、GitHub 页面或相关同步结果符合预期；若失败，应停止并报告原因。

## 禁止同步的内容

严禁提交或推送以下敏感或本机运行状态内容：

- API key、token、cookie、密码、私钥、证书和任何认证凭据；
- `profiles/`、`settings`、本机 Claude / Hermes 私有配置或用户专属 profile；
- 运行数据库，例如 SQLite 数据库、缓存、索引和状态快照；
- 日志、会话记录、终端输出、调试转储和临时文件；
- 包含用户隐私、内部地址、未脱敏请求或第三方敏感数据的文件；
- 与本次自我升级无关的代码、构建产物或依赖目录。

如果需要分享配置，只能提交脱敏后的模板、示例或说明，并明确标注占位符。

## Allowlist 最小提交策略

自我升级同步必须采用 allowlist 最小提交策略：

1. 先列出本次允许修改和提交的文件路径；
2. 只编辑 allowlist 中的文件，避免顺手修改无关内容；
3. 提交前使用 `git status --short` 和 `git diff --check` 检查工作区；
4. 使用路径限定方式暂存，例如 `git add docs/self-upgrade-policy.md README.md`，不要使用 `git add .`；
5. 审阅 `git diff --cached`，确认没有敏感内容或无关文件；
6. push 前后由 parent 再次验证远端状态。

当任务只要求文档修改时，不得修改代码；当任务只要求本地变更时，不得执行 `git add`、`git commit` 或 `git push`。

## 推荐检查清单

- [ ] 本次变更是否属于稳定规则或可迁移模板？
- [ ] 是否已经写入仓库中的文档、模板或规则文件？
- [ ] 是否排除了密钥、profiles、settings、数据库、日志和会话记录？
- [ ] 是否只包含 allowlist 文件？
- [ ] 是否运行 `git diff --check` 且无错误？
- [ ] 若需要提交 / push，parent 是否在前后完成验证？
