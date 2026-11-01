"""规则判定与人工复核分开；不使用LLM自评充当业务正确性。"""
from schemas import TripRequest, validate_plan_text, PlanValidationError


def judge(case, row):
    expected = case['expectation']
    failures = []
    status = row.get('status')
    if status not in expected['statuses']:
        failures.append(f"状态应为{expected['statuses']}，实际为{status}")
    text = row.get('output', '')
    if status == 'valid_plan':
        try:
            request = TripRequest.model_validate(case['request']) if case.get('request') else None
            plan = validate_plan_text(text, request)
            if expected.get('weather_empty') and plan['weather_info']:
                failures.append('超出预报范围，不应填入逐日天气')
        except (ValueError, TypeError) as exc:
            failures.append('输出未通过独立校验')
    if status == 'text':
        for group in expected.get('keyword_groups', []):
            if not any(word in text for word in group):
                failures.append('文本缺少必要信息组：'+'/'.join(group))
    attempts = row.get('run_report', {}).get('tool_attempts', 0)
    if attempts < expected.get('min_tool_attempts', 0):
        failures.append('未实际执行所要求的工具查询')
    if attempts > expected.get('max_tool_attempts', 10000):
        failures.append('应先追问，未澄清前不应查询工具')
    return {'rule_pass':not failures, 'failures':failures,
            'verdict':'fail' if failures else ('manual_review' if expected.get('manual_checks') else 'pass'),
            'manual_checks':expected.get('manual_checks', [])}


def summary(rows):
    counts = {key:0 for key in ('pass','manual_review','fail')}
    for row in rows:
        counts[row['assessment']['verdict']] += 1
    return {'executed':len(rows), 'rule_pass':sum(r['assessment']['rule_pass'] for r in rows),
            'verdicts':counts, 'note':'规则通过不等于人工复核通过；单次运行不代表稳定率'}
