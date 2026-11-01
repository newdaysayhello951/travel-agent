"""行程结构和可确定的业务规则；不负责验证实时价格真实性。"""
from datetime import date, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class TripRequest(BaseModel):
    city: str = Field(min_length=1)
    start_date: date
    end_date: date
    budget_limit: Money | None = None
    required_attractions: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid_dates(self):
        if not self.city.strip():
            raise ValueError('目的地不能为空')
        if self.end_date < self.start_date:
            raise ValueError('结束日期不能早于开始日期')
        if (self.end_date - self.start_date).days > 30:
            raise ValueError('单次最多规划31天')
        return self


class PlanItem(BaseModel):
    model_config = ConfigDict(extra='allow')
    name: str = Field(min_length=1)


class Attraction(PlanItem):
    ticket_price: Money


class Hotel(PlanItem):
    estimated_cost: Money


class Meal(PlanItem):
    type: str
    estimated_cost: Money


class DayPlan(BaseModel):
    model_config = ConfigDict(extra='allow')
    date: date
    day_index: int = Field(ge=0)
    hotel: Hotel
    attractions: list[Attraction] = Field(min_length=1)
    meals: list[Meal]

    @model_validator(mode='after')
    def three_meals(self):
        if sorted(m.type for m in self.meals) != ['breakfast', 'dinner', 'lunch']:
            raise ValueError('每天必须且只能包含早中晚三餐')
        return self


class Budget(BaseModel):
    total_attractions: Money
    total_hotels: Money
    total_meals: Money
    total_transportation: Money
    total: Money


class WeatherInfo(BaseModel):
    model_config = ConfigDict(extra='allow')
    date: date


class TripPlan(BaseModel):
    model_config = ConfigDict(extra='allow')
    city: str = Field(min_length=1)
    start_date: date
    end_date: date
    days: list[DayPlan] = Field(min_length=1, max_length=31)
    budget: Budget
    weather_info: list[WeatherInfo] = Field(default_factory=list)
    overall_suggestions: str

    @model_validator(mode='after')
    def consistency(self):
        span = (self.end_date - self.start_date).days
        if not 0 <= span <= 30:
            raise ValueError('行程日期范围无效或超过31天')
        expected = [self.start_date + timedelta(days=i) for i in range(span+1)]
        if [d.date for d in self.days] != expected:
            raise ValueError('每日日期必须按序完整覆盖起止日期，不能重复或遗漏')
        if [d.day_index for d in self.days] != list(range(len(expected))):
            raise ValueError('day_index必须从0连续递增')
        b = self.budget
        sums = [sum(a.ticket_price for d in self.days for a in d.attractions),
                sum(d.hotel.estimated_cost for d in self.days),
                sum(m.estimated_cost for d in self.days for m in d.meals)]
        declared = [b.total_attractions, b.total_hotels, b.total_meals]
        if any(abs(a-b) > 0.02 for a,b in zip(sums,declared)):
            raise ValueError('景点、酒店或餐饮明细与预算小计不一致')
        if abs(sum(declared)+b.total_transportation-b.total) > 0.02:
            raise ValueError('预算总额与分类小计不一致')
        dates = [w.date for w in self.weather_info]
        if len(set(dates)) != len(dates) or any(x not in set(expected) for x in dates):
            raise ValueError('天气日期重复或不属于旅行日期')
        return self


class PlanValidationError(ValueError):
    """输出尚未达到可展示为正式行程的要求。"""


def validate_plan_text(text: str, request: TripRequest | None = None) -> dict | None:
    from render import parse_plan
    data = parse_plan(text)
    if data is None:
        if request is not None:
            raise PlanValidationError('未返回结构化行程，请重试或补充需求')
        return None  # 允许天气问答或补充信息追问
    try:
        plan = TripPlan.model_validate(data)
    except ValueError as exc:
        raise PlanValidationError('行程结构或预算校验失败，请重新规划') from exc
    if request:
        if (plan.city, plan.start_date, plan.end_date) != (request.city, request.start_date, request.end_date):
            raise PlanValidationError('目的地或日期与请求不一致')
        if request.budget_limit is not None and plan.budget.total > request.budget_limit:
            raise PlanValidationError('行程预算超出限制')
        names = {a.name for d in plan.days for a in d.attractions}
        if not set(request.required_attractions) <= names:
            raise PlanValidationError('行程遗漏必去景点（按名称精确匹配）')
    return plan.model_dump(mode='json')
