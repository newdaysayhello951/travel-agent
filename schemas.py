"""行程结构和可确定的业务规则；不负责验证实时价格真实性。"""
from datetime import date, timedelta
from typing import Annotated
import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    hotel: Hotel | None
    attractions: list[Attraction]
    meals: list[Meal]



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


class PlanValidationError(ValueError):
    """只携带字段路径和规则诊断，不包含原始输入或供应商异常。"""

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__('；'.join(f"{x['path']}: {x['message']}" for x in issues[:8]))


def issue(path, code, message):
    return {'path': path, 'code': code, 'message': message}


def reject_constant(value):
    raise ValueError('JSON不得包含NaN或Infinity')


def decode_plan_json(text):
    raw = text.strip()
    fences = re.findall(r'```(?:json)?\s*(.*?)\s*```', raw, flags=re.S)
    if len(fences) > 1:
        raise ValueError('仅允许一个JSON代码块')
    return json.loads(fences[0] if fences else raw, parse_constant=reject_constant)


def recalculate_budget(text):
    """只重算冗余汇总字段，不修改每日报价；所有改动显式返回。"""
    try:
        data = decode_plan_json(text)
        plan = TripPlan.model_validate(data)
    except (ValueError, TypeError):
        return text, []
    sums = {
        'total_attractions': round(sum(a.ticket_price for d in plan.days for a in d.attractions),2),
        'total_hotels': round(sum(d.hotel.estimated_cost for d in plan.days if d.hotel),2),
        'total_meals': round(sum(m.estimated_cost for d in plan.days for m in d.meals),2),
    }
    sums['total'] = round(sum(sums.values()) + plan.budget.total_transportation,2)
    changes = []
    for key, value in sums.items():
        before = getattr(plan.budget,key)
        if abs(before-value) > .02:
            changes.append({'path':'budget.'+key, 'before':before, 'after':value, 'reason':'由每日明细确定性加总'})
            data['budget'][key] = value
    return json.dumps(data, ensure_ascii=False), changes


def validate_repair_facts(original: str, revised: str):
    """修正器仅修结构和汇总，不能静默替换已有地点、报价和天气。"""
    try:
        old, new = decode_plan_json(original), decode_plan_json(revised)
    except (ValueError, TypeError):
        return  # 无法完整解析的草稿仍由严格结构校验处理
    protected = {'name', 'address', 'location', 'rating', 'ticket_price', 'estimated_cost', 'weather_info'}
    errors = []
    def walk(before, after, path):
        if isinstance(before, dict) and isinstance(after, dict):
            for key, value in before.items():
                here = f'{path}.{key}' if path else key
                if key in protected and after.get(key) != value:
                    errors.append(issue(here, 'fact_changed', '修正不得改动已有地点、价格或天气；需要重新查询时应停止自动修正'))
                elif key in after:
                    walk(value, after[key], here)
        elif isinstance(before, list) and isinstance(after, list):
            if len(before) != len(after):
                errors.append(issue(path, 'items_changed', '结构修正不得增删已有明细'))
            else:
                for i, (a,b) in enumerate(zip(before,after)):
                    walk(a,b,f'{path}.{i}')
    walk(old,new,'')
    if errors:
        raise PlanValidationError(errors)


def validate_plan_text(text: str, request: TripRequest | None = None) -> dict | None:
    """严格解析，不借助渲染层补括号或修复截断，防止错误内容绕过检查。"""
    raw = text.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', raw, flags=re.S)
    if fenced:
        raw = fenced.group(1)
    try:
        data = decode_plan_json(text)
    except (ValueError, TypeError) as exc:
        if request is None and raw and '```' not in raw and not raw.startswith(('{', '[')) and '"days"' not in raw:
            return None  # 普通问答或追问，不是行程成功
        detail = f'（第{exc.lineno}行第{exc.colno}列：{exc.msg}）' if isinstance(exc, json.JSONDecodeError) else ''
        raise PlanValidationError([issue('$', 'invalid_json', '必须返回一个完整有效的行程JSON'+detail)]) from None
    try:
        plan = TripPlan.model_validate(data)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            path = '.'.join(str(x) for x in error['loc']) or '$'
            errors.append(issue(path, error['type'], error['msg']))
        raise PlanValidationError(errors) from None

    errors = []
    span = (plan.end_date-plan.start_date).days
    if not 0 <= span <= 30:
        errors.append(issue('end_date', 'date_range', '结束日期须不早于开始日期且最多31天'))
        expected = []
    else:
        expected = [plan.start_date+timedelta(days=i) for i in range(span+1)]
    if [d.date for d in plan.days] != expected:
        errors.append(issue('days', 'date_coverage', '每日日期须按序完整覆盖起止日期，不重复不遗漏'))
    for i, day in enumerate(plan.days):
        if not day.attractions and i != len(plan.days)-1:
            errors.append(issue(f'days.{i}.attractions', 'empty_day', '非返程日须至少安排一个景点'))
        if day.hotel is None and i != len(plan.days)-1:
            errors.append(issue(f'days.{i}.hotel', 'missing_hotel', '非离店日须安排住宿；最后一天可为null'))
        if day.day_index != i:
            errors.append(issue(f'days.{i}.day_index', 'day_index', f'应为{i}'))
        if sorted(m.type for m in day.meals) != ['breakfast', 'dinner', 'lunch']:
            errors.append(issue(f'days.{i}.meals', 'meal_coverage', '必须且只能包含早中晚三餐各一次'))
    b = plan.budget
    totals = {
        'total_attractions': sum(a.ticket_price for d in plan.days for a in d.attractions),
        'total_hotels': sum(d.hotel.estimated_cost for d in plan.days if d.hotel),
        'total_meals': sum(m.estimated_cost for d in plan.days for m in d.meals),
    }
    for key, value in totals.items():
        if abs(getattr(b, key)-value) > .02:
            errors.append(issue('budget.'+key, 'subtotal_mismatch', f'应与对应每日明细相加一致，明细合计{value:.2f}'))
    total = b.total_attractions+b.total_hotels+b.total_meals+b.total_transportation
    if abs(b.total-total) > .02:
        errors.append(issue('budget.total', 'total_mismatch', f'应等于四类小计之和{total:.2f}'))
    dates = [w.date for w in plan.weather_info]
    if len(set(dates)) != len(dates) or any(d not in set(expected) for d in dates):
        errors.append(issue('weather_info', 'weather_dates', '天气日期重复或不属于旅行日期'))
    if request:
        for field in ('city','start_date','end_date'):
            if getattr(plan, field) != getattr(request, field):
                errors.append(issue(field, 'request_mismatch', '与明确请求不一致'))
        if request.budget_limit is not None and b.total > request.budget_limit:
            errors.append(issue('budget.total', 'budget_exceeded', f'超出预算上限{request.budget_limit:.2f}'))
        names = {a.name for d in plan.days for a in d.attractions}
        if not set(request.required_attractions) <= names:
            errors.append(issue('days.attractions', 'missing_required', '遗漏必去景点，请按请求名称精确匹配'))
    if errors:
        raise PlanValidationError(errors)
    return plan.model_dump(mode='json')
