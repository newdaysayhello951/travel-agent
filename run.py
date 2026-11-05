"""从项目根目录启动智能客服服务。

监听地址与端口统一由 config.get_settings() 决定（默认 127.0.0.1:8001），
不再在这里硬编码，避免与 api/main.py 的启动方式出现不一致。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'python-impl'))

if __name__ == '__main__':
    import uvicorn

    from config import get_settings

    settings = get_settings()
    uvicorn.run('api.main:app', host=settings.host, port=settings.port)
