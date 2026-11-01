"""独立持久化层。SQLite可直接本地运行，PostgreSQL使用同一SQLAlchemy模型。"""
from dataclasses import asdict,dataclass
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from uuid import uuid4
from copy import copy

from sqlalchemy import JSON,DateTime,ForeignKey,Integer,String,Text,create_engine,event,select,update
from sqlalchemy.engine import make_url,URL
from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column,sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):pass


class Conversation(Base):
    __tablename__='travel_conversations'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    title:Mapped[str]=mapped_column(String(200))
    current_version:Mapped[int]=mapped_column(Integer,default=0)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
    owner_id:Mapped[str]=mapped_column(String(32),default='local',server_default='local')


class Version(Base):
    __tablename__='travel_versions'
    conversation_id:Mapped[str]=mapped_column(ForeignKey('travel_conversations.id'),primary_key=True)
    number:Mapped[int]=mapped_column(Integer,primary_key=True)
    plan:Mapped[dict]=mapped_column(JSON)
    request:Mapped[dict]=mapped_column(JSON)
    instruction:Mapped[str]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__='travel_runs'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    conversation_id:Mapped[str]=mapped_column(ForeignKey('travel_conversations.id'))
    report:Mapped[dict]=mapped_column(JSON)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__='travel_messages'
    id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    conversation_id:Mapped[str]=mapped_column(ForeignKey('travel_conversations.id'))
    role:Mapped[str]=mapped_column(String(16))
    content:Mapped[str]=mapped_column(Text)


class ConversationNotFound(LookupError):pass
class VersionConflict(RuntimeError):pass


@dataclass
class SavedPlan:
    conversation_id:str
    version:int
    plan:dict
    request:dict


def now():return datetime.now(timezone.utc)


class PlanStore:
    def __init__(self,database_url=None,*,owner_id='local'):
        self.owner_id=owner_id
        default=URL.create('sqlite',database=str(Path(__file__).resolve().parent/'.data/travel-agent.db'))
        url=make_url(database_url or os.getenv('DATABASE_URL') or default)
        kwargs={'pool_pre_ping':True,'hide_parameters':True}
        if url.get_backend_name()=='sqlite':
            kwargs['connect_args']={'check_same_thread':False,'timeout':15}
            if url.database in (None,'',':memory:'):kwargs['poolclass']=StaticPool
            else:Path(url.database).resolve().parent.mkdir(parents=True,exist_ok=True)
        self.engine=create_engine(url,**kwargs)
        if url.get_backend_name()=='sqlite':
            @event.listens_for(self.engine,'connect')
            def foreign_keys(connection,_):
                cursor=connection.cursor();cursor.execute('PRAGMA foreign_keys=ON');cursor.close()
        from db_migrations import upgrade_database
        upgrade_database(self.engine)
        self.sessions=sessionmaker(self.engine,expire_on_commit=False)

    def close(self):self.engine.dispose()

    def for_owner(self,owner_id):
        scoped=copy(self);scoped.owner_id=owner_id
        return scoped

    def _owned(self,session,conversation_id):
        row=session.scalar(select(Conversation).where(Conversation.id==conversation_id,Conversation.owner_id==self.owner_id))
        if row is None:raise ConversationNotFound('会话不存在')
        return row

    def create_conversation(self,title):
        identity=uuid4().hex
        with self.sessions.begin() as session:
            session.add(Conversation(id=identity,title=title[:200],current_version=0,created_at=now(),owner_id=self.owner_id))
        return identity

    def list_conversations(self):
        with self.sessions() as session:
            rows=session.scalars(select(Conversation).where(Conversation.owner_id==self.owner_id).order_by(Conversation.created_at.desc())).all()
            return [{'id':r.id,'title':r.title,'version':r.current_version,'created_at':r.created_at.isoformat()} for r in rows]

    def get_plan(self,conversation_id,version=None):
        with self.sessions() as session:
            conversation=self._owned(session,conversation_id)
            number=conversation.current_version if version is None else version
            if number==0:return None
            row=session.get(Version,(conversation_id,number))
            if row is None:raise ConversationNotFound('行程版本不存在')
            return SavedPlan(conversation_id,row.number,row.plan,row.request)

    def save_plan(self,conversation_id,expected_version,plan,request,instruction,report):
        number=expected_version+1
        with self.sessions.begin() as session:
            changed=session.execute(update(Conversation).where(Conversation.id==conversation_id,
                                     Conversation.owner_id==self.owner_id,Conversation.current_version==expected_version).values(current_version=number))
            if changed.rowcount!=1:
                self._owned(session,conversation_id)
                raise VersionConflict('行程已有更新，请重新加载最新版本后修改')
            session.add(Version(conversation_id=conversation_id,number=number,plan=plan,request=request,instruction=instruction,created_at=now()))
            session.add(Run(id=report.run_id,conversation_id=conversation_id,report=asdict(report),created_at=now()))
            session.add_all([Message(conversation_id=conversation_id,role='user',content=instruction),
                             Message(conversation_id=conversation_id,role='assistant',content=json.dumps(plan,ensure_ascii=False))])
        return SavedPlan(conversation_id,number,plan,request)

    def record_outcome(self,conversation_id,instruction,text,report):
        with self.sessions.begin() as session:
            self._owned(session,conversation_id)
            session.add(Run(id=report.run_id,conversation_id=conversation_id,report=asdict(report),created_at=now()))
            session.add_all([Message(conversation_id=conversation_id,role='user',content=instruction),
                             Message(conversation_id=conversation_id,role='assistant',content=text)])

    def history(self,conversation_id):
        with self.sessions() as session:
            self._owned(session,conversation_id)
            versions=session.scalars(select(Version).where(Version.conversation_id==conversation_id).order_by(Version.number)).all()
            messages=session.scalars(select(Message).where(Message.conversation_id==conversation_id).order_by(Message.id)).all()
            runs=session.scalars(select(Run).where(Run.conversation_id==conversation_id).order_by(Run.created_at)).all()
            return {'versions':[r.number for r in versions],
                    'instructions':[{'version':r.number,'instruction':r.instruction} for r in versions],
                    'messages':[{'role':r.role,'content':r.content} for r in messages],
                    'runs':[r.report for r in runs]}
