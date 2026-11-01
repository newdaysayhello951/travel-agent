"""逐输出列出来源匹配与待核实范围，不把来源匹配当作人工全部通过。"""
import argparse,hashlib,json
from datetime import date
from pathlib import Path


def review(data,references,observations=None):
    rows=[]
    for r in data['results']:
        row={'id':r['id'],'round':r['round'],'architecture':r['architecture'],'status':r['status'],
             'output_sha256':hashlib.sha256(r.get('output','').encode()).hexdigest(),'claims':[],
             'manual_requirements':r['assessment']['manual_checks']}
        row['output_observations']=[o for o in (observations or []) if (o['id'],o['round'],o['architecture'],o['output_sha256'])==(row['id'],row['round'],row['architecture'],row['output_sha256'])]
        if r['status']=='valid_plan':
            plan=json.loads(r['output']);matched=0
            for i,day in enumerate(plan['days']):
                for j,item in enumerate(day['attractions']):
                    applicable=[s for s in references['rules'] if s['city']==plan['city'] and item['name'] in [s['name'],*s['aliases']] and (not s.get('months') or date.fromisoformat(day['date']).month in s['months'])]
                    entry={'path':f'days.{i}.attractions.{j}','name':item['name'],'price':item['ticket_price'],'state':'needs_review'}
                    if len(applicable)==1:
                        matched+=1;s=applicable[0];entry.update(source=s['source'],note=s['note'])
                        if 'price' in s:
                            entry.update(reference_price=s['price'],price_consistent_numerically=abs(item['ticket_price']-s['price'])<=.02)
                        if date.fromisoformat(day['date']).weekday() in s.get('closed_weekdays',[]):entry['weekly_closure_warning']=True
                    else:entry['note']='本次参照集无唯一匹配；不得算作地点、票价或开放时间已核实'
                    row['claims'].append(entry)
            row['source_matched']=matched;row['total_attractions']=len(row['claims'])
            row['unverified_dimensions']=['优惠/票型/人数口径','预约凭证与实时库存','酒店身份与当日房价','餐饮饮食与亲子/老人适宜性','真实路线、开放公告与时间冲突']
        else:row['unverified_dimensions']=['文本语义、澄清完整性或失败原因，按案例标准单独复核']
        rows.append(row)
    return {'revision':data['revision'],'checked_on':references['checked_on'],'method':references['scope'],'rows':rows}


def render(result,data):
    expected=len(data['ids'])*data['rounds']*2;rows=result['rows']
    lines=['# 来源核查与人工复核清单','',result['method'],'',f'本文件覆盖当前{len(rows)}/{expected}次已保存输出。来源参照查阅日期：{result["checked_on"]}。',
           '规则通过与事实通过分开：以下核查不将任何输出标为全部事实合格，不冒充独立人工签字。未匹配资料仍须核实。','',
           '| 案例 | 轮次 | 架构 | 状态 | 景点数 | 参照匹配 | 票价口径待复核 | 常规闭馆提醒 |',
           '|---|---:|---|---|---:|---:|---:|---:|']
    for r in rows:
        claims=r['claims'];mismatches=[c for c in claims if c.get('price_consistent_numerically') is False]
        lines.append(f"| {r['id']} | {r['round']} | {r['architecture']} | {r['status']} | {r.get('total_attractions',0)} | {r.get('source_matched',0)} | {len(mismatches)} | {sum(c.get('weekly_closure_warning',False) for c in claims)} |")
    lines+=['','## 需重点核对的输出','']
    for r in rows:
        for c in r['claims']:
            if c.get('price_consistent_numerically') is False or c.get('weekly_closure_warning'):
                lines.append(f"- 案例{r['id']}第{r['round']}轮{r['architecture']}，{c['path']} {c['name']}："+
                    (f"输出费用{c['price']}，参照{c['reference_price']}；先确认票型与人数，不能直接把差值当成已证实错误。" if c.get('price_consistent_numerically') is False else '')+
                    ('游览日符合常规闭馆星期，需核实例外并调整。' if c.get('weekly_closure_warning') else '')+
                    f" [{c['note']}]({c['source']})")
    lines+=['','## 按案例保留的人工复核要求','']
    seen=set()
    for r in rows:
        if r['id'] not in seen:
            seen.add(r['id'])
            lines.append(f"- 案例{r['id']}："+('；'.join(r['manual_requirements']) or '显式景点数约束已自动检查；旅行事实仍按来源核实'))
    lines+=['','## 逐条查看输出后的补充观察','',
            '以下由编程助手查看对应输出后记录，仅在输出SHA-256一致时应用，不冒充独立人类评审。','']
    for r in rows:
        for o in r['output_observations']:
            lines.append(f"- 案例{r['id']}第{r['round']}轮{r['architecture']}（{o['state']}）：{o['note']}")
    lines+=['','## 尚不能据此确认的内容','',
            '- 酒店身份及当日房价、预约凭证、实际交通耗时和软偏好尚无逐项可追溯证据，保持待核实。',
            '- 旧官方公告只作参照，不自动认定适用于2026年的具体出行日；动态展览和价格以最新官方渠道为准。',
            '- 完整逐条记录、输出摘要指纹保存在本地JSON；报告不公开原始完整对话。','']
    return '\n'.join(lines)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('output');p.add_argument('--json-output',default='.work/source-review.json');args=p.parse_args()
    data=json.loads(Path(args.input).read_text(encoding='utf-8'))
    references=json.loads((Path(__file__).parent/'source_review_rules.json').read_text(encoding='utf-8'))
    notes_path=Path(__file__).parent/'source_review_observations.json'
    notes=json.loads(notes_path.read_text(encoding='utf-8'))['observations'] if notes_path.exists() else []
    result=review(data,references,notes)
    Path(args.json_output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    Path(args.output).write_text(render(result,data),encoding='utf-8')
