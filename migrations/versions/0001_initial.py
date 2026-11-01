"""冻结初始会话结构，禁止依赖未来ORM模型。"""
from alembic import op
import sqlalchemy as sa
revision='0001'
down_revision=None

def upgrade():
    op.create_table('travel_conversations',sa.Column('id',sa.String(32),primary_key=True),sa.Column('title',sa.String(200),nullable=False),sa.Column('current_version',sa.Integer,nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('travel_versions',sa.Column('conversation_id',sa.String(32),sa.ForeignKey('travel_conversations.id'),primary_key=True),sa.Column('number',sa.Integer,primary_key=True),sa.Column('plan',sa.JSON,nullable=False),sa.Column('request',sa.JSON,nullable=False),sa.Column('instruction',sa.Text,nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('travel_runs',sa.Column('id',sa.String(64),primary_key=True),sa.Column('conversation_id',sa.String(32),sa.ForeignKey('travel_conversations.id'),nullable=False),sa.Column('report',sa.JSON,nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('travel_messages',sa.Column('id',sa.Integer,primary_key=True,autoincrement=True),sa.Column('conversation_id',sa.String(32),sa.ForeignKey('travel_conversations.id'),nullable=False),sa.Column('role',sa.String(16),nullable=False),sa.Column('content',sa.Text,nullable=False))

def downgrade():
    raise RuntimeError('不提供可能删除旅行历史的自动降级；请使用备份恢复')
