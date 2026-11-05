# 补齐CI容器化与工程文档

改造计划阶段 7 的第一批：工程交付。让这个仓库从"能跑的代码"变成"能被别人接手的项目"。

## 修改内容

### 1. GitHub Actions CI（`.github/workflows/ci.yml`）

push 到 `main` 与 PR 时自动执行：安装 `requirements-dev.txt` → `compileall` 语法检查 → `pytest`。

测试用 `FakeLLM` 替代真实模型，**不需要密钥、不消耗模型额度**，因此 CI 无需配置任何 Secret。

### 2. 容器化（`Dockerfile` + `docker-compose.yml`）

- `Dockerfile`：基于 `python:3.12-slim`，**先只拷贝依赖清单以复用构建缓存**，再拷贝源码；内置 `HEALTHCHECK` 探测 `/health`。
- `docker-compose.yml`：`app` + 可选 `redis`；业务数据与检查点落在命名卷里（重建容器不丢）；`knowledge/` 以只读方式挂载。
- 容器内 `HOST=0.0.0.0`，并在文件里**明确注释**：监听非本机地址时若未配置 `API_KEYS`，服务会按安全默认值拒绝启动。这是阶段 3 定下的策略，容器化后依然生效——**不会因为打包而绕过安全约束**。

### 3. 架构决策记录 `docs/架构决策记录.md`

把改造过程中的 10 个关键取舍整理成 **决策 / 背景 / 权衡 / 结论** 四段式：图编排选型、`sub_results` 不加 reducer、词法 vs 向量检索、两阶段合规与 fail-closed、结构化输出必须显式 `function_calling`、并发闸门分桶、token 计量用代理、日志初始化时机、上下文按偶数条裁剪、测试用假模型。

其中 ADR-005（结构化输出的网关差异）与 ADR-008（uvicorn 覆盖 handler）都是**只有真实运行才会暴露**的问题，单测与静态检查发现不了。

### 4. 面试材料 `docs/面试材料.md`

包含：一句话介绍（业务 → 架构 → 技术栈三段式）、三条可写的核心工作、**8 条高频追问的标准答法**、**必须主动说出的能力边界表**、以及简历自检三问。

---

## 修改原因

用户要求按七阶段计划改造项目。阶段 7 的目标是工程交付：一个能被别人接手、能在 CI 上自动验证、能打包运行、并且有决策记录的项目，与"只有代码能跑"是两个层次。

## 验证结果

- **CI 将执行的两条命令本地预演通过**：
  - `python -m compileall -q python-impl` → 通过（无输出即无语法错误）；
  - `python -m pytest -q` → **87 passed**。
- **YAML 语法校验**：`.github/workflows/ci.yml` 与 `docker-compose.yml` 均能被 `yaml.safe_load` 正常解析。
- **Dockerfile 关键指令检查**：`FROM python:3.12-slim`、`COPY requirements.txt`、`HEALTHCHECK`、`CMD [` 均存在。
- 全部模块导入成功。

## 未解决的问题

- **CI 未在 GitHub 上真实运行过**：只本地预演了它要执行的命令，未验证工作流在 GitHub 运行器上的实际结果（例如 `actions/setup-python` 的缓存行为）。
- **镜像未构建验证**：本机没有 docker（`docker` 命令不存在），`Dockerfile` 与 `docker-compose.yml` **只做了语法与内容检查，未执行过 `docker build` / `docker compose up`**。因此"容器化可用"目前**不能算已验证**。
- `docker-compose.yml` 需要 `.env` 中存在 `API_KEYS`，否则容器会因安全默认值拒绝启动；这一点写在文件注释里，但**未实际跑过一次完整启动**。
- compose 里 `depends_on` 用了 `condition: service_healthy`，该语法在部分旧版本 compose 中不支持，未验证。
- 本批次未补 **HTTP 层自动化测试**（401 / 429 / `/mcp` / SSE）——这仍是最大的技术债，此前排查已证明手工验证不可靠。
- `docs/架构决策记录.md` 与 README 的"关键设计决策"表存在**内容重叠**，尚未决定是合并还是保留两处（当前是：README 给速览，ADR 给完整推演）。
- 本次校验脚本自身又出了一次误报：`yaml.safe_load` 会把 `on:` 解析成布尔 `True`，我随后对混合类型的键调用了 `sorted()` 导致 `TypeError`。**误报来自校验代码，不是 YAML 本身**——与之前那次"验证工具制造幽灵缺陷"是同一类问题。
