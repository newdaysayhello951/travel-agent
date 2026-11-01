"""从项目根目录启动 Python 客服网页。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'python-impl'))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('api.main:app', host='127.0.0.1', port=8001)
