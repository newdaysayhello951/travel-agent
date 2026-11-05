# 多 Agent 智能客服系统 — 运行镜像
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 先只拷贝依赖清单，让依赖层可以复用缓存
COPY requirements.txt ./
COPY python-impl/requirements.txt python-impl/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# 容器内必须监听 0.0.0.0 才能被外部访问；
# 注意：监听非本机地址时若未配置 API_KEYS，服务会按安全默认值拒绝启动。
ENV HOST=0.0.0.0 \
    PORT=8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health').read()"

CMD ["python", "run.py"]
