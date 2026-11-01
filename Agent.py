"""
智能旅行助手 —— 入口。

用法:
    python Agent.py                                  # 用内置示例需求，流式输出
    python Agent.py "成都3日游，2026年6月1日-6月3日"    # 自定义需求
    python Agent.py --no-stream "成都3日游"           # 非流式输出
"""
import argparse
import asyncio
from config import CONFIG
from agents.planner import TripPlanner
from render import format_plan_cli
from schemas import PlanValidationError
from reliability import ExecutionLimitError


# 不传参数时使用的默认示例
DEFAULT_INPUT = (
    "长沙3日游，2026年5月21日-2026年5月23日，"
    "喜欢自然风光和历史文化，中等预算，住五一广场"
)


# ==================== 演示 ====================

async def demo_stream(planner: TripPlanner, user_input: str):
    """流式输出演示 —— 实时打印 token，结束后渲染格式化计划"""
    print("=" * 60)
    print(f"🚀 正在为您规划旅行...\n输入: {user_input}\n")
    print("=" * 60)

    buffer = ""
    async for token in planner.stream(user_input):
        print(token, end="", flush=True)
        buffer += token

    # 格式化渲染
    formatted = format_plan_cli(buffer)
    if formatted:
        print(formatted)

    print("=" * 60)
    print("✅ 旅行计划生成完毕")


async def demo_invoke(planner: TripPlanner, user_input: str):
    """非流式输出演示"""
    print("=" * 60)
    print(f"🚀 正在为您规划旅行...\n输入: {user_input}\n")
    print("=" * 60)

    result = await planner.invoke(user_input)
    formatted = format_plan_cli(result)
    if formatted:
        print(formatted)
    else:
        print(result)

    print("=" * 60)
    print("✅ 旅行计划生成完毕")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智能旅行助手 CLI")
    parser.add_argument(
        "query", nargs="?", default=DEFAULT_INPUT,
        help="旅行需求（自然语言）。不传则用内置示例。",
    )
    parser.add_argument(
        "--no-stream", action="store_true",
        help="关闭流式输出（一次性返回完整结果）",
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    llm = CONFIG.create_llm()
    planner = TripPlanner(llm)

    if args.no_stream:
        await demo_invoke(planner, args.query)
    else:
        await demo_stream(planner, args.query)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (PlanValidationError, ExecutionLimitError, TimeoutError) as exc:
        print(f"规划失败：{exc or '执行超时，请缩小规划范围后重试'}")
        raise SystemExit(1)
