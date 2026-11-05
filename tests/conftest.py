"""pytest 共享夹具。

测试**不调用真实模型**：用 `FakeLLM` 按提示词内容返回固定结果，
既能跑通整张图，也不消耗模型额度。

临时目录用自定义的 `tmp_dir` 而不是 pytest 内置 `tmp_path`：后者依赖系统临时目录，
在受限环境下创建与清理都会失败（已在 pytest.ini 中禁用 tmpdir 插件）。
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python-impl"))

from agents.supervisor import create_supervisor_graph  # noqa: E402
from agents.ticket_handler import TicketStore  # noqa: E402
from memory.long_term import LongTermMemory  # noqa: E402
from memory.working_memory import WorkingMemory  # noqa: E402
from tool_server.mcp_server import MCPToolServer, create_default_tools  # noqa: E402

from helpers import FakeLLM  # noqa: E402

TMP_ROOT = ROOT / ".work" / "test-tmp"


@pytest.fixture
def tmp_dir() -> Path:
    """每次测试一个独立的工作区临时目录，结束后清理"""
    path = TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def ticket_store() -> TicketStore:
    return TicketStore()


@pytest.fixture
def long_term_memory(tmp_dir) -> LongTermMemory:
    memory = LongTermMemory(index_path=tmp_dir / "index")
    memory.add_document("退款政策：用户在购买后7天内可申请无理由退款。", source="refund_policy.md")
    memory.add_document("开户流程：准备身份证原件、填写申请表、视频认证。", source="account_guide.md")
    return memory


@pytest.fixture
def tool_server(ticket_store, long_term_memory) -> MCPToolServer:
    return create_default_tools(
        MCPToolServer(max_call_log=50),
        ticket_store=ticket_store,
        long_term_memory=long_term_memory,
    )


@pytest.fixture
def graph(fake_llm, tool_server, long_term_memory):
    return create_supervisor_graph(
        llm=fake_llm,
        working_memory=WorkingMemory(),
        long_term_memory=long_term_memory,
        tool_server=tool_server,
        max_context_messages=6,
    )
