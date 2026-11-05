# 重写README并明确架构定性

改造计划阶段 6 的收尾：把项目的定位、架构、设计决策与能力边界写进 README。此前 README 只有一段简述，既没有架构图，也没有把"多 Agent"这个说法界定清楚——这在面试中是最容易被追问的点。

## 修改内容

按"定位 → 架构 → 使用 → 接口 → 决策 → 边界 → 目录 → 测试 → 来源"九节重写：

1. **定位说明（先说实话）**：明确写出**它不是完全自主的 agentic 系统，而是 routing workflow + evaluator-optimizer**，并逐条给出依据（流程预定义、无并行、无 Agent 间协商、只有合规环节存在真实循环），同时列出要变成完全自主系统还缺什么。结尾标明与完整 MCP 实现的差距（无传输层、无握手、无 resources/prompts）。
2. **架构图**：画出**含回边的图**，标出 `revise → compliance_check` 这条环，并说明它就是 evaluator-optimizer 的体现；补上请求处理链路（鉴权 → 限流 → 输入检查 → 并发闸门 → 图执行 → 落库）。
3. **记忆与持久化表**：把五处存储（工作记忆 / 短期记忆 / 长期记忆 / 图检查点 / 业务数据）的介质、生命周期、用途列清楚，并说明**会话事实来源以图检查点为准**。
4. **接口表**：补齐 SSE、MCP 入口、会话清理、指标等阶段 1～6 新增的端点，并注明鉴权方式与 429 行为。
5. **关键设计决策表**：把全程做出的 9 个取舍写成"决策 + 理由"，包括 `sub_results` 为什么不加 reducer、为什么用词法检索、合规为什么两阶段且 fail-closed、结构化路由为什么必须显式指定 `function_calling`、并发闸门为什么按调用方分桶、token 计量为什么用代理。
6. **能力边界**：逐条列出**未实现**的东西（MCP 完整性、向量检索、鉴权体系、限流多实例、工单状态机、示例数据、无 Docker/CI、HTTP 层缺自动化测试、整改不校验事实一致性）。
7. **目录结构**：与当前实际分层一致（含新增的 `domain/`、`persistence/`、`security/`、`logging_setup.py`、`knowledge/`、`tests/`）。
8. **测试**：说明测试用假模型、不消耗额度，并列出覆盖范围。
9. **来源说明**：保留上游项目地址、版本与 MIT 许可，并说明本仓库在其之上做了哪些改动。

## 修改原因

用户要求按七阶段计划改造项目。阶段 6 的目标是让"多 Agent"这个说法名副其实——先补上合规修正循环（图里有了环）与 SSE，再把定位写准。README 是简历项目的门面，含糊或夸大都会在面试中被戳穿。

## 验证结果

- README 内容与代码逐一核对，本次核对到的对应关系：
  - 接口表中的每个端点都在 `python-impl/api/main.py` 中存在（`/api/chat`、`/api/chat/stream`、`GET/DELETE /api/history/{id}`、`/api/tools`、`/api/tools/call`、`/mcp`、`/api/metrics`、`/health`）；
  - 架构图中的节点与条件边，与阶段 6 实测编译出的图一致（`compliance_check → revise → compliance_check` 构成环，路由只有 `knowledge_rag` / `ticket_handler` 两个去向）；
  - 记忆与持久化表与 `api/main.py` 的实际装配一致（`ShortTermMemory` + `LongTermMemory` + `SqlTicketStore` + `AsyncSqliteSaver`）；
  - 目录结构用实际文件树核对，无遗漏、无臆造。
- `pytest`：87 passed（本次只改文档，未触及代码）。
- 全部模块导入成功。

## 未解决的问题

- **README 中"浏览器 SSE 接口尚待接入"是事实陈述**：前端 `web/index.html` 目前仍调用非流式的 `/api/chat`，未接入 `/api/chat/stream`。
- 能力边界一节里列出的项目都是**真实未实现**的，其中"HTTP 层缺自动化测试"是最应该优先补的技术债——本次排查已经证明手工验证不可靠。
- 本次未补插图的渲染验证（README 中的 ASCII 图在不同渲染器下的对齐效果未逐一检查）。
- 阶段 7 仍未开始：ADR（架构决策记录）、GitHub Actions CI、容器化、简历与面试材料。
- 未添加 `LICENSE` 中上游作者署名的补充说明（当前 `LICENSE` 为 MIT，README 已注明来源与版本）。
