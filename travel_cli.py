"""带持久化的命令行入口；历史查询不调用模型。"""
import argparse
import asyncio
import json


def arguments():
    parser=argparse.ArgumentParser(description='旅行会话与行程版本')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('list',help='列出本机保存的会话')
    for name in ('show','history'):
        p=commands.add_parser(name);p.add_argument('conversation_id')
        if name=='show':p.add_argument('--version',type=int)
    p=commands.add_parser('create');p.add_argument('query')
    p.add_argument('--city',required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--budget',type=float);p.add_argument('--stay-last',action='store_true')
    p=commands.add_parser('revise');p.add_argument('conversation_id');p.add_argument('instruction')
    p.add_argument('--expected-version',type=int);p.add_argument('--keep-hotels',action='store_true')
    p.add_argument('--updates',default='{}',help='TripRequest字段JSON，例如预算、日期、必去景点；未知字段拒绝')
    return parser.parse_args()


async def main():
    args=arguments()
    from config import CONFIG
    from storage import PlanStore
    store=PlanStore();planner=None
    try:
        if args.command=='list':result=store.list_conversations()
        elif args.command=='history':result=store.history(args.conversation_id)
        elif args.command=='show':
            from dataclasses import asdict
            saved=store.get_plan(args.conversation_id,args.version)
            result=asdict(saved) if saved else {'message':'尚无合格行程'}
        else:
            from agents.planner import TripPlanner
            from schemas import TripRequest
            from travel_service import TravelService
            planner=TripPlanner(CONFIG.create_llm());service=TravelService(planner,store)
            if args.command=='create':
                request=TripRequest(city=args.city,start_date=args.start,end_date=args.end,
                                    budget_limit=args.budget,stay_on_last_day=args.stay_last)
                saved=await service.create(args.query,request)
            else:
                updates=json.loads(args.updates)
                if not isinstance(updates,dict):raise ValueError('updates必须是JSON对象')
                saved=await service.revise(args.conversation_id,args.instruction,request_updates=updates,
                                           expected_version=args.expected_version,preserve_hotels=args.keep_hotels)
            result={'conversation_id':saved.conversation_id,'version':saved.version,'plan':saved.plan}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        if planner:await planner.mcp.close()
        store.close()


if __name__=='__main__':
    try:asyncio.run(main())
    except KeyboardInterrupt:print('操作已取消')
    except Exception as exc:
        from schemas import PlanValidationError
        from storage import ConversationNotFound,VersionConflict
        from reliability import ExecutionLimitError,PlanningServiceError
        if isinstance(exc,(PlanValidationError,ConversationNotFound,VersionConflict,ExecutionLimitError,PlanningServiceError)):
            print(f'操作失败：{exc}')
        else:print(f'操作失败（{type(exc).__name__}），请检查输入或服务配置。')
        raise SystemExit(1)
