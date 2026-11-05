# Python 智能客服演示

基于 [bcefghj/smart-cs-multi-agent](https://github.com/bcefghj/smart-cs-multi-agent) 的 Python 实现适配，来源版本 `e045f0256d42db3c800662f60bd957d9c78374a4`，保留上游 MIT 许可证。不引入 Java、Go 部分。

## 启动

1. 安装：`python -m pip install -r requirements.txt`
2. 根目录放置 `.env`，支持 `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。现有本机 `.env` 已保留，不提交 Git。
3. 执行 `python run.py`，打开 <http://127.0.0.1:8001>。Windows 当前环境可执行 `.venv\Scripts\python.exe run.py`。

网页支持知识问答、创建和查询演示工单、新建会话。接口文档位于 `/docs`。模型请求走配置的模型网关。

## 当前实现边界

- Supervisor 选择知识问答或工单分支，再审查并返回结果。
- 知识库为三个示例文档；上游随机向量已改为中英文词法检索，**目前没有真实 Embedding 向量检索**。
- 工单、图检查点为内存数据，重启丢失；Redis可选记录聊天历史，不代表图检查点已持久化。
- MCP 工具仍是上游演示数据，没有接入真实订单、退款、人工客服。
- 仅供本机演示，无登录鉴权，默认只监听 `127.0.0.1`。
- 没有 SSE、并行 Agent 或人工审批恢复功能。上游文件中的此类说明不代表已实现。

工作目录只保留本智能客服项目、协作约定与对应修改记录。

## 阅读目录

- `python-impl/agents/`：路由、知识问答、工单与合规审查。
- `python-impl/api/`：网页及 HTTP 接口。
- `python-impl/memory/`：工作记忆、会话记录和知识检索。
- `python-impl/tool_server/`：演示工具服务。
- `python-impl/tracing/`：步骤追踪。
- `python-impl/settings.py`：读取根目录 `.env` 并创建模型。
- `python-impl/web/`：聊天页面。
- `run.py`：启动入口。

环境配置统一参考根目录 `.env.example`。`.venv` 是本机运行环境，`.work` 保存当前服务日志与验证结果，均无需阅读或提交。
