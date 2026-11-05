# 打通MCP工具层并接入真实存储

改造计划阶段 4：把一直"写了但没接通"的 MCP 工具层真正用起来。

## 修改内容

### 1. 业务层改走工具层

`TicketHandlerAgent` 原先直接操作 `TicketStore`，MCP 工具层形同虚设。现在四个工单操作全部**经由 `MCPToolServer.call_tool()`**：

| 操作 | 调用的工具 |
|---|---|
| 创建 | `ticket_create` |
| 查询 | `ticket_query` |
| 更新状态 | `ticket_update` |
| 列出工单 | `ticket_list` |

`create_supervisor_graph()` 的参数由 `ticket_store` 改为 `tool_server`。好处是**以后把工具实现换成真实工单系统时，Agent 不需要改动**。

Agent 未接入工具层时会返回明确错误（"工单 Agent 未接入工具层"），而不是静默失败。

### 2. 工具绑定真实存储与知识库

`create_default_tools()` 新增 `ticket_store` 与 `long_term_memory` 参数，由调用方注入：

- 工单类工具（新增 `ticket_query` / `ticket_update` / `ticket_list`）直接操作注入的存储实现——生产是 `SqlTicketStore`，测试是内存 `TicketStore`；
- `knowledge_search` 由返回硬编码假文档改为**真实检索知识库索引**；
- `order_query` 保持演示数据，但描述里**明确标注"演示数据，未接入真实订单系统"**，不再让人误以为可用。

### 3. 落实 `requires_auth`

`call_tool()` 新增 `authenticated` 参数，**默认 False 是安全默认值**：声明了 `requires_auth` 的工具必须由已鉴权的调用方显式声明才能执行。四个工单工具都声明了 `requires_auth=True`。

对外的两个入口（`POST /api/tools/call`、`POST /mcp`）按真实鉴权结果传入；Agent 内部调用传 `authenticated=True`，理由是**它只在已鉴权的 HTTP 请求内运行**，这一点在代码注释中写明。

### 4. 入参按 `input_schema` 校验

新增 `validate_arguments()`，实现本项目用到的 JSON Schema 子集：`required`、`type`、`enum`。特别注意 **`bool` 是 `int` 的子类**，所以单独拦掉用 `True` 冒充整数的情况。未声明的参数交给 handler 的默认值处理。

校验在**业务逻辑之前**执行，非法入参不会触达存储层。

### 5. `POST /mcp` JSON-RPC 2.0 入口

`handle_jsonrpc()` 此前**没有任何 HTTP 入口**，协议层白写。现在挂在 `POST /mcp` 上，支持 `tools/list`、`tools/call`、`ping`；未知方法返回 `-32601`，内部异常返回 `-32603`，并要求鉴权。

## 修改原因

用户要求按七阶段计划改造项目。阶段 4 针对的是"架构里有这一层、但业务根本没用它"的问题——这在面试里属于一问就穿的硬伤：MCP 工具层的 4 个工具从未被调用，`handle_jsonrpc` 无入口，`requires_auth` 声明了从不检查。

## 验证结果

### 专项验证（`.work/verify_step4.py`，不发真实模型请求）— `ALL PASS`

- **工具注册**：共 7 个工具（`knowledge_search`、`order_query`、`risk_check`、`ticket_create/query/update/list`），每个都声明了 `inputSchema`。
- **入参校验 6 例**：缺少必填、类型不符、**布尔冒充整数**、枚举越界均被拒绝并给出原因；合法入参与未声明参数通过。
- **`requires_auth`**：未鉴权调用 `ticket_create` 被拒绝（"工具 ticket_create 需要鉴权后才能调用"）；`authenticated=True` 时成功且**工单确实落到存储里**。
- **校验在调用链上生效**：缺 `ticket_type` → "入参校验失败: 缺少必填参数: ticket_type"；`status='created'` → "参数 status 取值必须是 ['processing','resolved','closed'] 之一"。
- **业务层确实走工具层**：用桩替换模型输出后依次触发 create/query/update/list，**工具调用日志显示这 4 个工具全部被调用且全部成功**。
- **未接入工具层时明确报错**：返回"工单查询失败：工单 Agent 未接入工具层"。
- **JSON-RPC**：`tools/list` 正常、`ping` 返回 `{"status":"ok"}`、未知方法返回 `-32601`、未鉴权时 `tools/call` 同样被拦、已鉴权时执行成功。
- **知识检索接真实索引**：`knowledge_search` 命中 `refund_policy.md`（score 0.5），不再是假文档。

### HTTP 验证（服务带 `API_KEYS` 启动）

| 请求 | 结果 |
|---|---|
| `POST /mcp` 无密钥 | **401** `{"detail":"缺少 API Key"}` |
| `POST /mcp` `ping` | **200** `{"jsonrpc":"2.0","result":{"status":"ok"},"id":1}` |
| `POST /mcp` `tools/list` | **200**，返回 **7** 个工具 |
| `POST /mcp` `tools/call` 建单 | **200**，返回完整工单对象与真实工单号 |
| `POST /mcp` `tools/call` 缺必填 | **200**（协议层正常返回业务错误）`"入参校验失败: 缺少必填参数: ticket_type"` |
| `POST /mcp` 未知方法 | **200** `{"error":{"code":-32601,...}}` |
| `POST /api/tools/call`（另一入口） | **200**，同一工具、同一结果 |
| **落库确认** | 用 `SqlTicketStore` 直读 `.data/app.db`，`u-mcp` 名下**确有 1 条工单**，字段完整 |

> 过程中一次 `tools/call` 解析失败是 **PowerShell 嵌套引号破坏了 JSON body**，改用 `--data-binary @文件` 后全部正常——属测试方法问题，非代码缺陷。

### 回归与静态检查

- 阶段 0～3 的五个验证脚本重跑均为 `ALL PASS`（`verify_step01.py` 同步改为注入工具层，使工单分支测的是真实路径）。
- `agents`、`memory`、`tool_server`、`tracing`、`settings`、`config`、`domain`、`persistence`、`security`、`api.main` 全部导入成功；`compileall` 通过。

## 未解决的问题

- 工单类工具的 `requires_auth` 在**未启用鉴权的本机模式**下会被拒绝（调用方是 `ANONYMOUS`）。这是安全默认值带来的结果：本机模式可以直接聊天（Agent 内部传 `authenticated=True`），但**不能通过 HTTP 直接调用工单工具**。若希望本机模式也能直接调工具，需要显式放宽。
- `order_query` 与 `risk_check` 仍是演示数据（前者已在描述中标注，后者是按金额分级的简单规则）。
- **未实现真正的 MCP 传输层**：仍是进程内调用 + 一个 HTTP JSON-RPC 入口，没有 stdio/SSE、没有 `initialize` 握手与 capabilities 协商，也没有 resources/prompts 能力。当前定位是"MCP 的工具子集 + 一个可用的 JSON-RPC 入口"。
- `validate_arguments` 只覆盖 `required`/`type`/`enum`，不支持 `oneOf`、`format`、嵌套对象校验等；未声明参数被静默忽略（由 handler 默认值兜底），没有做"拒绝未知参数"。
- 工具层没有独立的超时控制，超时依赖上层 `REQUEST_TIMEOUT_SECONDS`。
- 工具调用日志有长度上限，但**没有持久化**，重启即丢。
