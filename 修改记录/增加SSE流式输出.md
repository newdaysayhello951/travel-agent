# 增加SSE流式输出

改造计划阶段 6 的后半部分（流式输出）。README 架构定性仍未完成，见文末。

## 修改内容

### 1. 新增 `POST /api/chat/stream`（Server-Sent Events）

用 `graph.astream(..., stream_mode="updates")` 逐节点推送进度：

- `event: start` —— 立即返回，携带 `session_id`
- `event: node` —— 每执行完一个节点推一帧（`{"node": "supervisor_route"}` 等）
- `event: done` —— 携带最终回复、意图、合规结果
- `event: error` —— 超时或上游异常时推送，**不再回传第三方异常原文**

这样在一次问答要跑 5 次模型调用的场景下，前端可以持续显示"路由中 → 检索中 → 审查中"，而不是干等将近十秒。

响应头带 `Cache-Control: no-cache` 与 `X-Accel-Buffering: no`（后者用于避免反向代理缓冲导致"流式变成一次性返回"）。

### 2. 抽出两处共用逻辑，避免流式/非流式两条路径行为漂移

- `_initial_state(text, user_id, session_id)`：构造图调用的初始状态，两个聊天接口共用；
- `_check_request(principal, message)`：限流 + 输入侧检查，两个接口共用。

抽出来的直接收益是：**新增的流式接口自动继承了鉴权、限流、输入检查**，不会出现"为了加功能而绕过安全防线"。

### 3. 落库时机与流式解耦

短期记忆的写入放在流**末尾**（`done` 之前），保证只有整轮跑完才落库，与阶段 1 确立的"成功才记录"规则一致。

## 修改原因

用户要求按七阶段计划改造项目。阶段 6 的目标是让这个系统在能力上"名副其实"：先补上合规修正循环（图里有了环），再补上流式输出（`/api/chat` 一次性返回，用户最长要干等近 3 分钟）。

## 验证结果

- 全部模块导入成功；**`pytest` 80 passed**（本次未新增用例，见未解决问题）。
- **SSE 真实逐帧验证**（真实启动 + 真实模型 + `curl -N`），事件到达时间如下：

| 时间 | 事件 |
|---|---|
| 0.10s | `event: start` |
| 3.39s | `event: node` → `supervisor_route` |
| 4.84s | `event: node` → `knowledge_rag` |
| 7.05s | `event: node` → `compliance_check` |
| 7.07s | `event: node` → `synthesize` |
| 7.07s | `event: done`（携带最终回复） |

  事件确实是**逐帧到达**的（首帧 0.10s、末帧 7.07s），证明流式生效而非一次性返回。
- **鉴权继承**：不带密钥请求 `/api/chat/stream` 返回 **401** `{"detail":"缺少 API Key"}`。

## 未解决的问题

### ⚠️ 本次发现一个尚未定位的矛盾（如实记录，不作猜测）

在上述 SSE 真实请求中，最终回复是**兜底话术**：

```
"抱歉，知识库中暂未找到与您问题相关的信息。建议您联系人工客服获取帮助。"
```

这个字符串来自 `knowledge_rag.generate_answer()` 的"无检索结果"分支，说明该次请求里 `context_docs` 为空。但同一时刻的对照验证显示：

- 服务启动日志确认知识库装载正常：`{'rebuilt': False, 'documents': 3, 'sources': 3}`；
- 用同一个索引文件直接调用 `LongTermMemory.search('购买后几天内可以申请退款？')` **能命中** `refund_policy.md`（score 0.636）与 `account_guide.md`。

按代码路径推断，`retrieve_documents` 返回空才会走到该兜底分支，而 rerank 在非空输入下不会清空结果——因此**这两种观察互相矛盾，根因未定位**。下一步需要：在真实请求上打印 `raw_docs` / `reranked_docs` 的长度与重排模型的原始输出，确认空结果发生在检索还是重排环节。

**在定位之前，不能宣称知识问答链路是可靠的。**

### 其他

- **本次未为 SSE 增加 pytest 用例**：流式响应需要 `httpx.ASGITransport` 或真实启动，而 `api/main.py` 在导入时即固化配置（`settings` 与全局对象），测试内改环境变量不生效。需要先引入应用工厂或依赖覆盖（与阶段 5 遗留的 HTTP 层测试缺口是同一个问题）。
- **README 的架构定性仍未更新**：文档里还没有把系统明确写成 "routing workflow + evaluator-optimizer"，也没画出含环的图。
- 流式接口**没有做逐 token 输出**，粒度是"每个节点一帧"。真正的 token 级流式需要 `stream_mode="messages"` 并在前端做增量拼接。
- 前端页面**尚未接入流式接口**，仍在用非流式的 `/api/chat`。
- 流式响应期间会**一直占用一个并发位**（`concurrency_gate` 在 `event_source` 内持有），长连接多时更容易触顶；目前没有针对流的独立超时或最大时长限制（沿用 `REQUEST_TIMEOUT_SECONDS`）。
- 客户端中途断开时，图执行是否被取消取决于 `StreamingResponse` 的取消传播，本次未验证。
