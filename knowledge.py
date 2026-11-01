"""中文双字词BM25检索：不需要另购Embedding接口，来源及新鲜度随片段返回。"""
from collections import Counter
from datetime import date
import hashlib
import json
import math
import re
from urllib.parse import urlsplit,parse_qs
from uuid import uuid4
from sqlalchemy import String,Text,JSON,Integer,ForeignKey,select,delete,func
from sqlalchemy.orm import Mapped,mapped_column
from storage import Base,ConversationNotFound


class Document(Base):
    __tablename__='travel_documents'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    owner_id:Mapped[str]=mapped_column(String(32))
    title:Mapped[str]=mapped_column(String(200))
    city:Mapped[str]=mapped_column(String(100))
    source_url:Mapped[str]=mapped_column(Text)
    content_hash:Mapped[str]=mapped_column(String(64))
    updated_at:Mapped[str]=mapped_column(String(10))
    expires_on:Mapped[str]=mapped_column(String(10))
    facts:Mapped[list]=mapped_column(JSON)


class Chunk(Base):
    __tablename__='travel_chunks'
    id:Mapped[int]=mapped_column(Integer,primary_key=True)
    document_id:Mapped[str]=mapped_column(ForeignKey('travel_documents.id'))
    position:Mapped[int]=mapped_column(Integer)
    content:Mapped[str]=mapped_column(Text)


def tokens(text):
    result=re.findall(r'[a-z0-9]+',text.lower())
    for word in re.findall(r'[\u4e00-\u9fff]+',text):result.extend(word[i:i+2] for i in range(max(1,len(word)-1)))
    return result


class KnowledgeStore:
    def __init__(self,store):self.store=store

    def put(self,title,city,source_url,content,expires_on,*,document_id=None,facts=None):
        from fact_checks import FactRule
        facts=[FactRule.model_validate(f).model_dump(mode='json') for f in (facts or [])]
        parsed=urlsplit(source_url)
        if (parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password
                or any(any(w in k.lower() for w in ('key','token','secret')) for k in parse_qs(parsed.query))):
            raise ValueError('来源必须是无凭据的HTTPS链接')
        if not 1<=len(title)<=200 or not 1<=len(city)<=100 or not 1<=len(content)<=50000:raise ValueError('资料长度无效')
        if date.fromisoformat(expires_on)<date.today():raise ValueError('资料已过期')
        identity=document_id or uuid4().hex
        with self.store.sessions.begin() as session:
            old=session.get(Document,identity)
            if document_id and (old is None or old.owner_id!=self.store.owner_id):raise ConversationNotFound('资料不存在')
            if not document_id and session.scalar(select(func.count()).select_from(Document).where(Document.owner_id==self.store.owner_id))>=50:
                raise ValueError('每个用户最多保存50份资料')
            if old:
                session.execute(delete(Chunk).where(Chunk.document_id==identity));document=old
            else:
                document=Document(id=identity,owner_id=self.store.owner_id);session.add(document)
            document.title=title;document.city=city;document.source_url=source_url
            snapshot=json.dumps({'content':content,'facts':facts,'source_url':source_url,'title':title,'expires_on':expires_on},ensure_ascii=False,sort_keys=True)
            document.content_hash=hashlib.sha256(snapshot.encode()).hexdigest()
            document.updated_at=date.today().isoformat();document.expires_on=expires_on;document.facts=facts or []
            session.flush()
            session.add_all([Chunk(document_id=identity,position=n,content=content[start:start+700]) for n,start in enumerate(range(0,len(content),600))])
        return identity

    def list_documents(self):
        with self.store.sessions() as session:
            rows=session.scalars(select(Document).where(Document.owner_id==self.store.owner_id)).all()
            return [{'id':d.id,'title':d.title,'city':d.city,'source_url':d.source_url,'updated_at':d.updated_at,'expires_on':d.expires_on,'content_hash':d.content_hash} for d in rows]

    def retrieve(self,query,city,limit=4):
        with self.store.sessions() as session:
            rows=session.execute(select(Chunk,Document).join(Document,Document.id==Chunk.document_id).where(Document.owner_id==self.store.owner_id,Document.city.in_([city,'通用']),Document.expires_on>=date.today().isoformat()).order_by(Document.id,Chunk.position)).all()
            if not rows:return []
            bags=[Counter(tokens(c.content)) for c,d in rows];query_tokens=set(tokens(query));average=sum(sum(b.values()) for b in bags)/len(bags) or 1
            df=Counter(t for b in bags for t in b)
            ranked=[]
            for (chunk,doc),bag in zip(rows,bags):
                length=sum(bag.values());score=0
                for t in query_tokens & bag.keys():
                    frequency=bag[t];idf=math.log(1+(len(rows)-df[t]+.5)/(df[t]+.5))
                    score+=idf*frequency*2.5/(frequency+1.5*(.25+.75*length/average))
                if score>0:ranked.append((score,chunk,doc))
            ranked.sort(key=lambda x:(-x[0],x[2].id,x[1].position))
            return [{'id':f'K{d.id[:12]}_{c.position}_{d.content_hash[:8]}','document_id':d.id,'chunk':c.position,'title':d.title,'source_url':d.source_url,'content_hash':d.content_hash,'updated_at':d.updated_at,'expires_on':d.expires_on,'content':c.content,'score':round(score,4)} for score,c,d in ranked[:max(1,min(limit,8))]]

    def facts(self,city):
        with self.store.sessions() as session:
            docs=session.scalars(select(Document).where(Document.owner_id==self.store.owner_id,Document.city==city,Document.expires_on>=date.today().isoformat())).all()
            return [{**f,'source_url':d.source_url,'document_id':d.id} for d in docs for f in d.facts]
