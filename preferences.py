"""有限的显式饮食冲突筛查：只拒绝明确冲突，不据关键词缺失证明菜品安全。"""
import re

_PHRASES=re.compile(r'取消不吃辣限制|不再要求不辣|(?:不吃辣|不能吃辣|不可以吃辣|不允许吃辣|完全不辣)(?=$|[，,。；;！!？?\s])')
_ALLOW={'取消不吃辣限制','不再要求不辣'}
_LEVELS=re.compile(r'微辣|少辣|小辣|中辣|重辣|特辣')
_NEGATED=re.compile(r'(?:不要|不得|拒绝|不能|不允许)(?:微|少|小|中|重|特)辣')


def update_preferences(text,previous=None):
    old=(previous or {}).get('avoid_spicy')
    state={'avoid_spicy':old if isinstance(old,bool) else None}
    for match in _PHRASES.finditer(text):state['avoid_spicy']=match.group() not in _ALLOW
    return state


def check_preferences(plan,state):
    errors=[];unknown=[]
    if state.get('avoid_spicy'):
        for i,day in enumerate(plan['days']):
            for j,meal in enumerate(day['meals']):
                name=meal['name'];negated=[m.span() for m in _NEGATED.finditer(name)]
                explicit=[m for m in _LEVELS.finditer(name) if not any(a<=m.start()<b for a,b in negated)]
                if explicit:
                    errors.append({'path':f'days.{i}.meals.{j}.name','code':'explicit_spice_conflict',
                                   'message':'明确要求不吃辣，但餐饮推荐仍包含具体辣度选项；请重新确认，不通过改名伪装实际菜品'})
        unknown.append({'path':'days.meals','code':'spice_requires_vendor_confirmation',
                        'message':'未发现辣度冲突也不代表实际菜品无辣，免辣/清汤和加工方式仍须向商家确认'})
    return {'constraints':state,'errors':errors,'unverified':unknown,
            'scope':'仅识别明确短语与餐名辣度冲突，不是过敏原检测或完整饮食语义验证'}
