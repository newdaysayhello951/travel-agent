"""在指定 Git 版本执行相同案例；结果默认留在忽略的 .work 目录。"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
import hashlib
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', default='1', help='逗号分隔的案例编号')
    parser.add_argument('--output', default='.work/baseline.json')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--resume', action='store_true', help='保留已有结果，只执行缺少的案例；源文件和案例必须一致')
    args = parser.parse_args()
    from config import CONFIG
    from agents.planner import TripPlanner
    from render import parse_plan
    from langchain_core.callbacks import AsyncCallbackHandler
    from reliability import RunReport
    from schemas import PlanValidationError, TripRequest
    from evals.judge import judge, summary

    class Metrics(AsyncCallbackHandler):
        def __init__(self):
            self.calls = 0
            self.tokens = 0
        async def on_llm_start(self, *a, **kw):
            self.calls += 1
        async def on_chat_model_start(self, *a, **kw):
            self.calls += 1
        async def on_llm_end(self, response, **kw):
            for group in response.generations:
                for generation in group:
                    usage = getattr(getattr(generation, 'message', None), 'usage_metadata', None)
                    if usage:
                        self.tokens += usage.get('total_tokens', 0)

    cases = json.loads((ROOT / 'evals/cases.json').read_text(encoding='utf-8'))
    selected = {c['id'] for c in cases} if args.ids == 'all' else {int(x) for x in args.ids.split(',')}
    if selected - {c['id'] for c in cases}:
        raise ValueError('案例编号不存在')
    path = ROOT / args.output
    source_files = ('agents/planner.py', 'prompts.py', 'config.py', 'schemas.py', 'reliability.py', 'intake.py', 'evals/cases.json', 'evals/judge.py', 'evals/run.py')
    sources = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files}
    settings={name:getattr(CONFIG,name) for name in ('temperature','tool_timeout','tool_retries','max_tool_calls','max_model_calls','run_timeout','recursion_limit','plan_repairs','llm_enable_thinking')}
    rows = []
    if args.resume and path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('source_hashes') != sources or saved.get('model') != CONFIG.model_name or saved.get('settings') != settings:
            raise ValueError('源文件、案例、模型或执行参数已改变，请使用新的输出文件')
        rows = saved['results']
    for case in cases:
        if case['id'] not in selected or any(r['id']==case['id'] for r in rows):
            continue
        metrics = Metrics()
        llm = CONFIG.create_llm(streaming=False)
        planner = TripPlanner(llm)
        start = time.perf_counter()
        row = dict(case)
        report = RunReport()
        try:
            request = TripRequest.model_validate(case['request']) if case.get('request') else None
            text = await asyncio.wait_for(planner.invoke(case['query'], request=request, report=report, callbacks=[metrics]), args.timeout)
            row.update(output=text, parsed_plan=bool(parse_plan(text)), status=report.status)
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__)
            if isinstance(exc, PlanValidationError):
                row['validation_errors'] = exc.issues
        finally:
            await planner.mcp.close()
        row.update(seconds=round(time.perf_counter()-start, 2), model_calls=metrics.calls or None, reported_tokens=metrics.tokens or None)
        row['run_report'] = asdict(report)
        row['assessment'] = judge(case, row)
        rows.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(), 'source_hashes':sources, 'model':CONFIG.model_name,'settings':settings,'summary':summary(rows),'results':rows},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'id':row['id'],'status':row['status'],'seconds':row['seconds'],'assessment':row['assessment'],'report':row['run_report']},ensure_ascii=True),flush=True)

if __name__ == '__main__':
    asyncio.run(main())
