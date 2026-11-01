# 智能旅行规划助手

基于单个旅行 Agent，直接调用高德 MCP 的地点搜索、详情、天气和路线工具。提供 Streamlit 页面、命令行和流式输出。

## 当前架构

```text
Streamlit / CLI → TripPlanner（单 Agent）→ OpenAI 兼容网关或 DashScope
                        ↓
                  高德 MCP 工具
                        ↓
                 JSON 行程解析与展示
```

技术栈：Python 3.12、LangChain、LangGraph（create_agent 底层运行时）、MCP、AsyncIO、Streamlit、Pydantic、python-dotenv。

## 启动

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，填写自己的模型和高德凭据
.\.venv\Scripts\python.exe Agent.py --no-stream "长沙两日游，2026-11-10至2026-11-11"
.\.venv\Scripts\python.exe -m streamlit run app.py
```

不要提交 `.env`。网关、模型、密钥由使用者自行配置；C++ 等其他服务也不能自动获得这些环境变量。

## 本地验证与基线

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe evals/run.py --ids 1 --output .work/single-baseline.json
# 全部案例会真实调用模型及地图服务，产生费用与配额消耗
.\.venv\Scripts\python.exe evals/run.py --ids 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
```

`evals/cases.json` 包含 15 条固定需求。脚本记录耗时、调用次数、供应方上报的 token 量和是否可解析；这些指标不等于业务正确性。完整输出保存在忽略的 `.work/` 中。

旧多 Agent 实现保存在 Git 提交 `5ca1796`，可用该版本复现对比；原架构说明见 `docs/原多智能体架构.md`，仅作为历史资料。对比应保持模型、案例和配置一致，且重复运行，不能从单次结果断言性能优势。

## 行程校验与执行边界

- Pydantic 校验每日日期覆盖、连续序号、三餐、非负费用、预算明细/小计/总额一致性，以及天气日期范围。
- 页面传入 `TripRequest`，额外检查目的地和日期一致。程序调用 `invoke/stream` 可传入预算上限、必去景点；必去景点暂按名称精确匹配。自然语言中的预算和偏好尚未自动转换为这些硬约束。
- CLI 通用问答允许普通文本；传入明确的 `TripRequest` 时必须返回有效行程。错误行程抛出 `PlanValidationError`，目前不自动改写或修复业务错误。
- 流式内容是预览，结束后才校验；页面校验失败时不保存失败结果。调用方必须处理结束阶段的异常，不能将已收到片段直接视为成功。
- 工具超时默认20秒，最多重试1次；仅超时、连接错误及 OSError 自动重试。工具返回的业务错误不重试，参数错误和取消不重试。
- 每次运行最多24次实际工具尝试（包含重试），整体180秒（包含工具初始化），图执行最多40步。图步数不是精确的模型调用次数。配置范围在启动时检查，变量见 `.env.example`。
- 工具重试耗尽后返回安全错误消息，明确禁止编造；达到次数上限则终止。并行工具共享本次运行预算，不跨请求累计。

## 当前边界

- 暂未实现 FastAPI、持久化会话、多轮修改和 RAG。
- 天气真实性、景点开放时间、交通时间冲突和估算价格是否合理仍需要进一步校验；当前结构及预算一致性检查不能保证旅行可执行。
- 如仅在受限执行环境出现 SSL / pywin32 DLL 导入失败，先在普通终端运行相同命令核实，再判断是否需要重装依赖。
- 本机适配说明和历史文档记录旧实现，当前入口及架构以本 README 和代码为准。

后续提交规则见 `AGENTS.md`，中文修改说明存放于 `修改记录/`。
