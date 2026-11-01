"""在指定 Git 版本执行相同案例；结果默认留在忽略的 .work 目录。"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', default='1', help='逗号分隔的案例编号')
    parser.add_argument('--output', default='.work/baseline.json')
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    from config import CONFIG
    from agents.planner import TripPlanner
    from render import parse_plan
    from langchain_core.callbacks import AsyncCallbackHandler

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
    selected = {int(x) for x in args.ids.split(',')}
    rows = []
    for case in cases:
        if case['id'] not in selected:
            continue
        metrics = Metrics()
        llm = CONFIG.create_llm(streaming=False)
        planner = TripPlanner(llm)
        start = time.perf_counter()
        row = dict(case)
        try:
            await asyncio.wait_for(planner.build(), args.timeout)
            planner._agent = planner._agent.with_config(callbacks=[metrics])
            text = await asyncio.wait_for(planner.invoke(case['query']), args.timeout)
            row.update(output=text, parsed_plan=bool(parse_plan(text)), status='completed')
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__)
            from pydantic import ValidationError
            if isinstance(exc.__cause__, ValidationError):
                row['validation_errors'] = [
                    {'location':e['loc'], 'type':e['type'], 'message':e['msg']}
                    for e in exc.__cause__.errors(include_input=False, include_context=False, include_url=False)
                ]
        finally:
            await planner.mcp.close()
        row.update(seconds=round(time.perf_counter()-start, 2), model_calls=metrics.calls or None, reported_tokens=metrics.tokens or None)
        rows.append(row)
        path = ROOT / args.output
        path.parent.mkdir(parents=True, exist_ok=True)
        sources = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('agents/planner.py', 'prompts.py', 'config.py', 'schemas.py', 'reliability.py') if (ROOT/name).exists()}
        path.write_text(json.dumps({'revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(), 'source_hashes':sources, 'model':CONFIG.model_name,'results':rows},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in row.items() if k not in ('output','query')},ensure_ascii=True),flush=True)

if __name__ == '__main__':
    asyncio.run(main())
