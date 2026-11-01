"""新库升级及原create_all数据库的受控接管，不删除已有数据。"""
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

BASELINE={
    'travel_conversations':{'id','title','current_version','created_at'},
    'travel_versions':{'conversation_id','number','plan','request','instruction','created_at'},
    'travel_runs':{'id','conversation_id','report','created_at'},
    'travel_messages':{'id','conversation_id','role','content'},
}

def upgrade_database(engine):
    cfg=Config();cfg.set_main_option('script_location',str(Path(__file__).resolve().parent/'migrations'))
    with engine.begin() as connection:
        cfg.attributes['connection']=connection
        inspector=inspect(connection);tables=set(inspector.get_table_names())
        if 'alembic_version' not in tables and tables.intersection(BASELINE):
            if not set(BASELINE)<=tables or any({c['name'] for c in inspector.get_columns(t)}!=columns for t,columns in BASELINE.items()):
                raise RuntimeError('旧数据库结构不匹配，拒绝自动标记迁移版本，请先备份并检查')
            command.stamp(cfg,'0001')
        command.upgrade(cfg,'head')
