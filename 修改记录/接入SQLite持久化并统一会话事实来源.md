# 接入SQLite持久化并统一会话事实来源

改造计划阶段 2：真持久化。目标是让"重启即丢"的三样东西（工单、多轮对话、知识库）都落盘。

## 修改内容

### 1. 抽出工单领域规则 `python-impl/domain/ticket.py`

工单状态、优先级、状态流转表、各类文案标签、工单号生成与流转校验原先都在 `agents/ticket_handler.py` 里。现在有了数据库实现，如果两边各写一份校验就会行为漂移，因此抽成独立领域模块 `domain/ticket.py`，内存实现与数据库实现共用 `check_transition()`。

### 2. 新增持久化层 `python-impl/persistence/`

- `database.py`：`Database` 薄封装（SQLAlchemy 2.0 + SQLite），负责建库建表、事务性会话、显式关闭。上层不直接接触引擎。
- `models.py`：`TicketRecord` ORM 模型，`ticket_id` 主键，`type`/`status`/`user_id` 建索引；`to_dict()` 输出的字段与内存实现**完全一致**，保证两种存储可互换。
- `ticket_store.py`：`SqlTicketStore`，接口与 `agents.ticket_handler.TicketStore` 一致（`create` / `query` / `query_by_user` / `update_status`）。

内存实现保留为**测试替身**与无数据库场景的降级选项，不是被弃用的冗余代码。

### 3. 工单落库

`create_supervisor_graph()` 新增 `ticket_store` 参数并注入 `TicketHandlerAgent`，由 `api/main.py` 传入 `SqlTicketStore`。此前存储是在图内部 new 出来的，无法替换，也无法持久化。

### 4. 知识库来源改为磁盘目录

- 三篇示例文档从 `api/main.py` 的硬编码字符串移到 `knowledge/*.md`（带标题与段落结构）。
- `LongTermMemory` 新增 `load_or_rebuild(kb_dir)`：源文件比索引新（或索引缺失）时重新分块并落盘，否则复用缓存——**此前从未被调用的 `save()` 与 `load_knowledge_base()` 现在真正可用**。
- `load_knowledge_base()` 原先只扫 `*.txt`，现在同时支持 `*.md`。
- **分块改为段落感知**：按空行切段再贪心装箱，单个超长段落保持完整、不做句中断裂；`overlap` 仍用于让相邻块共享尾部，避免关键信息正好落在边界。

### 5. 图检查点改用 SQLite

`create_supervisor_graph()` 新增 `checkpointer` 参数；`api/main.py` 用 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` 挂在 `lifespan` 的异步上下文里，服务运行期间保持连接，退出时自动关闭。多轮对话状态因此**跨重启保留**。

### 6. 统一会话事实来源

`/api/history/{session_id}` 改为：**优先返回短期记忆**（带时间戳、可过期、可走 Redis 的缓存），**未命中时回退到图检查点**（持久化、与模型看到的上下文一致），并在响应中带 `source` 字段说明数据来自哪一侧。这样"两套记录对不上"的问题被收敛为明确的"缓存 + 权威来源"关系，而不是两个互不相干的存储。

### 7. 配置与依赖

- 新增配置项：`DATABASE_PATH`、`CHECKPOINT_DB_PATH`、`KNOWLEDGE_DIR`（`KNOWLEDGE_INDEX_PATH` 沿用）。
- `requirements.txt` 新增 `langgraph-checkpoint-sqlite`、`aiosqlite`、`SQLAlchemy`；`.env.example` 按"模型 / 持久化 / 会话与记忆 / 服务 / 工具与可观测"重新分组。

## 修改原因

用户要求按七阶段计划改造项目。阶段 2 是"从演示到项目"的关键一步：此前工单存进程内存、图检查点在内存、知识库每次启动重灌硬编码字符串，服务重启后数据全丢，这在面试中是最容易被质疑的一点。

## 验证结果

### 持久化专项验证（`.work/verify_step2.py`，不发真实模型请求）— `ALL PASS`

- **工单跨"进程"持久化**：用第一个 `Database` 实例建单并流转到 `processing`，`dispose()` 后新建 `Database` 实例读同一个库文件，**仍能查到工单**，且 `summary`/`status`/`user_id` 字段完整保留；按用户查询可用；未知工单返回 `None`。
- **两种存储接口一致**：`SqlTicketStore` 与内存 `TicketStore` 返回的字段集合完全相同；两者都遵守状态流转规则（非法流转均被拒）。
- **知识库装载**：首次 `load_or_rebuild` 返回 `{'rebuilt': True, 'documents': 2, 'sources': 2}`——只读取了 `.md`/`.txt` 两个文件、忽略了同目录的 `.json`；索引文件已落盘。第二次在**新实例**上装载返回 `{'rebuilt': False, ...}`，说明复用了缓存、未重复分块。
- **检索可用**：分块后"退款 几天"命中 `refund.md`、"开户需要什么材料"命中 `account.md`。
- **分块行为**：`chunk_size=12, overlap=2` 下产出 2 个块、均非空且不超过 `chunk_size + overlap`。
- **表结构**：`tickets` 表存在，`ticket_id` 为主键，`user_id` 有索引，关键列齐全。

### 重启持久化验证（真实模型 1 轮对话）

- 删除 `.data` 下的数据库与索引文件后启动服务，`POST /api/chat` 返回 HTTP 200、`intent=knowledge_rag`；此时 `GET /api/history` 返回 `source=cache`、2 条消息（短期记忆命中）。
- **完全停止服务**，再重新启动：
  - `GET /api/history` 返回 **`source=checkpoint`、2 条消息**，内容为 `user: 购买后几天内可以申请退款？` 与 `assistant: 用户在购买后**7天内**可以申请无理由退款…`——**多轮对话状态确实跨重启保留了**。
- 落盘文件确认：`.data/app.db`（24KB）、`.data/checkpoints.sqlite`（含 WAL）、`.data/knowledge/index.meta.json`。

### 回归与静态检查

- 阶段 0/1 的四个验证脚本（`verify_step01`、`verify_step02`、`verify_step1`、`verify_step2`）重跑均为 `ALL PASS`。其中 `verify_step01.py` 的导入路径因领域规则迁移做了同步修正（脚本在忽略目录，不入库）。
- `agents`、`memory`、`tool_server`、`tracing`、`settings`、`config`、`domain`、`persistence`、`api.main` 全部导入成功；`compileall` 通过。
- 依赖升级后（`langgraph-checkpoint` 4.3.0 → 4.3.1）**先跑了回归再改代码**，确认升级本身没有破坏已有功能。

## 未解决的问题

- **日志未配置**：`logger.info("知识库装载完成: ...")` 在 uvicorn 默认日志配置下**不会输出**（根 logger 级别为 WARNING），本次只能通过脚本验证索引复用行为，未能从服务日志确认。日志规范化安排在阶段 5。
- **未引入 Alembic 迁移**：当前用 `Base.metadata.create_all()` 建表，表结构变更没有版本管理。虽然 `alembic` 已安装，但按"不过早引入"的原则暂缓。
- **SQLite WAL 文件较大**（`checkpoints.sqlite-wal` 156KB）且没有 checkpoint/vacuum 策略；长期运行需要评估。
- 知识库检索仍是**词法匹配**，未做向量检索（按既定计划不做）；分块对**单个超长段落不做切分**（保持完整性，但可能超过 `chunk_size`）。
- `overlap` 的尾部拼接是**按字符**截取的，所以重叠块可能以半句话开头（段落边界只保证块的首尾不切断，不保证重叠部分完整）。
- MCP 工具层的 `ticket_create` 仍是演示数据、未接数据库（阶段 4）。
- 接口仍**无鉴权与限流**（阶段 3）。
- 短期记忆与检查点仍可能存在**内容差异**：检查点会按 `max_context_messages` 裁剪历史，而短期记忆按 `max_turns` 保留，两者窗口不同，`/api/history` 命中缓存时返回的条数可能多于检查点。
- 本次为安装 `langgraph-checkpoint-sqlite` 需要给虚拟环境引导 pip，该操作被沙箱拦在临时目录，**经一次提权重试后成功**；`.venv` 中的 pip 此前并不存在。
