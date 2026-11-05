# 多 Agent 智能客服系统

基于 **LangGraph** 编排的多 Agent 客服后端：用户消息进来后先做意图路由，分派给**知识问答（RAG）**或**工单处理**，产出结果再经**两阶段合规审查**；审查不通过时**自动整改并复审**，最后汇总返回。

技术栈：Python · FastAPI · LangGraph · LangChain · SQLAlchemy(SQLite) · OpenTelemetry · Pydantic

---

## 一、定位说明（先说实话）

这个系统**严格来说属于 routing workflow + evaluator-optimizer，不是完全自主的 agentic 系统**。依据：

- 流程与边**由代码预定义**，节点不会自主决定下一步，也没有 Agent 之间的协商；
- 没有并行分支；
- 一次请求固定走一条分支，只有合规环节存在**真实的循环**。

它确实具备多智能体的几个要素：**职责单一的处理单元**、**通过共享状态协作**、**条件路由**、**强制的出口审查**，以及**评估不通过时回到生成侧整改**的闭环。

要变成完全自主的 agentic 系统，还需要补：自主工具调用循环（由模型决定调哪个工具并多轮执行）、动态任务拆解、以及更强的反思重试策略。

---

## 二、架构

```
                         ┌──────────────────────┐
   用户消息 ──► supervisor_route ──条件路由──┐
                         └──────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
              knowledge_rag                  ticket_handler
             (RAG 四段式)                   (经 MCP 工具层)
                    │                             │
                    └──────────────┬──────────────┘
                                   ▼
                          compliance_check
                     （规则快筛 + LLM 深度审查）
                            │            ▲
                   通过 ────┤            │ 不通过且还有整改额度
                            │            ▼
                            │         revise（回传违规原因让模型重写）
                            │            │
                            │            └──────► 回到 compliance_check
                            ▼
                       synthesize ──► END
```

**图里有一条回边**（`revise → compliance_check`），这正是 evaluator-optimizer 模式的体现：评估不过就让生成侧改，改完再评，达到上限才拒答。

### 请求处理链路

```
HTTP ─► 鉴权(API Key) ─► 限流(令牌桶/按调用方) ─► 输入侧检查 ─► 按调用方并发闸门
     ─► LangGraph 图执行 ─► 合规审查（不通过则整改复审）
     ─► 短期记忆落库（成功才写） ─► 响应
```

### 记忆与持久化

| 层 | 存储 | 生命周期 | 用途 |
|---|---|---|---|
| 工作记忆 | 进程内存 | 进程存活期 | 路由判断的上下文 |
| 短期记忆 | Redis（可选）→ 降级内存 | 空闲过期 + 条数/会话数上限 | 历史查询接口的缓存 |
| 长期记忆 | `knowledge/*.md` + 分块索引缓存 | 落盘 | RAG 检索源 |
| 图检查点 | SQLite（`AsyncSqliteSaver`） | 跨重启 | **模型看到的多轮上下文** |
| 业务数据 | SQLite（SQLAlchemy） | 跨重启 | 工单 |

> 会话事实来源：**模型上下文以图检查点为准**；`/api/history` 优先返回短期记忆（带时间戳的缓存），未命中时回退到检查点，响应中带 `source` 字段说明来源。

---

## 三、快速开始

1. 安装依赖：`python -m pip install -r requirements.txt`
2. 复制 `.env.example` 为 `.env` 并填入模型配置（密钥不会进入版本库）
3. 启动：`python run.py`，打开 <http://127.0.0.1:8001>（接口文档在 `/docs`）

Windows 下可用 `.venv\Scripts\python.exe run.py`。

### 启动前请注意两件事

- **模型配置缺失会直接启动失败**（而不是等第一个请求才报 502）；
- **未配置 `API_KEYS` 且监听了非本机地址时会拒绝启动**，避免接口对外裸奔。

---

## 四、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 主聊天接口（一次性返回） |
| POST | `/api/chat/stream` | **SSE 流式**：逐节点推送 `start` / `node` / `done` / `error` |
| GET | `/api/history/{session_id}` | 会话记录（缓存优先，回退检查点） |
| DELETE | `/api/history/{session_id}` | 清空会话：短期记忆 + 工作记忆 + 图检查点 |
| GET | `/api/tools` | 工具发现（MCP `tools/list`） |
| POST | `/api/tools/call` | 工具调用（MCP `tools/call`） |
| POST | `/mcp` | **MCP 传输入口**（JSON-RPC 2.0：`tools/list`、`tools/call`、`ping`） |
| GET | `/api/metrics` | per-agent 指标 + token 用量 + 工具调用日志 |
| GET | `/health` | 健康检查（公开） |

鉴权：`Authorization: Bearer <key>` 或 `X-API-Key: <key>`。限流超限返回 **429** 并带 `Retry-After`。

---

## 五、关键设计决策

| 决策 | 理由 |
|---|---|
| 用 LangGraph 而不是一串 `if-else` | 流程与状态解耦：节点只读写共享状态，加 Agent 只需加节点和边；检查点机制直接复用 |
| `sub_results` **不加 reducer** | 加了会让 `initial_state` 的空值与检查点旧值合并，**上一轮中间结果串进本轮变成脏数据**；当前是串行流水线，保持覆盖语义更安全（引入并行时再配合显式重置一起改） |
| 检索用**词法匹配**而不是向量 | 零外部依赖即可跑通；且客服场景有大量精确匹配（订单号）需求，那是向量检索的弱项。升级路径清晰：换 `search()` 实现即可，上层不动 |
| 合规用**两阶段** | 规则引擎毫秒级、零成本、可解释，但只能查已知模式；LLM 能覆盖未知语义。**规则治"已知"，LLM 治"未知"**，两者互补 |
| 合规 **fail-closed** | 审查结果解析失败时判定为**不通过**：安全场景下"不确定"必须当作"不安全" |
| 结构化路由**显式指定 `method="function_calling"`** | `with_structured_output()` 默认走 `response_format`，实测所在网关会以 400 拒绝，只有 function_calling 可用（**这个坑单测发现不了，是真实调用才暴露的**） |
| 并发闸门**按调用方分桶** | 原先的全局 `Semaphore(1)` 把整个服务锁成串行，排队请求必然超时；分桶后单个调用方仍受保护，不同调用方互不拖累 |
| token 计量用**代理包装** | 一次问答要调 5 次模型，逐点埋点会漏统计且侵入业务代码；`MeteredLLM` 代理让 Agent 代码零改动 |
| 上下文窗口**按偶数条裁剪** | 保证剩下的历史仍是"提问 + 回答"成对结构，不留下孤立的回答 |

---

## 六、能力边界（未实现）

- **不是完整 MCP 实现**：只有 tools 子集 + 一个 HTTP JSON-RPC 入口，没有 stdio/SSE 传输层、没有 `initialize` 握手与能力协商、没有 resources/prompts。
- **没有真实向量检索**：检索是中文 bigram + 词元重合度的本地词法匹配。
- **没有鉴权体系**：只有静态 API Key，没有用户表、密钥轮换、审计日志；密钥在 `.env` 明文、浏览器 `localStorage`。
- **限流与并发闸门是进程内实现**：多实例部署需换成 Redis 实现。
- **工单状态机较简单**：4 个状态与固定流转表，没有流转历史审计。
- **示例知识库**：`knowledge/` 下三篇演示文档，不代表真实金融政策。
- **`order_query` / `risk_check` 工具仍是演示逻辑**，未接入真实订单与风控系统。
- 没有 Docker/CI，没有覆盖率统计；HTTP 层（401/429/`/mcp`/SSE）目前只有手工验证。
- 自省能力有限：整改只做一次全量重写，**不校验整改后的事实是否与原文一致**。

---

## 七、目录结构

```
run.py                        启动入口（监听地址来自统一配置）
knowledge/                    知识库源文件（Markdown）
python-impl/
  config.py                   统一配置入口（Pydantic 校验 + 缓存）
  logging_setup.py            日志初始化
  settings.py                 按配置创建模型客户端
  domain/                     领域模型与业务规则（工单状态流转）
  agents/                     supervisor / knowledge_rag / ticket_handler
                              / compliance_checker / revision
  api/main.py                 接口层：路由、生命周期、SSE、异常映射
  memory/                     工作记忆 / 短期记忆 / 长期记忆
  persistence/                数据库连接、ORM 模型、工单存储实现
  security/                   鉴权 / 限流 / 并发闸门 / 输入侧检查
  tool_server/                MCP 工具层
  tracing/                    OpenTelemetry 追踪与 token 计量
  web/index.html              单页聊天界面（支持 API Key，SSE 接口尚待接入）
tests/                        pytest 测试套件（假模型，不消耗额度）
修改记录/                     每次改动的中文记录（含验证结果与未解决问题）
```

---

## 八、测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

测试**不调用真实模型**：`FakeLLM` 按提示词内容返回固定结果，即可跑通整张图，因此可离线、可重复、不消耗模型额度。覆盖图表结构、路由与兜底、合规闸门与 fail-closed、整改循环的终止性、上下文窗口、工单领域规则、工具层鉴权与入参校验、RAG 重排的异常路径、鉴权策略、限流、并发闸门、持久化与索引复用。

---

## 九、来源说明

基于 [bcefghj/smart-cs-multi-agent](https://github.com/bcefghj/smart-cs-multi-agent)（版本 `e045f0256d42db3c800662f60bd957d9c78374a4`）的 Python 实现做二次开发，保留上游 MIT 许可证。本仓库在其之上完成了分层重构、持久化接入、安全与限额、MCP 工具层接通、测试与可观测性建设等改动，详见 `修改记录/`。
