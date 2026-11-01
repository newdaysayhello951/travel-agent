"""Argon2密码哈希、数据库中仅存哈希的可撤销Bearer会话。"""
import hashlib
import secrets
import time
from uuid import uuid4
from pwdlib import PasswordHash
from sqlalchemy import String,Text,BigInteger,ForeignKey,select,delete
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy.exc import IntegrityError
from storage import Base


class User(Base):
    __tablename__='travel_users'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    username:Mapped[str]=mapped_column(String(40),unique=True)
    password_hash:Mapped[str]=mapped_column(Text)


class Session(Base):
    __tablename__='travel_sessions'
    token_hash:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(ForeignKey('travel_users.id'))
    expires_at:Mapped[int]=mapped_column(BigInteger)


class AuthenticationError(ValueError):pass
class UsernameUnavailable(ValueError):pass


class AuthStore:
    def __init__(self,store):
        self.sessions=store.sessions;self.passwords=PasswordHash.recommended()
        self.dummy=self.passwords.hash(secrets.token_urlsafe(24))

    def register(self,username,password):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_]{3,40}',username) or not 12<=len(password)<=128:
            raise ValueError('用户名须为3至40位字母数字下划线，密码长度12至128位')
        identity=uuid4().hex;hashed=self.passwords.hash(password)
        try:
            with self.sessions.begin() as session:session.add(User(id=identity,username=username.lower(),password_hash=hashed))
        except IntegrityError:raise UsernameUnavailable('用户名不可用') from None
        return identity

    def login(self,username,password):
        with self.sessions() as session:
            user=session.scalar(select(User).where(User.username==username.lower()))
            valid=self.passwords.verify(password,user.password_hash if user else self.dummy)
            if not valid or user is None:raise AuthenticationError('用户名或密码错误')
            identity=user.id
        token=secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            session.execute(delete(Session).where(Session.expires_at<=int(time.time())))
            session.add(Session(token_hash=self.digest(token),user_id=identity,expires_at=int(time.time())+3600))
        return token

    @staticmethod
    def digest(token):return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self,token):
        if len(token)>256:raise AuthenticationError('登录已失效')
        with self.sessions() as session:
            row=session.get(Session,self.digest(token))
            if row is None or row.expires_at<=int(time.time()):raise AuthenticationError('登录已失效')
            return row.user_id

    def logout(self,token):
        with self.sessions.begin() as session:session.execute(delete(Session).where(Session.token_hash==self.digest(token)))
