"""无明确结构化请求时，先做无工具的信息澄清；不替用户选择目的地。"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class IntakeDecision(BaseModel):
    intent: Literal['plan','weather','other']
    city: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    budget_limit: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    conflict: bool | None = False
    stay_on_last_day: bool | None = False
    message: str | None = Field(default='', max_length=1500)


def clarification(decision: IntakeDecision, query: str):
    if decision.intent == 'other':
        return '我可以帮你规划旅行或查询天气，请说明目的地和需求。'
    # 要求城市在输入中有字面证据，避免模型凭空挑选目的地。
    if not decision.city or not decision.city.strip() or not (decision.city in query or (len(decision.city)>1 and decision.city.endswith('市') and decision.city[:-1] in query)):
        return '请先告诉我目的地城市；如果需要行程规划，也请提供出发和结束日期。'
    if decision.start_date and decision.end_date and decision.end_date < decision.start_date:
        return '出发与结束日期存在先后冲突：结束日期早于出发日期，请修正日期。'
    if decision.start_date and decision.end_date and (decision.end_date-decision.start_date).days > 30:
        return '单次最多规划31天，请缩短日期范围。'
    if decision.conflict:
        return decision.message or '当前需求存在冲突，请确认预算与住宿等要求后再规划。'
    if decision.intent == 'plan' and (not decision.start_date or not decision.end_date):
        return '请提供明确的出发和结束日期，再为你规划行程。'
    return None
