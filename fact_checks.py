"""检查与带来源资料的一致性；未知、过期、未预约不能算事实通过。"""
from datetime import date,datetime
import math
from pydantic import BaseModel,ConfigDict,Field,model_validator
from schemas import Money


class FactRule(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(min_length=1,max_length=200)
    aliases:list[str]=Field(default_factory=list,max_length=20)
    valid_from:date
    valid_until:date
    opens:str|None=Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    closes:str|None=Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    last_entry:str|None=Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    closed_dates:list[date]=Field(default_factory=list,max_length=100)
    closed_weekdays:list[int]=Field(default_factory=list,max_length=7)
    adult_ticket_price:Money|None=None
    reservation_required:bool|None=None

    @model_validator(mode='after')
    def valid(self):
        if self.valid_until<self.valid_from:raise ValueError('资料有效日期顺序错误')
        if any(i<0 or i>6 for i in self.closed_weekdays):raise ValueError('星期必须为0至6')
        if self.opens and self.closes and self.opens>=self.closes:raise ValueError('开放时间须早于闭馆时间')
        return self


def minutes(value):
    if not isinstance(value,str):return None
    try:
        parsed=datetime.strptime(value,'%H:%M');return parsed.hour*60+parsed.minute
    except ValueError:return None


def check_plan(plan,facts):
    errors=[];unknown=[];sources=set();checks=0
    def note(target,path,code,message,source=None):
        target.append({'path':path,'code':code,'message':message,**({'source_url':source} if source else {})})
    for i,day in enumerate(plan['days']):
        previous_end=None
        for j,item in enumerate(day['attractions']):
            path=f'days.{i}.attractions.{j}';start=minutes(item.get('start_time'));end=minutes(item.get('end_time'))
            if start is None or end is None:note(unknown,path,'missing_schedule','缺少明确起止时间，不能核实当日时间安排')
            else:
                checks+=1
                if end<=start:note(errors,path,'invalid_duration','游览结束时间须晚于开始时间')
                if previous_end is not None:
                    if start<previous_end:note(errors,path,'time_overlap','与前一个景点游览时间重叠')
                    travel=item.get('travel_minutes_from_previous')
                    if isinstance(travel,(int,float)) and not isinstance(travel,bool) and math.isfinite(travel) and travel>=0:
                        if start<previous_end+travel:note(errors,path,'insufficient_travel_time','前后景点之间预留交通时间不足')
                    else:note(unknown,path,'missing_route_duration','缺少经过查询的路段耗时，不能验证交通衔接')
                previous_end=end
            if start is None or end is None:previous_end=None
            matches=[f for f in facts if item['name'] in [f['name'],*f.get('aliases',[])] and f['valid_from']<=day['date']<=f['valid_until']]
            if len(matches)!=1:
                note(unknown,path,'missing_or_conflicting_source','没有唯一且日期有效的地点资料，开放时间、价格及预约待核实');continue
            fact=matches[0];source=fact['source_url'];sources.add(source);checks+=1
            if day['date'] in fact.get('closed_dates',[]):note(errors,path,'closed_on_date','资料标明该日闭馆',source)
            if date.fromisoformat(day['date']).weekday() in fact.get('closed_weekdays',[]):
                note(unknown,path,'weekly_closure','该日符合常规闭馆星期，需核实法定节假日或临时开放例外',source)
            opens=minutes(fact.get('opens'));closes=minutes(fact.get('closes'))
            if start is not None and end is not None and opens is not None and closes is not None:
                if start<opens or end>closes:note(errors,path,'outside_open_hours','游览安排超出资料记载的开放时间',source)
            else:note(unknown,path,'unverified_open_hours','缺少开放时段或游览时段，无法验证是否在开放时间内',source)
            last_entry=minutes(fact.get('last_entry'))
            if start is not None and last_entry is not None and start>last_entry:
                note(errors,path,'after_last_entry','游览起始晚于资料记载的停止入馆时间',source)
            price=fact.get('adult_ticket_price')
            if price is not None and item.get('ticket_price_basis')=='adult_per_person':
                if abs(item['ticket_price']-price)>.02:note(errors,path+'.ticket_price','price_mismatch','成人单人门票与来源资料不一致，不含优惠或额外展馆',source)
            else:note(unknown,path+'.ticket_price','unverified_price_basis','缺少相同计价口径的票价资料，估算不等于实时报价',source)
            if fact.get('reservation_required'):
                note(unknown,path,'reservation_required','需要预约，须由用户确认有效预约凭证，模型声明不能代替凭证',source)
    # 即使已知字段全部一致，也不把外部世界或预订可用性标记为全部验证。
    return {'status':'conflicts' if errors else 'needs_review' if unknown else 'consistent_with_sources',
            'checked_items':checks,'errors':errors,'unverified':unknown,'sources':sorted(sources),
            'scope':'仅按填入时间、交通耗时及所提供资料检查一致性；未自动核验耗时来源，不证明资料真实性、实时库存或预约成功'}
