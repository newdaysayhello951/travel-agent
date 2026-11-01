"""认证HTTP接口；用户身份来自Bearer会话，不能由请求体指定owner。"""
import asyncio
from collections import OrderedDict,deque
from dataclasses import asdict
from threading import Lock
import time
from fastapi import FastAPI,Depends,HTTPException,Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel,ConfigDict,Field
from auth import AuthStore,AuthenticationError,UsernameUnavailable
from schemas import TripRequest,PlanValidationError
from storage import PlanStore,ConversationNotFound,VersionConflict
from reliability import ExecutionLimitError,PlanningServiceError
from travel_service import TravelService
from fact_checks import FactRule
from datetime import date


class Credentials(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=40)
    password:str=Field(min_length=12,max_length=128)


class PlanInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    query:str=Field(min_length=1,max_length=10000)
    request:TripRequest


class RevisionInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instruction:str=Field(min_length=1,max_length=5000)
    expected_version:int=Field(ge=1)
    request_updates:dict=Field(default_factory=dict)
    preserve_hotels:bool=True


class DocumentInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    title:str=Field(min_length=1,max_length=200)
    city:str=Field(min_length=1,max_length=100)
    source_url:str=Field(max_length=2000)
    content:str=Field(min_length=1,max_length=50000)
    expires_on:date
    facts:list[FactRule]=Field(default_factory=list,max_length=100)


class RateLimiter:
    def __init__(self):self.entries=OrderedDict();self.lock=Lock()
    def check(self,key,limit,window=60):
        with self.lock:
            now=time.monotonic();events=self.entries.pop(key,deque())
            while events and events[0]<=now-window:events.popleft()
            self.entries[key]=events
            while len(self.entries)>2048:self.entries.popitem(last=False)
            if len(events)>=limit:raise HTTPException(429,'请求过于频繁，请稍后重试',headers={'Retry-After':str(window)})
            events.append(now)


def create_app(store=None,planner_factory=None):
    if store is None:
        from config import CONFIG  # 加载.env
        store=PlanStore()
    auth=AuthStore(store);limiter=RateLimiter();gate=asyncio.Semaphore(2)
    bearer=HTTPBearer(auto_error=False)
    app=FastAPI(title='旅行规划服务',version='1.0.0')

    @app.exception_handler(RequestValidationError)
    async def malformed(request,exc):
        return JSONResponse(status_code=422,content={'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in exc.errors()]})

    def identity(credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
        if credentials is None or credentials.scheme.lower()!='bearer':raise HTTPException(401,'请先登录',headers={'WWW-Authenticate':'Bearer'})
        try:return auth.authenticate(credentials.credentials)
        except AuthenticationError:raise HTTPException(401,'登录已失效',headers={'WWW-Authenticate':'Bearer'}) from None

    def scope(user=Depends(identity)):return store.for_owner(user)

    @app.exception_handler(ConversationNotFound)
    async def not_found(request,exc):return JSONResponse(status_code=404,content={'detail':'会话或版本不存在'})
    @app.exception_handler(VersionConflict)
    async def conflict(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.exception_handler(PlanValidationError)
    async def invalid_plan(request,exc):return JSONResponse(status_code=422,content={'detail':'行程未通过校验','issues':exc.issues})
    @app.exception_handler(ValueError)
    async def invalid_input(request,exc):return JSONResponse(status_code=400,content={'detail':'输入或约束无效'})
    @app.exception_handler(TimeoutError)
    async def timeout(request,exc):return JSONResponse(status_code=504,content={'detail':'规划超时'})
    @app.exception_handler(ExecutionLimitError)
    async def budget(request,exc):return JSONResponse(status_code=429,content={'detail':'本次规划达到执行预算上限'})
    @app.exception_handler(PlanningServiceError)
    async def upstream(request,exc):return JSONResponse(status_code=503,content={'detail':'上游服务暂不可用'})
    @app.exception_handler(Exception)
    async def unexpected(request,exc):return JSONResponse(status_code=500,content={'detail':'服务处理失败，请检查服务端记录'})

    @app.get('/health')
    def health():return {'status':'ok'}
    @app.post('/auth/register',status_code=201)
    def register(body:Credentials,request:Request):
        limiter.check(('register',request.client.host),5)
        try:return {'user_id':auth.register(body.username,body.password)}
        except UsernameUnavailable:raise HTTPException(409,'用户名不可用') from None
    @app.post('/auth/login')
    def login(body:Credentials,request:Request):
        limiter.check(('login',request.client.host),10)
        try:return {'access_token':auth.login(body.username,body.password),'token_type':'bearer','expires_in':3600}
        except AuthenticationError:raise HTTPException(401,'用户名或密码错误') from None
    @app.post('/auth/logout',status_code=204)
    def logout(user=Depends(identity),credentials:HTTPAuthorizationCredentials=Depends(bearer)):
        auth.logout(credentials.credentials)
    @app.get('/me')
    def me(user=Depends(identity)):return {'user_id':user}
    @app.get('/conversations')
    def conversations(scoped=Depends(scope)):return scoped.list_conversations()
    @app.get('/knowledge')
    def documents(scoped=Depends(scope)):
        from knowledge import KnowledgeStore
        return KnowledgeStore(scoped).list_documents()
    def save_document(body,scoped,document_id=None):
        from knowledge import KnowledgeStore
        fields=body.model_dump(mode='json')
        return {'document_id':KnowledgeStore(scoped).put(**fields,document_id=document_id)}
    @app.post('/knowledge',status_code=201)
    def add_document(body:DocumentInput,scoped=Depends(scope)):return save_document(body,scoped)
    @app.put('/knowledge/{document_id}')
    def update_document(document_id:str,body:DocumentInput,scoped=Depends(scope)):return save_document(body,scoped,document_id)
    @app.get('/knowledge/search')
    def search_knowledge(query:str,city:str,scoped=Depends(scope)):
        from knowledge import KnowledgeStore
        if len(query)>10000 or len(city)>100:raise HTTPException(422,'检索参数过长')
        return KnowledgeStore(scoped).retrieve(query,city)
    @app.get('/conversations/{cid}')
    def latest(cid:str,scoped=Depends(scope)):
        saved=scoped.get_plan(cid);return asdict(saved) if saved else {'version':0}
    @app.get('/conversations/{cid}/versions/{version}')
    def version(cid:str,version:int,scoped=Depends(scope)):
        if version<1:raise HTTPException(422,'版本号须大于0')
        return asdict(scoped.get_plan(cid,version))
    @app.get('/conversations/{cid}/history')
    def history(cid:str,scoped=Depends(scope)):return scoped.history(cid)

    async def generate(scoped,operation):
        limiter.check(('plan',scoped.owner_id),5)
        if gate.locked():raise HTTPException(503,'规划服务繁忙，请稍后重试',headers={'Retry-After':'5'})
        async with gate:
            if planner_factory is None:
                from config import CONFIG
                from agents.planner import TripPlanner
                planner=TripPlanner(CONFIG.create_llm())
            else:planner=planner_factory()
            try:return asdict(await operation(TravelService(planner,scoped)))
            finally:
                if getattr(planner,'mcp',None):await planner.mcp.close()

    @app.post('/plans',status_code=201)
    async def plan(body:PlanInput,scoped=Depends(scope)):
        return await generate(scoped,lambda service:service.create(body.query,body.request))
    @app.post('/conversations/{cid}/revisions',status_code=201)
    async def revise(cid:str,body:RevisionInput,scoped=Depends(scope)):
        scoped.get_plan(cid)  # 越权请求在模型初始化与付费请求之前拒绝
        return await generate(scoped,lambda service:service.revise(cid,body.instruction,expected_version=body.expected_version,
            request_updates=body.request_updates,preserve_hotels=body.preserve_hotels))
    return app


def app_factory():return create_app()
