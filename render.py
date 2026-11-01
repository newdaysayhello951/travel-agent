"""渲染工具 —— JSON 解析、CLI 格式化、Streamlit 组件。"""
import json
import re

# markdown 代码围栏（LLM 几乎总会把 JSON 包在 ```json ... ``` 里）
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
# 对象/数组结尾前多余的逗号：{"a":1,} 或 [1,2,]
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _try_load(s: str) -> dict | None:
    """尝试解析 JSON，成功且是对象则返回，否则 None。"""
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _looks_like_plan(data: dict) -> bool:
    """粗判一个 dict 是不是行程计划。

    用于避免误判：正文里若先出现一个无关的 {...}（例如示例说明），
    括号平衡扫描会先命中它，把无关对象当成计划返回。
    """
    return any(k in data for k in ("city", "days", "budget", "weather_info"))


def _balanced_object(text: str, start: int) -> str | None:
    """从 text[start]（应为 '{'）起做括号平衡扫描。

    会跳过字符串字面量内部的括号和转义字符。
    若一直扫到文本末尾都没闭合，返回 None（说明被截断了）。
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _close_truncated(s: str) -> str:
    """把被截断的 JSON 修补到可解析：补上未闭合的字符串与括号。

    典型场景：模型输出被 max_tokens 截断，JSON 停在半途。
    原实现遇到这种情况只能放弃，界面一片空白；这里尽量抢救出前面的内容。
    """
    in_str = False
    esc = False
    stack: list[str] = []
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()

    out = s
    if in_str:
        out += '"'          # 先闭合被截断的字符串
    # 去掉结尾不完整的片段：悬空的逗号、只有键没有值、只有冒号
    out = re.sub(r",\s*$", "", out)
    out = re.sub(r',\s*"[^"]*"\s*:?\s*$', "", out)
    out = re.sub(r"[{[,]\s*$", "", out)
    out = re.sub(r",\s*$", "", out)
    # 补齐所有未闭合的容器
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def parse_plan(text: str) -> dict | None:
    """从混合文本中提取并解析旅行计划 JSON。

    比原始版本耐操得多。原始实现只有一招：
        json.loads(text[第一个{ : 最后一个}+1])
    只要模型输出带 markdown 围栏、结尾多一个逗号、或者被 max_tokens 截断，
    它就返回 None，Web 界面直接变成空白 —— 什么提示都没有。

    这里改为逐级尝试：
        候选片段：代码围栏内容 / 首{到末} / 括号平衡扫描 / 首个{到结尾
        每个片段：原样 → 去结尾逗号 → 修补截断
    """
    if not text:
        return None

    candidates: list[str] = []

    # 1) markdown 代码围栏里的内容（最常见）
    candidates.extend(m.group(1) for m in _FENCE_RE.finditer(text))

    # 2) 从**每一个** '{' 出发做括号平衡扫描。
    #    不能只看第一个 '{'：模型可能先在正文里写个示例对象，
    #    真正的计划在后面，只扫第一个就会误判成那个示例。
    #    上限 200 个起点，避免超长文本下退化成 O(n²)。
    starts = [i for i, ch in enumerate(text) if ch == "{"][:200]
    for i in starts:
        balanced = _balanced_object(text, i)
        if balanced:
            candidates.append(balanced)

    # 3) 首个 '{' 到最后一个 '}'（原始实现的策略，保留）
    if starts:
        start = starts[0]
        end = text.rfind("}") + 1
        if end > start:
            candidates.append(text[start:end])
        # 4) 首个 '{' 到结尾：给「截断修补」提供素材
        candidates.append(text[start:])

    cleaned = [c.strip() for c in candidates if c and c.strip()]

    fallback: list[dict] = []

    def _consider(data: dict | None) -> dict | None:
        """命中"像行程"的对象就直接返回；否则记为备选。"""
        if data is None:
            return None
        if _looks_like_plan(data):
            return data
        fallback.append(data)
        return None

    # 第一轮：不做截断修补，尽量拿到完整且正确的 JSON
    for raw in cleaned:
        for attempt in (raw, _TRAILING_COMMA_RE.sub(r"\1", raw)):
            hit = _consider(_try_load(attempt))
            if hit is not None:
                return hit

    # 第二轮：修补截断后再试
    for raw in cleaned:
        hit = _consider(_try_load(_close_truncated(_TRAILING_COMMA_RE.sub(r"\1", raw))))
        if hit is not None:
            return hit

    # 都不像行程时，退回键最多的那个对象（保持原有的宽松行为）
    return max(fallback, key=len) if fallback else None


# ==================== CLI 格式化 ====================

def _weather_icon(weather: str) -> str:
    mapping = {
        "晴": "☀️", "多云": "⛅", "阴": "☁️",
        "小雨": "🌧️", "中雨": "🌧️", "大雨": "⛈️", "暴雨": "⛈️",
        "雪": "❄️", "雾": "🌫️", "霾": "🌫️",
    }
    for key, icon in mapping.items():
        if key in weather:
            return icon
    return "🌡️"


def format_plan_cli(json_text: str) -> str | None:
    """将 Planner JSON 渲染为 CLI 可读的中文旅行计划。"""
    data = parse_plan(json_text)
    if data is None:
        return None

    lines = []
    city = data.get("city", "未知")
    start_date = data.get("start_date", "")
    end_date = data.get("end_date", "")

    lines.append("")
    lines.append("╔" + "═" * 58 + "╗")
    title = f"  🌴 {city} {start_date} ~ {end_date} 旅行计划"
    lines.append(f"║{title:<56}║")
    lines.append("╚" + "═" * 58 + "╝")

    # 天气
    weather_info = data.get("weather_info", [])
    if weather_info:
        lines.append("")
        lines.append("🌤️  天气概况")
        for w in weather_info:
            d = w.get("date", "")[-5:]
            di = _weather_icon(w.get("day_weather", ""))
            ni = _weather_icon(w.get("night_weather", ""))
            lines.append(
                f"   {d}  {di} {w.get('day_weather', '?')} → "
                f"{ni} {w.get('night_weather', '?')}  "
                f"{w.get('day_temp', '?')}°C / {w.get('night_temp', '?')}°C  "
                f"{w.get('wind_direction', '')}{w.get('wind_power', '')}"
            )

    # 每日行程
    for day in data.get("days", []):
        idx = day.get("day_index", 0) + 1
        d = day.get("date", "")[-5:]
        desc = day.get("description", "")
        lines.append("")
        lines.append("━" * 60)
        lines.append(f"📅 Day {idx}  {d}  {desc}")
        lines.append("━" * 60)

        hotel = (day.get("hotel") or {})
        if hotel.get("name"):
            lines.append(
                f"  🏨 {hotel['name']}  ★{hotel.get('rating', '')}  "
                f"¥{hotel.get('estimated_cost', 0)}/晚  |  {hotel.get('address', '')}"
            )
        lines.append(f"  🚌 {day.get('transportation', '')}")

        attractions = day.get("attractions", [])
        if attractions:
            lines.append(f"  🏛️  景点 ({len(attractions)}个):")
            for a in attractions:
                ticket = a.get("ticket_price", 0)
                ts = "免费" if ticket == 0 else f"¥{ticket}"
                lines.append(f"     · {a.get('name', '?')}")
                lines.append(
                    f"       {a.get('address', '')}  |  {a.get('category', '')}  |  "
                    f"游玩约{a.get('visit_duration', 0)}分钟  |  {ts}"
                )

        meals = day.get("meals", [])
        if meals:
            lines.append("  🍽️  餐饮:")
            for m in meals:
                mt = {"breakfast": "早", "lunch": "午", "dinner": "晚"}
                label = mt.get(m.get("type", ""), "餐")
                lines.append(f"     {label} {m.get('name', '?')}  ¥{m.get('estimated_cost', 0)}")

    # 预算
    budget = data.get("budget", {})
    if budget:
        lines.append("")
        lines.append("━" * 60)
        lines.append("💰 预算汇总")
        lines.append(
            f"   景点: ¥{budget.get('total_attractions', 0):>6}  |  "
            f"酒店: ¥{budget.get('total_hotels', 0):>6}  |  "
            f"餐饮: ¥{budget.get('total_meals', 0):>6}  |  "
            f"交通: ¥{budget.get('total_transportation', 0):>6}"
        )
        lines.append(f"   📊 总计: ¥{budget.get('total', 0):,}")

    # 建议
    suggestions = data.get("overall_suggestions", "")
    if suggestions:
        lines.append("")
        lines.append("💡 旅行建议")
        for tip in suggestions.replace("；", ";").split(";"):
            tip = tip.strip()
            if tip:
                lines.append(f"   {tip}")

    lines.append("")
    return "\n".join(lines)
