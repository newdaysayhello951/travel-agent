"""发布配对统计，不发布原始提示、回答、网关地址或凭据。"""
import argparse
import json
from pathlib import Path
from statistics import mean


def render(data):
    rows=data['results']
    expected={(i,n,a) for i in data['ids'] for n in range(1,data['rounds']+1) for a in ('single','multi')}
    if len(rows)!=len(expected) or {(r['id'],r['round'],r['architecture']) for r in rows}!=expected:
        raise ValueError('配对尚未完整或存在重复结果，不能生成完整对照报告')
    lines=['# 相同底座下的单/多Agent对照','',f"固定版本：`{data['revision']}`；模型：`{data['model']}`。",
           f"{len(data['ids'])}例×{data['rounds']}轮×2种架构，共{len(rows)}次；同例交替执行顺序，不并发跑两种架构。",'',
           '两种编排共用模型、规则、事实保护、有限修正、总时限及工具/模型调用上限。子模型与真实地图调用均累计到本次任务预算，委派本身不计地图请求。',
           '', '| 架构 | 规则通过 | 待人工复核 | 平均秒 | 平均模型调用 | 平均工具尝试 | 已上报token合计 |',
           '|---|---:|---:|---:|---:|---:|---:|']
    for arch in ('single','multi'):
        group=[r for r in rows if r['architecture']==arch]
        tokens=sum(r['reported_tokens'] for r in group) if all(r.get('reported_tokens') is not None for r in group) else '未完整上报'
        lines.append(f"| {arch} | {sum(r['assessment']['rule_pass'] for r in group)}/{len(group)} | "
                     f"{sum(r['assessment']['verdict']=='manual_review' for r in group)} | {mean(r['seconds'] for r in group):.2f} | "
                     f"{mean(r['run_report']['model_calls'] for r in group):.2f} | {mean(r['run_report']['tool_attempts'] for r in group):.2f} | {tokens} |")
    planning=[r for r in rows if r['id']<=11]
    if planning:
        lines+=['','## 仅规划案例（排除澄清与天气问答）','',
                '| 架构 | 规则通过 | 平均秒（含失败） | 平均模型调用 |','|---|---:|---:|---:|']
        for arch in ('single','multi'):
            group=[r for r in planning if r['architecture']==arch]
            lines.append(f"| {arch} | {sum(r['assessment']['rule_pass'] for r in group)}/{len(group)} | {mean(r['seconds'] for r in group):.2f} | {mean(r['run_report']['model_calls'] for r in group):.2f} |")
    lines+=['','| 案例 | 轮次 | 架构 | 状态 | 规则 | 秒 | 模型 | 工具 | 已上报token |',
            '|---|---:|---|---|---|---:|---:|---:|---:|']
    for r in sorted(rows,key=lambda x:(x['round'],x['id'],x['architecture']=='multi')):
        report=r['run_report']
        lines.append(f"| {r['id']} | {r['round']} | {r['architecture']} | {r['status']} | "
                     f"{'通过' if r['assessment']['rule_pass'] else '失败'} | {r['seconds']} | {report['model_calls']} | {report['tool_attempts']} | {r.get('reported_tokens') or '未上报'} |")
    lines+=['','## 失败与待复核','']
    for r in rows:
        if not r['assessment']['rule_pass']:
            lines.append(f"- 案例{r['id']}第{r['round']}轮{r['architecture']}：{r.get('error_type','rule_failure')}，运行状态{r['run_report']['status']}，模型{r['run_report']['model_calls']}次、工具{r['run_report']['tool_attempts']}次；保留原始失败，不以补跑覆盖。")
        notes=r['assessment']['failures']+r['assessment']['manual_checks']
        if notes:lines.append(f"- {r['architecture']}案例{r['id']}第{r['round']}轮："+'；'.join(notes))
    lines+=['','## 解释边界','',
            '- 规则通过不代表真实开放时间、票价、交通可执行性或人工复核通过。',
            '- 顺序交替减少固定顺序影响，但服务负载和网络波动仍存在；小样本平均耗时仅描述本轮，不是稳定性能承诺。',
            '- token来自供应方回调，不等同账单金额；未独立审计第三方内部调用及回调上报完整性。',
            '- 该结果比较项目中的两种实现，不证明所有单Agent或多Agent系统的普遍优劣。',
            '- 均值包含失败提前退出及澄清任务；规划子集单列，不能只看更短耗时而忽略失败。',
            '- 比较的是冻结规划器底座，不包含后续应用层RAG、饮食筛查、HTTP鉴权或数据库开销；这些功能另行验证。',
            '- 模型标识相同不代表锁定供应方权重及硬件；本机负载和网络波动未做实验室级控制。',
            '- 原始输出及完整源码指纹保存在本地忽略的结果文件。','']
    return '\n'.join(lines)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('input');parser.add_argument('output');args=parser.parse_args()
    Path(args.output).write_text(render(json.loads(Path(args.input).read_text(encoding='utf-8'))),encoding='utf-8')
