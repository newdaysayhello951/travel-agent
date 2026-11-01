"""行程结构和可确定的业务规则；不负责验证实时价格真实性。"""
from datetime import date, timedelta
from typing import Annotated, Literal
import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Money = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class TripRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    city: str = Field(min_length=1)
    start_date: date
    end_date: date
    budget_limit: Money | None = None
    required_attractions: list[str] = Field(default_factory=list)
    max_attractions_per_day: int | None = Field(default=None, ge=1, le=10)
    allowed_transportation_modes: list[Literal['public_transit','walking','driving','cycling','mixed']] = Field(default_factory=list)
    stay_on_last_day: bool = False

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
    type: Literal['breakfast','lunch','dinner']
    estimated_cost: Money


class DayPlan(BaseModel):
    model_config = ConfigDict(extra='allow')
    date: date
    day_index: int = Field(ge=0)
    hotel: Hotel | None
    attractions: list[Attraction]
    meals: list[Meal] = Field(min_length=3,max_length=3)
    transportation_mode: Literal['public_transit','walking','driving','cycling','mixed'] | None = None



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
    except (ValueError, TypeError):
        return text, []
    changes = []
    meal_types={'breakfast':'breakfast','早餐':'breakfast','早饭':'breakfast',
                'lunch':'lunch','午餐':'lunch','午饭':'lunch',
                'dinner':'dinner','晚餐':'dinner','晚饭':'dinner'}
    if isinstance(data,dict) and isinstance(data.get('days'),list):
        for i, day in enumerate(data['days']):
            if not isinstance(day,dict) or not isinstance(day.get('meals'),list):
                continue
            for j, meal in enumerate(day['meals']):
                if isinstance(meal,dict) and isinstance(meal.get('type'),str):
                    before=meal['type']; canonical=meal_types.get(before.strip().lower())
                    if canonical and canonical != before:
                        meal['type']=canonical
                        changes.append({'path':f'days.{i}.meals.{j}.type','before':before,'after':canonical,'reason':'餐别的明确同义表示标准化'})
    try:
        plan = TripPlan.model_validate(data)
    except (ValueError,TypeError):
        return json.dumps(data,ensure_ascii=False), changes
    sums = {
        'total_attractions': round(sum(a.ticket_price for d in plan.days for a in d.attractions),2),
        'total_hotels': round(sum(d.hotel.estimated_cost for d in plan.days if d.hotel),2),
        'total_meals': round(sum(m.estimated_cost for d in plan.days for m in d.meals),2),
    }
    sums['total'] = round(sum(sums.values()) + plan.budget.total_transportation,2)
    modes={'public_transit':'public_transit','walking':'walking','driving':'driving','cycling':'cycling',
           '公共交通':'public_transit','公交':'public_transit','地铁':'public_transit','公交+步行':'public_transit',
           '步行+公交':'public_transit','地铁+步行':'public_transit','步行+地铁':'public_transit','公交+地铁':'public_transit',
           '步行':'walking','驾车':'driving','自驾':'driving','骑行':'cycling'}
    for i, day in enumerate(data['days']):
        mode=modes.get(str(day.get('transportation','')).strip().lower())
        if day.get('transportation_mode') is None and mode:
            day['transportation_mode']=mode
            changes.append({'path':f'days.{i}.transportation_mode','after':mode,'reason':'从明确交通文本映射标准类型'})
    for key, value in sums.items():
        before = getattr(plan.budget,key)
        if abs(before-value) > .02:
            changes.append({'path':'budget.'+key, 'before':before, 'after':value, 'reason':'由每日明细确定性加总'})
            data['budget'][key] = value
    if not re.search(r'(费用|预算).{0,12}(估算|预估|参考)|(估算|预估).{0,12}(费用|预算)',plan.overall_suggestions):
        notice = '费用均为估算，不代表实时可预订报价；具体价格请向服务商确认。'
        data['overall_suggestions'] = plan.overall_suggestions.rstrip() + '\n' + notice
        changes.append({'path':'overall_suggestions','reason':'补充费用估算标识，不修改明细价格'})
    return json.dumps(data, ensure_ascii=False), changes


def validate_repair_facts(original: str, revised: str, request: TripRequest | None = None):
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
                # 显式景点上限允许删除超额项，但留下的每个完整条目必须来自草稿，
                # 不借机换景点、改票价、增加新事实或删除其他明细。
                limit=request.max_attractions_per_day if request else None
                if (limit is not None and re.fullmatch(r'days\.\d+\.attractions',path)
                        and len(before)>limit and len(after)<=limit):
                    remaining=list(before)
                    for item in after:
                        if item not in remaining:break
                        remaining.remove(item)
                    else:return
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
            actual=','.join(m.type for m in day.meals)
            errors.append(issue(f'days.{i}.meals', 'meal_coverage', f'必须且只能包含早中晚三餐各一次，实际餐别为{actual}'))
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
        if not request.stay_on_last_day and plan.days[-1].hotel is not None and plan.days[-1].hotel.estimated_cost > 0:
            errors.append(issue(f'days.{len(plan.days)-1}.hotel','departure_charge','未要求返程日续住，请将返程日hotel设为null，不要修改酒店报价'))
        for field in ('city','start_date','end_date'):
            if getattr(plan, field) != getattr(request, field):
                errors.append(issue(field, 'request_mismatch', '与明确请求不一致'))
        if request.budget_limit is not None and b.total > request.budget_limit:
            errors.append(issue('budget.total', 'budget_exceeded', f'超出预算上限{request.budget_limit:.2f}'))
        names = {a.name for d in plan.days for a in d.attractions}
        if not set(request.required_attractions) <= names:
            errors.append(issue('days.attractions', 'missing_required', '遗漏必去景点，请按请求名称精确匹配'))
        for i, day in enumerate(plan.days):
            if request.max_attractions_per_day is not None and len(day.attractions) > request.max_attractions_per_day:
                errors.append(issue(f'days.{i}.attractions', 'too_many_attractions', f'每天最多{request.max_attractions_per_day}个景点'))
            if request.allowed_transportation_modes and day.transportation_mode not in request.allowed_transportation_modes:
                errors.append(issue(f'days.{i}.transportation_mode', 'transport_not_allowed', '须明确使用请求允许的交通类型'))
            text=str(getattr(day,'transportation','')).strip().lower()
            declared={'driving':'driving','驾车':'driving','自驾':'driving','walking':'walking','步行':'walking',
                      'public_transit':'public_transit','公交':'public_transit','公共交通':'public_transit','地铁':'public_transit'}.get(text)
            if request.allowed_transportation_modes and declared and declared not in request.allowed_transportation_modes:
                errors.append(issue(f'days.{i}.transportation', 'transport_text_conflict', '交通文字与允许类型冲突，不能只改类型标签'))
    if errors:
        raise PlanValidationError(errors)
    return plan.model_dump(mode='json')
