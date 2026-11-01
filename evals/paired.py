"""顺序交替执行两种架构，统一底座、预算、判定；每例保存，可中断续跑。"""
import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

async def main():
    from agents.multi import create_planner
    from config import CONFIG
    from evals.judge import judge
    from langchain_core.callbacks import AsyncCallbackHandler
    from reliability import RunReport
    from schemas import TripRequest,PlanValidationError
    parser=argparse.ArgumentParser()
    parser.add_argument('--ids',default='1,3,5,6,8,14,15')
    parser.add_argument('--rounds',type=int,default=1)
    parser.add_argument('--output',default='.work/paired-architectures.json')
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    if not 1<=args.rounds<=3:raise ValueError('rounds须为1至3')
    cases=json.loads((ROOT/'evals/cases.json').read_text(encoding='utf-8'))
    ids={int(x) for x in args.ids.split(',')}
    if ids-{c['id'] for c in cases}:raise ValueError('案例不存在')
    paths=['agents/planner.py','agents/multi.py','schemas.py','intake.py','prompts.py','config.py','reliability.py','quota.py','mcp_client.py','evals/cases.json','evals/judge.py','evals/paired.py']
    metadata={'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'sources':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths},
              'model':CONFIG.model_name,'ids':sorted(ids),'rounds':args.rounds,
              'settings':{p:getattr(CONFIG,p) for p in ('temperature','run_timeout','tool_timeout','tool_retries','max_tool_calls','max_model_calls','recursion_limit','plan_repairs','llm_enable_thinking')}}
    path=ROOT/args.output;rows=[];interruptions=[]
    if args.resume and path.exists():
        old=json.loads(path.read_text(encoding='utf-8'))
        if any(old.get(k)!=metadata[k] for k in ('sources','model','ids','rounds','settings')):
            raise ValueError('对照源码、模型、案例或预算改变，请使用新文件')
        interruptions=old.get('interruptions',[])+[r for r in old['results'] if r['status']=='quota_limited']
        rows=[r for r in old['results'] if r['status']!='quota_limited'];metadata['revision']=old['revision']
    class Tokens(AsyncCallbackHandler):
        def __init__(self):self.total=0
        async def on_llm_end(self,response,**kwargs):
            for group in response.generations:
                for generation in group:
                    usage=getattr(getattr(generation,'message',None),'usage_metadata',None)
                    if usage:self.total+=usage.get('total_tokens',0)
    for round_number in range(1,args.rounds+1):
        for case in cases:
            if case['id'] not in ids:continue
            order=['single','multi'] if (case['id']+round_number)%2==0 else ['multi','single']
            for architecture in order:
                if any(r['id']==case['id'] and r['round']==round_number and r['architecture']==architecture for r in rows):continue
                planner=create_planner(CONFIG.create_llm(streaming=False),architecture)
                report=RunReport();tokens=Tokens();row={**case,'round':round_number,'architecture':architecture}
                try:
                    request=TripRequest.model_validate(case['request']) if case.get('request') else None
                    text=await planner.invoke(case['query'],request=request,report=report,callbacks=[tokens])
                    row.update(output=text,status=report.status)
                except Exception as exc:
                    row.update(status='failed',error_type=type(exc).__name__)
                    from quota import is_quota_error
                    if is_quota_error(exc):
                        row.update(status='quota_limited',stop_reason='upstream_quota_or_429')
                    if isinstance(exc,PlanValidationError):row['validation_errors']=exc.issues
                finally:await planner.mcp.close()
                row.update(run_report=asdict(report),seconds=report.seconds,reported_tokens=tokens.total or None)
                row['assessment']=judge(case,row);rows.append(row)
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps({**metadata,'results':rows,'interruptions':interruptions},ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({'id':case['id'],'round':round_number,'architecture':architecture,'status':row['status'],
                                  'rule_pass':row['assessment']['rule_pass'],'seconds':report.seconds,
                                  'models':report.model_calls,'tools':report.tool_attempts},ensure_ascii=True),flush=True)
                if row['status']=='quota_limited':return

if __name__=='__main__':asyncio.run(main())
