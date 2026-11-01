"""显式运行的真实浏览器回归。full会付费调用模型，勿放入单元测试发现路径。"""
import argparse,json,re,sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8513');p.add_argument('--credentials',default='.work/browser-credentials.json')
    p.add_argument('--phase',choices=['prepare','full'],default='prepare');p.add_argument('--channel',default='msedge');args=p.parse_args()
    if urlsplit(args.url).hostname not in ('localhost','127.0.0.1','::1'):raise ValueError('此测试只允许本机隔离服务')
    credentials=json.loads(Path(args.credentials).read_text(encoding='utf-8'))
    work=Path('.work');result={'phase':args.phase,'checks':[]};page=None
    with sync_playwright() as pw:
        profile=work/f'browser-profile-{uuid4().hex}';artifacts=work/f'browser-artifacts-{uuid4().hex}'
        profile.mkdir(parents=True);artifacts.mkdir(parents=True)
        context=pw.chromium.launch_persistent_context(str(profile.resolve()),channel=args.channel,headless=True,
            artifacts_dir=str(artifacts.resolve()),viewport={'width':1440,'height':1100})
        browser=context.browser;page=context.new_page();page.set_default_timeout(30000)
        def login():
            page.get_by_label('用户名',exact=True).fill(credentials['username'])
            page.get_by_label('密码（12至128位）',exact=True).fill(credentials['password'])
            page.get_by_role('button',name='登录',exact=True).click()
            page.get_by_label('📍 目的地城市',exact=True).wait_for()
        def load_latest(cid):
            option=page.get_by_role('option',name=re.compile(cid[:6]))
            for attempt in range(3):
                page.get_by_label('选择会话',exact=True).click()
                try:option.wait_for(timeout=3000);break
                except Exception:
                    page.keyboard.press('Escape')
                    if attempt==2:raise
            option.click()
            page.get_by_role('button',name='加载此版本',exact=True).click()
        def set_date(label,iso):
            control=page.get_by_label(label,exact=True)
            if control.get_attribute('role')=='group':
                for part,target in zip(('year','month','day'),map(int,iso.split('-'))):
                    segment=control.get_by_role('spinbutton',name=f'{part}, {label}',exact=True)
                    difference=target-int(segment.get_attribute('aria-valuenow'))
                    if abs(difference)>40:raise ValueError('测试日期偏离当前控件范围过大')
                    for _ in range(abs(difference)):segment.press('ArrowUp' if difference>0 else 'ArrowDown')
                    assert int(segment.get_attribute('aria-valuenow'))==target
                page.get_by_label('📍 目的地城市',exact=True).click()
                return
            value=iso if control.get_attribute('type')=='date' else iso.replace('-','/')
            control.fill(value);control.press('Tab')
            assert control.input_value().replace('/','-')==iso
        try:
            page.goto(args.url);login();result['checks'].append('browser_login')
            page.get_by_text('知识库资料导入与更新',exact=True).click()
            page.get_by_label('更新目标',exact=True).click()
            page.get_by_role('option',name=re.compile('岳麓书院参观摘要')).wait_for()
            page.keyboard.press('Escape')
            result['checks'].append('scoped_knowledge_visible')
            if args.phase=='prepare':
                page.get_by_label('📍 目的地城市',exact=True).fill('长沙')
                for label,value in [('📅 开始日期','2026-11-10'),('📅 结束日期','2026-11-11')]:set_date(label,value)
                result['checks'].append('browser_date_inputs')
            page.screenshot(path=str(work/'browser-authenticated.png'),full_page=True)
            if args.phase=='full':
                from storage import PlanStore
                store=PlanStore(credentials['database_url'],owner_id=credentials['user_id'])
                rows=[c for c in store.list_conversations() if c['version']>0]
                if not rows:
                    page.get_by_label('📍 目的地城市',exact=True).fill('长沙')
                    set_date('📅 开始日期','2026-11-10')
                    set_date('📅 结束日期','2026-11-11')
                    page.get_by_label('总预算上限（元，0表示不限）',exact=True).fill('1800')
                    page.get_by_label('补充说明',exact=True).fill('一个成人，喜欢历史文化，每天最多两个景点，第一天包含岳麓书院；参考知识库并引用来源。基础入园门票和观光交通费分开，未知信息不要编造。')
                    page.get_by_role('button',name='🚀 开始规划',exact=True).click()
                    page.get_by_text(re.compile('当前查看版本 v1')).wait_for(timeout=220000)
                    rows=[c for c in store.list_conversations() if c['version']>0]
                cid=rows[0]['id'];before=store.get_plan(cid)
                if not page.get_by_text(re.compile('当前查看版本')).count():load_latest(cid)
                result['checks'].append('browser_generate_and_persist')
                result['initial_citations']=len(before.plan.get('cited_sources',[]))
                page.screenshot(path=str(work/'browser-generated.png'),full_page=True)
                if before.version==1:
                    page.get_by_label('继续修改行程',exact=True).fill('保持现有景点、酒店及报价不变，只在总体建议补充雨天携带雨具和防滑鞋的准备，不编造未来逐日天气。保留当前有效来源引用。')
                    page.get_by_role('button',name='保存为新版本',exact=True).click()
                    page.get_by_text(re.compile('当前查看版本 v2')).wait_for(timeout=220000)
                after=store.get_plan(cid)
                assert after.version>=2
                result['final_citations']=len(after.plan.get('cited_sources',[]))
                assert result['final_citations']>0,'explicit source citation was requested'
                first=store.get_plan(cid,1).plan
                hotel=first['days'][0]['hotel'];new_hotel=after.plan['days'][0]['hotel']
                assert all(hotel.get(k)==new_hotel.get(k) for k in ('name','address','location','estimated_cost'))
                old_places=[(d['date'],a['name'],a['ticket_price']) for d in first['days'] for a in d['attractions']]
                new_places=[(d['date'],a['name'],a['ticket_price']) for d in after.plan['days'] for a in d['attractions']]
                assert sorted(old_places)==sorted(new_places),'rain advice must not change attractions or prices'
                result['checks'].append('browser_revise_and_preserve_hotel')
                page.reload()
                page.get_by_label('用户名',exact=True).or_(page.get_by_label('选择会话',exact=True)).first.wait_for()
                if page.get_by_label('用户名',exact=True).count():login()
                else:page.get_by_label('选择会话',exact=True).wait_for()
                load_latest(cid);page.get_by_text(re.compile(f'当前查看版本 v{after.version}')).wait_for()
                result['checks'].append('browser_reload_and_restore')
                result['versions']=store.history(cid)['versions'];store.close()
                page.screenshot(path=str(work/'browser-restored.png'),full_page=True)
            result['passed']=True
        except Exception as exc:
            result.update(passed=False,error_type=type(exc).__name__)
            (work/'browser-error-redacted.txt').write_text(str(exc).replace(credentials['password'],'[redacted]'),encoding='utf-8')
            if page:page.screenshot(path=str(work/'browser-failure.png'),full_page=True)
        finally:
            (work/f'browser-{args.phase}-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            browser.close()
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result.get('passed') else 1


if __name__=='__main__':raise SystemExit(main())
