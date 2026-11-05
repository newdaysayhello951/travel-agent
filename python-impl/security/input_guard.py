"""输入侧检查。

合规审查只覆盖**输出**，恶意或越权的输入不会被拦截。这里补齐输入侧：
- 业务长度上限（比 Pydantic 的硬上限更严，避免把超长文本喂给模型）
- 明显的提示注入 / 越权指令（试图覆盖系统规则、套取系统提示词）

只做确定性判断，不调用模型：拦不住的复杂情形交给输出侧合规审查兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class InputCheckResult:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # 便于 `if not result:` 的写法
        return self.allowed


# 提示注入 / 越权指令的典型特征
_INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"忽略(以上|上面|之前|先前)(的)?(所有)?(指令|要求|规则|设定)", "试图覆盖系统指令"),
    (r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions", "试图覆盖系统指令"),
    (r"(输出|告诉我|重复|打印|展示).{0,6}(你的)?(系统)?(提示词|prompt|指令原文)", "试图套取系统提示词"),
    (r"(reveal|show|print|repeat).{0,12}(system\s+)?prompt", "试图套取系统提示词"),
    (r"(你现在是|从现在起你是|扮演).{0,10}(管理员|root|开发者模式|DAN)", "试图切换角色越权"),
    (r"(绕过|跳过).{0,8}(合规|审查|风控|限制)", "试图绕过合规审查"),
)

_COMPILED_INJECTION = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in _INJECTION_PATTERNS
)


def inspect_input(text: str, max_chars: int = 2000) -> InputCheckResult:
    """检查用户输入；返回是否放行与拒绝原因。"""
    stripped = (text or "").strip()
    if not stripped:
        return InputCheckResult(False, "输入为空")

    if len(stripped) > max_chars:
        return InputCheckResult(False, f"输入过长，请控制在 {max_chars} 字以内")

    for pattern, reason in _COMPILED_INJECTION:
        if pattern.search(stripped):
            return InputCheckResult(False, reason)

    return InputCheckResult(True)
