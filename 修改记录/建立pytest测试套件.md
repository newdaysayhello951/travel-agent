# 建立pytest测试套件

改造计划阶段 5 的**前半部分**（测试）。可观测性部分（日志配置、token 计量、per-agent 指标）尚未完成，见文末"未解决的问题"。

## 修改内容

### 1. 新增 `tests/` 测试套件（61 个用例）

| 文件 | 覆盖内容 |
|---|---|
| `tests/helpers.py` | `FakeLLM`（按提示词内容分发，覆盖图中所有模型调用点）与状态构造工具 |
| `tests/conftest.py` | 夹具：假模型、内存工单存储、长期记忆、工具服务端、已编译的图 |
| `tests/test_graph.py` | 图结构、路由（结构化/回退/兜底）、合规闸门、上下文窗口、多轮、状态重置 |
| `tests/test_tickets.py` | 领域规则（合法/非法/幂等/终态）、存储 CRUD、工具层（鉴权/校验/JSON-RPC）、Agent 走工具层 |
| `tests/test_security.py` | 鉴权策略三种场景、令牌桶（突发/分桶/补充/上限）、并发闸门、输入侧检查 |
| `tests/test_persistence.py` | 工单跨连接持久化、两种存储接口一致、表结构、知识库索引复用、分块、幂等 |

**全部测试不发真实模型请求**：`FakeLLM` 按提示词内容返回固定结果即可跑通整张图，因此测试可离线、可重复、不消耗模型额度。

### 2. 关键断言（这些是"防回归"的核心）

- 图结构固定为 5 个节点、条件边只有 2 个合法去向；
- 合规不通过时正文被替换、**解析失败必须 fail-closed**（不能默认放行）；
- 上下文窗口裁剪后消息条数为 `[2,4,6,6,6]`（不裁剪会是 10），且**裁剪后仍是问答成对结构**；
- `sub_results` **每轮重置**，不把上一轮结果带进本轮；
- 工单 Agent 的四个操作**确实经由工具层**（用工具调用日志断言，而不是看返回值）；
- 非法状态流转被拒绝、`closed` 是终态、重复设置同状态幂等；
- 工具入参校验拦截缺必填/类型不符/**布尔冒充整数**/枚举越界；
- `bool` 是 `int` 子类这一 Python 陷阱有专门用例；
- 不同调用方并发不互相阻塞、同一调用方仍被串行化。

### 3. 测试基础设施

- `pytest.ini`：`asyncio_mode = auto`，并**禁用 tmpdir 插件**。
- 自定义 `tmp_dir` 夹具替代 pytest 内置 `tmp_path`：后者依赖系统临时目录，在受限环境下创建与清理都会失败。
- `requirements-dev.txt`：开发依赖（`pytest`、`pytest-asyncio`）。
- `.gitignore` 增加 `.pytest_tmp/`、`.pytest_cache/`、`pytest-cache-files-*/`。

## 修改原因

用户要求按七阶段计划改造项目。阶段 5 的目标是"有测试"——此前项目里 `tests/` 目录根本不存在，全部验证依赖临时脚本与手工请求。面试中"你有什么测试"是很常见的问题，能当场跑出一套通过的测试是最直接的可信度证明。

## 验证结果

- **`pytest` 全量通过：61 passed in 3.48s**。
- 阶段 0～4 的六个验证脚本重跑均为 `ALL PASS`。
- `agents`、`memory`、`tool_server`、`tracing`、`settings`、`config`、`domain`、`persistence`、`security`、`api.main` 全部导入成功；`compileall` 通过。
- 临时目录与缓存目录已确认未被 git 跟踪。

### 过程中遇到的问题（已解决，记录以便复用）

1. **`tmp_path` 全部报错**：pytest 默认把临时目录建在系统临时区，受限环境下 `PermissionError WinError 5`。改用 `--basetemp` 指向项目内目录后，**收尾的 `cleanup_dead_symlinks()` 仍然失败**（`os.listdir` 被拒）。最终方案是**禁用 tmpdir 插件**并自建 `tmp_dir` 夹具。
2. **测试脚本被 PowerShell 改写破坏**：用 `Set-Content` 做批量替换时中文被破坏（`SyntaxError: invalid non-printable character`）。**应始终用编辑工具而不是 PowerShell 改写含中文的源码**——这个坑本次已第二次踩到。
3. 一个用例最初漏传 `arguments` 参数导致 `TypeError`，已修正。

## 未解决的问题

- **阶段 5 的可观测性部分尚未完成**：`logger.info` 在 uvicorn 默认日志配置下不可见（根 logger 级别为 WARNING）；token 消耗仍未采集（`usage_metadata` 未读取）；`/api/metrics` 的 per-agent 指标仍然为空（装饰器里没有调用 `record_call`）；OTLP 未配置采样率。这些留待阶段 5 的后半部分处理。
- 测试覆盖仍有缺口：**未测 HTTP 层**（鉴权 401、限流 429、`/mcp` 入口的端到端行为目前靠手工 curl 验证）；未测 `api/main.py` 的异常映射分支；未做覆盖率统计。
- 未接入 CI，测试只能在本地手动运行。
- 测试用的 `FakeLLM` 是手工桩，与真实模型的行为差异无法覆盖（例如真实网关对结构化输出的支持差异，就是阶段 1 由真实调用才发现的）。
- 未引入 `pytest-cov`、`pytest-mock` 等辅助插件。
- `.work/` 下的六个验证脚本仍是更完整的端到端验证手段（含真实启动与 HTTP 探测），本次未把它们全部改写成 pytest 用例，两套验证暂时并存。
