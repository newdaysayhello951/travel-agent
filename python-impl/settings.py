"""复用根目录原旅行项目环境变量，不复制或输出密钥。"""
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')


def create_llm():
    provider = os.getenv('LLM_PROVIDER', 'openai_compatible')
    if provider == 'dashscope':
        key = os.getenv('DASHSCOPE_API_KEY')
        base = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        model = os.getenv('DASHSCOPE_MODEL', 'qwen3-max')
    else:
        key = os.getenv('LLM_API_KEY') or os.getenv('OPENAI_API_KEY')
        base = os.getenv('LLM_BASE_URL') or os.getenv('OPENAI_BASE_URL')
        model = os.getenv('LLM_MODEL') or os.getenv('OPENAI_MODEL')
    if not key or not model:
        raise RuntimeError('缺少模型配置，请检查根目录 .env 中的模型名称与密钥')
    extra = {}
    if os.getenv('LLM_ENABLE_THINKING', 'false').lower() not in ('1', 'true', 'yes'):
        extra['enable_thinking'] = False
    return ChatOpenAI(model=model, api_key=key, base_url=base, temperature=0,
                      timeout=45, max_retries=0, extra_body=extra or None)
