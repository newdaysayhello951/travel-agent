"""智能旅行助手 — Streamlit Web 界面。"""
import asyncio
import json
import os
from dataclasses import asdict
from datetime import date

import streamlit as st

# ---- 页面配置 ----
st.set_page_config(
    page_title="智能旅行助手",
    page_icon="🧳",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- CSS 样式 ----
st.markdown("""
<style>
    .main-header {
        font-size: 2rem; font-weight: 700; padding: 0.5rem 0;
        border-bottom: 3px solid #4A90D9; margin-bottom: 1rem;
    }
    .plan-title {
        font-size: 1.4rem; font-weight: 700; color: #2E7D32;
        text-align: center; margin: 1rem 0;
    }
    .day-header {
        font-size: 1.1rem; font-weight: 700; color: #1565C0;
        border-bottom: 2px solid #BBDEFB; padding: 0.5rem 0; margin: 1rem 0 0.5rem;
    }
    .weather-card {
        background: #E3F2FD; border-radius: 10px; padding: 1rem; margin: 0.5rem 0;
        color: #1a1a1a;
    }
    .weather-card b {
        color: #1565C0;
    }
    .budget-card {
        background: #FFF8E1; border-radius: 10px; padding: 1rem; margin: 0.5rem 0;
        color: #1a1a1a;
    }
</style>
""", unsafe_allow_html=True)


# ---- 初始化 ----
@st.cache_resource
def get_planner():
    from config import CONFIG
    from agents.planner import TripPlanner
    llm = CONFIG.create_llm()
    return TripPlanner(llm)


@st.cache_resource
def get_database():
    from config import CONFIG  # 先加载.env，包含可选DATABASE_URL
    from storage import PlanStore
    return PlanStore()


def get_store():
    return get_database().for_owner(st.session_state.get('user_id','local'))


@st.cache_resource
def get_auth():
    from auth import AuthStore
    return AuthStore(get_database())


@st.cache_resource
def get_login_limiter():
    from api import RateLimiter
    return RateLimiter()


def get_service():
    from travel_service import TravelService
    return TravelService(get_planner(),get_store())


def load_saved(saved):
    st.session_state.plan_data=saved.plan
    st.session_state.plan_raw=json.dumps(saved.plan,ensure_ascii=False)
    st.session_state.conversation_id=saved.conversation_id
    st.session_state.plan_version=saved.version
    st.session_state.run_report=None
    st.session_state.status_lines=[]


# ---- 辅助: 构建 prompt ----
def build_prompt(
    city: str,
    start_date: date,
    end_date: date,
    transport: list[str],
    hotel_type: str,
    preferences: list[str],
    extra: str,
) -> str:
    # 含头含尾计算天数：5/21~5/23 是 3 天而不是 2 天
    # （原代码用 (end - start).days，得到的是"间隔"，会少算一天）
    days = (end_date - start_date).days + 1
    parts = [
        f"{city}{days}日游",
        f"{start_date.strftime('%Y年%m月%d日')}-{end_date.strftime('%Y年%m月%d日')}",
    ]
    if preferences:
        parts.append(f"喜欢{'、'.join(preferences)}")
    if hotel_type:
        parts.append(f"住宿偏好{hotel_type}")
    if transport:
        parts.append(f"交通方式偏好{'、'.join(transport)}")
    if extra.strip():
        parts.append(f"额外要求: {extra.strip()}")
    parts.append("中等预算")
    return "，".join(parts)


# ---- 辅助: 转换流式事件 ----
_SILENCE_NAMES = {"maps_weather", "maps_text_search", "maps_search_detail",
                   "maps_direction_walking", "maps_direction_driving",
                   "maps_direction_transit_integrated", "maps_direction_bicycling",
                   "maps_distance", "maps_geo", "maps_regeocode",
                   "maps_ip_location", "maps_around_search",
                   "maps_schema_personal_map", "maps_schema_navi",
                   "maps_schema_take_taxi"}

_STREAM_LABELS = {
    "query_weather":     "🌤️ 查询天气中...",
    "search_hotel":      "🏨 搜索酒店中...",
    "search_attraction": "🏛️ 搜索景点中...",
    "maps_direction_walking_by_address":             "🚶 规划步行路线...",
    "maps_direction_driving_by_address":             "🚗 规划驾车路线...",
    "maps_direction_transit_integrated_by_address":  "🚌 规划公交路线...",
}

# ---- 主 UI ----
st.markdown('<div class="main-header">🧳 智能旅行助手</div>', unsafe_allow_html=True)

# 本机模式只能由启动服务的操作者通过环境变量开启，不能由访客选择。
from config import CONFIG
if os.getenv('TRAVEL_LOCAL_MODE')!='1':
    from auth import AuthenticationError,UsernameUnavailable
    if st.session_state.get('auth_token'):
        try:
            st.session_state.user_id=get_auth().authenticate(st.session_state.auth_token)
        except AuthenticationError:
            st.session_state.clear();st.rerun()
    else:
        st.info('登录后查看和修改自己的旅行记录。旧版本本地记录保留在本机模式，不自动分配给新账号。')
        with st.form('account',clear_on_submit=True):
            username=st.text_input('用户名',max_chars=40)
            password=st.text_input('密码（12至128位）',type='password',max_chars=128)
            login=st.form_submit_button('登录')
            register=st.form_submit_button('注册')
        if login or register:
            try:
                get_login_limiter().check(('web-account',username.lower()),10)
                if register:
                    get_auth().register(username,password);st.success('注册成功，请登录。')
                else:
                    token=get_auth().login(username,password)
                    st.session_state.auth_token=token
                    st.session_state.user_id=get_auth().authenticate(token)
                    st.rerun()
            except (ValueError,UsernameUnavailable,AuthenticationError):st.error('注册或登录失败，请检查用户名及密码。')
            except Exception:st.error('请求过于频繁或服务不可用，请稍后重试。')
        st.stop()
    if st.sidebar.button('退出登录'):
        get_auth().logout(st.session_state.auth_token);st.session_state.clear();st.rerun()

# ============ 侧边栏: 参数输入 ============
with st.sidebar:
    st.markdown("### 📋 旅行参数")

    city = st.text_input("📍 目的地城市", placeholder="例如: 杭州、成都、三亚...")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("📅 开始日期", value=date.today())
    with col2:
        end_date = st.date_input("📅 结束日期", value=date.today())

    if start_date and end_date and end_date >= start_date:
        # 含头含尾：与 build_prompt 保持一致
        trip_days = (end_date - start_date).days + 1
        st.info(f"📌 共计 **{trip_days}** 天")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")

    st.markdown("---")
    st.markdown("### 🚗 交通方式")
    transport_options = ["公共交通", "自驾", "打车/网约车", "骑行", "步行"]
    transport_selected = []
    for opt in transport_options:
        if st.checkbox(opt, key=f"trans_{opt}"):
            transport_selected.append(opt)

    st.markdown("---")
    st.markdown("### 🏨 住宿偏好")
    hotel_type = st.selectbox(
        "住宿类型",
        ["不限", "经济型酒店", "中档型酒店", "豪华型酒店", "民宿/客栈", "青年旅舍"],
        index=2,
        label_visibility="collapsed",
    )
    if hotel_type == "不限":
        hotel_type = ""

    st.markdown("---")
    st.markdown("### 🎯 旅行偏好")
    pref_options = ["自然风光", "历史文化", "美食探店", "休闲度假", "艺术展览", "购物逛街", "亲子乐园"]
    pref_selected = []
    for opt in pref_options:
        if st.checkbox(opt, key=f"pref_{opt}"):
            pref_selected.append(opt)

    st.markdown("---")
    st.markdown("### 💬 额外要求")
    extra_requirements = st.text_area(
        "补充说明",
        placeholder="例如: 带老人出行需要轻松行程、想在市中心活动...",
        label_visibility="collapsed",
    )

    st.markdown("---")
    budget_limit=st.number_input('总预算上限（元，0表示不限）',min_value=0,step=100)
    stay_on_last_day=st.checkbox('返程当天仍需住宿',value=False)
    submit_btn = st.button("🚀 开始规划", type="primary", use_container_width=True)


# ============ 主区域: 结果展示 ============
if "plan_data" not in st.session_state:
    st.session_state.plan_data = None
if "plan_raw" not in st.session_state:
    st.session_state.plan_raw = ""

with st.sidebar:
    st.markdown('### 已保存行程')
    conversations=get_store().list_conversations()
    labels={c['id']:f"{c['title']} · v{c['version']} · {c['id'][:6]}" for c in conversations}
    selected=st.selectbox('选择会话',['']+list(labels),format_func=lambda cid:labels.get(cid,'请选择'))
    if selected:
        history=get_store().history(selected)
        if history['versions']:
            selected_version=st.selectbox('查看版本',list(reversed(history['versions'])))
            if st.button('加载此版本'):
                load_saved(get_store().get_plan(selected,selected_version))
                st.rerun()
        else:
            st.caption('尚无合格行程；可查看失败记录后重新规划。')
            with st.expander('失败执行记录'):st.json(history['runs'])
    with st.expander('知识库资料导入与更新'):
        from knowledge import KnowledgeStore
        kb=KnowledgeStore(get_store())
        documents=kb.list_documents()
        choices={d['id']:d['title'] for d in documents}
        replacement=st.selectbox('更新目标',['']+list(choices),format_func=lambda key:choices.get(key,'新增资料'))
        uploaded=st.file_uploader('资料JSON（含来源、城市和过期日期）',type=['json'])
        if st.button('保存资料') and uploaded:
            try:
                if uploaded.size>200000:raise ValueError('文件过大')
                from api import DocumentInput
                body=DocumentInput.model_validate(json.loads(uploaded.getvalue()))
                kb.put(**body.model_dump(mode='json'),document_id=replacement or None)
                st.success('资料已保存；过期资料不参与检索。')
            except Exception:st.error('导入失败，请检查资料格式、有效日期和来源链接。')
        if documents:st.dataframe(documents,hide_index=True)

# 未开始时的引导页
if not submit_btn and st.session_state.plan_data is None:
    st.info("👈 在左侧填写旅行参数，然后点击 **开始规划** 按钮")
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("##### 🌤️ 实时天气查询")
        st.caption("接入高德地图 MCP，获取目的地准确天气预报")
    with col_b:
        st.markdown("##### 🏛️ 智能景点推荐")
        st.caption("根据你的偏好，AI 精准匹配最适合的景点和路线")
    with col_c:
        st.markdown("##### 📊 预算自动汇总")
        st.caption("景点门票、餐饮、住宿、交通费用一目了然")

# 点击按钮后执行
if submit_btn:
    if not city.strip():
        st.error("请输入目的地城市")
    elif end_date < start_date:
        st.error("结束日期不能早于开始日期")
    else:
        from schemas import TripRequest, PlanValidationError
        from reliability import ExecutionLimitError, RunReport, PlanningServiceError
        if (end_date - start_date).days > 30:
            st.error("单次最多规划31天，请缩短日期范围。")
            st.stop()
        request = TripRequest(city=city.strip(), start_date=start_date, end_date=end_date,
                              budget_limit=budget_limit or None,stay_on_last_day=stay_on_last_day)
        prompt = build_prompt(
            city, start_date, end_date,
            transport_selected, hotel_type, pref_selected, extra_requirements,
        )

        # 流式输出区域
        with st.spinner("🤖 AI 正在为您规划旅行方案..."):
            status_placeholder = st.empty()
            report=RunReport()
            def progress(token):
                if token.startswith('\n') and token.endswith('...\n'):
                    status_placeholder.markdown(token.strip())
            try:
                saved=asyncio.run(get_service().create(prompt,request,on_progress=progress,report=report))
            except (PlanValidationError, ExecutionLimitError, TimeoutError, PlanningServiceError,ValueError) as exc:
                st.error(str(exc) or "执行超时，请缩小需求后重试。")
                st.json(asdict(report))
                st.stop()
            load_saved(saved)
            st.session_state.run_report=get_store().history(saved.conversation_id)['runs'][-1]
        st.rerun()


# ============ 结果展示 ============
if st.session_state.get('conversation_id') and st.session_state.plan_data:
    from storage import VersionConflict
    from schemas import PlanValidationError
    from reliability import ExecutionLimitError,PlanningServiceError,RunReport
    cid=st.session_state.conversation_id
    version=st.session_state.plan_version
    st.caption(f'会话 {cid[:8]} · 当前查看版本 v{version}')
    current=get_store().get_plan(cid,version)
    with st.form('revise_plan'):
        instruction=st.text_area('继续修改行程',placeholder='例如：第二天减少一个景点，安排得轻松一些')
        preserve_hotels=st.checkbox('保留已有酒店及报价',value=True)
        revised_budget=st.number_input('修改后的总预算上限（元，0表示不限）',min_value=0.0,
                                       value=float(current.request.get('budget_limit') or 0),step=100.0,
                                       key=f'revise_budget_{cid}_{version}')
        revise_btn=st.form_submit_button('保存为新版本')
    if revise_btn:
        report=RunReport()
        try:
            with st.spinner('正在修改；通过校验后保存新版本...'):
                updated=asyncio.run(get_service().revise(cid,instruction,expected_version=version,
                    preserve_hotels=preserve_hotels,request_updates={'budget_limit':revised_budget or None},report=report))
            load_saved(updated)
            st.session_state.run_report=get_store().history(cid)['runs'][-1]
            st.rerun()
        except (ValueError,VersionConflict,PlanValidationError,ExecutionLimitError,TimeoutError,PlanningServiceError) as exc:
            st.error(str(exc) or '修改失败，已保存的行程版本保持不变。')
            st.session_state.run_report=asdict(report)
    with st.expander('会话历史与全部执行记录'):
        st.json(get_store().history(cid))

if st.session_state.get('run_report'):
    with st.expander('本次执行与校验记录', expanded=False):
        st.json(st.session_state.run_report)
plan = st.session_state.plan_data
if plan is not None:
    facts=plan.get('fact_check')
    if facts:
        st.warning('行程结构校验通过，事实仍需按来源与预约凭证核实。')
        with st.expander('事实检查与待核实项目'):
            st.json(facts)
    with st.expander('检索参考资料与实际引用'):
        st.caption('检索候选不等于模型实际引用；引用来自当前用户知识库，不代表网站内容已自动核实。')
        st.json({'retrieved':plan.get('knowledge_references',[]),'cited':plan.get('cited_sources',[])})
    # 显示状态
    if "status_lines" in st.session_state and st.session_state.status_lines:
        with st.expander("🔍 规划过程", expanded=False):
            st.markdown("\n".join(st.session_state.status_lines))

    # ---- 标题 ----
    city = plan.get("city", "")
    sd = plan.get("start_date", "")
    ed = plan.get("end_date", "")
    st.markdown(
        f'<div class="plan-title">🌴 {city}旅行计划 ｜ {sd} ~ {ed}</div>',
        unsafe_allow_html=True,
    )

    # ---- 天气卡片 ----
    weather = plan.get("weather_info", [])
    if weather:
        st.markdown("##### 🌤️ 天气预报")
        cols = st.columns(len(weather))
        weather_icon_map = {
            "晴": "☀️", "多云": "⛅", "阴": "☁️",
            "小雨": "🌧️", "中雨": "🌧️", "大雨": "⛈️", "暴雨": "⛈️",
        }

        for i, w in enumerate(weather):
            d = w.get("date", "")[-5:]
            di = weather_icon_map.get(w.get("day_weather", ""), "🌡️")
            with cols[i]:
                st.markdown(
                    f"""<div class="weather-card" style="text-align:center">
                    <b>{d}</b><br>
                    {di} {w.get('day_weather', '?')}<br>
                    🌡️ {w.get('day_temp', '?')}°C / {w.get('night_temp', '?')}°C<br>
                    💨 {w.get('wind_direction', '')}{w.get('wind_power', '')}
                    </div>""",
                    unsafe_allow_html=True,
                )

    # ---- 每日行程 ----
    st.markdown("---")
    st.markdown("##### 📅 每日行程")

    days = plan.get("days", [])
    if days:
        tabs = st.tabs([f"Day {d.get('day_index', i) + 1}" for i, d in enumerate(days)])
        for i, (tab, day) in enumerate(zip(tabs, days)):
            with tab:
                d = day.get("date", "")[-5:]
                desc = day.get("description", "")

                st.markdown(f'<div class="day-header">📅 {d}  {desc}</div>', unsafe_allow_html=True)

                # 住宿
                hotel = (day.get("hotel") or {})
                if hotel.get("name"):
                    st.markdown(
                        f"🏨 **{hotel['name']}**  ★{hotel.get('rating', '-')}  "
                        f"¥{hotel.get('estimated_cost', 0)}/晚  |  {hotel.get('address', '')}"
                    )
                st.caption(f"🚌 {day.get('transportation', '')}")

                # 景点
                attractions = day.get("attractions", [])
                if attractions:
                    st.markdown("**🏛️ 景点**")
                    for a in attractions:
                        ticket = a.get("ticket_price", 0)
                        ts = "🆓 免费" if ticket == 0 else f"🎫 ¥{ticket}"
                        with st.container(border=True):
                            st.markdown(
                                f"**{a.get('name', '?')}**  |  {a.get('category', '')}  |  "
                                f"⏱️ {a.get('visit_duration', 0)}分钟  |  {ts}"
                            )
                            st.caption(a.get("address", ""))
                            if a.get("description"):
                                st.caption(a["description"])

                # 餐饮
                meals = day.get("meals", [])
                if meals:
                    st.markdown("**🍽️ 餐饮推荐**")
                    mt = {"breakfast": "🌅 早餐", "lunch": "☀️ 午餐", "dinner": "🌙 晚餐"}
                    meal_cols = st.columns(len(meals))
                    for j, m in enumerate(meals):
                        label = mt.get(m.get("type", ""), "餐")
                        with meal_cols[j]:
                            st.markdown(
                                f"*{label}*\n\n**{m.get('name', '?')}**  \n"
                                f"¥{m.get('estimated_cost', 0)}"
                            )

    # ---- 预算 ----
    budget = plan.get("budget", {})
    if budget:
        st.markdown("---")
        st.markdown("##### 💰 预算汇总")
        bc1, bc2, bc3, bc4, bc5 = st.columns(5)
        with bc1:
            st.metric("景点门票", f"¥{budget.get('total_attractions', 0):,}")
        with bc2:
            st.metric("酒店住宿", f"¥{budget.get('total_hotels', 0):,}")
        with bc3:
            st.metric("餐饮美食", f"¥{budget.get('total_meals', 0):,}")
        with bc4:
            st.metric("交通出行", f"¥{budget.get('total_transportation', 0):,}")
        with bc5:
            st.metric("📊 总计", f"¥{budget.get('total', 0):,}",
                      delta=None, delta_color="off")

    # ---- 建议 ----
    suggestions = plan.get("overall_suggestions", "")
    if suggestions:
        st.markdown("---")
        st.markdown("##### 💡 旅行建议")
        for tip in suggestions.replace("；", ";").split(";"):
            tip = tip.strip()
            if tip:
                st.markdown(f"- {tip}")

    # ============ 导出 ============
    st.markdown("---")
    st.markdown("##### 📥 导出计划")

    def _build_markdown(p):
        md = f"# 🌴 {p.get('city', '')}旅行计划\n\n"
        md += f"**日期:** {p.get('start_date', '')} ~ {p.get('end_date', '')}\n\n"

        md += "## 🌤️ 天气预报\n\n"
        for w in p.get("weather_info", []):
            md += (
                f"- {w.get('date', '')[-5:]}: "
                f"{w.get('day_weather', '')}/{w.get('night_weather', '')}  "
                f"{w.get('day_temp', '')}°C~{w.get('night_temp', '')}°C  "
                f"{w.get('wind_direction', '')}{w.get('wind_power', '')}\n"
            )

        md += "\n## 📅 每日行程\n\n"
        for day in p.get("days", []):
            idx = day.get("day_index", 0) + 1
            md += f"### Day {idx} — {day.get('date', '')[-5:]}  {day.get('description', '')}\n\n"
            h = (day.get("hotel") or {})
            if h.get("name"):
                md += f"- **住宿:** {h['name']}  ★{h.get('rating', '')}  ¥{h.get('estimated_cost', 0)}/晚  |  {h.get('address', '')}\n"
            md += f"- **交通:** {day.get('transportation', '')}\n"
            for a in day.get("attractions", []):
                t = "免费" if a.get("ticket_price", 0) == 0 else f"¥{a.get('ticket_price', 0)}"
                md += f"  - **{a.get('name', '')}** ({a.get('category', '')})  ⏱️{a.get('visit_duration', 0)}分钟  {t}  |  {a.get('address', '')}\n"
            for m in day.get("meals", []):
                mt = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
                md += f"  - {mt.get(m.get('type', ''), '餐')}: {m.get('name', '')}  ¥{m.get('estimated_cost', 0)}\n"
            md += "\n"

        b = p.get("budget", {})
        if b:
            md += "## 💰 预算汇总\n\n"
            md += f"| 项目 | 金额 |\n|------|------|\n"
            md += f"| 景点门票 | ¥{b.get('total_attractions', 0):,} |\n"
            md += f"| 酒店住宿 | ¥{b.get('total_hotels', 0):,} |\n"
            md += f"| 餐饮美食 | ¥{b.get('total_meals', 0):,} |\n"
            md += f"| 交通出行 | ¥{b.get('total_transportation', 0):,} |\n"
            md += f"| **总计** | **¥{b.get('total', 0):,}** |\n"

        sug = p.get("overall_suggestions", "")
        if sug:
            md += "\n## 💡 旅行建议\n\n"
            for tip in sug.replace("；", ";").split(";"):
                tip = tip.strip()
                if tip:
                    md += f"- {tip}\n"
        return md

    md_content = _build_markdown(plan)
    st.download_button(
        label="📄 下载 Markdown",
        data=md_content,
        file_name=f"{plan.get('city', '旅行')}_旅行计划.md",
        mime="text/markdown",
        use_container_width=True,
    )

elif st.session_state.get("plan_raw"):
    # 兜底：模型返回了内容但没能解析成结构化行程时，
    # 至少把原始输出摆出来，而不是让用户面对一片空白。
    st.error("⚠️ 未能从模型输出中解析出结构化行程。以下是原始输出，可据此人工整理：")
    if st.session_state.get("status_lines"):
        with st.expander("🔍 规划过程", expanded=False):
            st.markdown("\n".join(st.session_state.status_lines))
    with st.expander("📄 原始输出", expanded=True):
        st.code(st.session_state.plan_raw, language="json")

