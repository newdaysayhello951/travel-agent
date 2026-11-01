"""从本地批量结果生成不含原始请求/回答的中文统计报告。"""
import argparse
import json
from pathlib import Path


def render(data):
    rows=data['results']
    passed=sum(r['assessment']['rule_pass'] for r in rows)
    lines=['# 旅行规划批量回归结果','',f'已执行{len(rows)}条；规则通过{passed}条。规则通过不代表人工复核通过，也不代表实际旅行可执行。','',
           '| 编号 | 案例 | 状态 | 规则判定 | 耗时/秒 | 模型调用 | 工具尝试 |',
           '|---|---|---|---|---:|---:|---:|']
    for r in rows:
        report=r.get('run_report',{})
        lines.append(f"| {r['id']} | {r['name']} | {r['status']} | {'通过' if r['assessment']['rule_pass'] else '失败'} | {r['seconds']} | {report.get('model_calls',0)} | {report.get('tool_attempts',0)} |")
    lines.extend(['','## 失败与待复核项',''])
    for r in rows:
        notes=r['assessment']['failures']+r['assessment']['manual_checks']
        if notes:
            lines.append(f"- 案例{r['id']}："+'；'.join(notes))
    lines.extend(['','## 限制','',
                  '- 单次批量执行不代表稳定率；未进行架构性能对比。',
                  '- 关键词只用于最低限度检查，不能替代语义和事实复核。',
                  '- 原始输出保存在忽略的本地结果文件中，未随本报告发布。',''])
    return '\n'.join(lines)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output')
    args=parser.parse_args()
    Path(args.output).write_text(render(json.loads(Path(args.input).read_text(encoding='utf-8'))),encoding='utf-8')
