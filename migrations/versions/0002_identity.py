"""用户、可撤销会话与会话所有者。"""
from alembic import op
import sqlalchemy as sa
revision='0002'
down_revision='0001'

def upgrade():
    op.create_table('travel_users',sa.Column('id',sa.String(32),primary_key=True),sa.Column('username',sa.String(40),nullable=False,unique=True),sa.Column('password_hash',sa.Text,nullable=False))
    op.create_table('travel_sessions',sa.Column('token_hash',sa.String(64),primary_key=True),sa.Column('user_id',sa.String(32),sa.ForeignKey('travel_users.id'),nullable=False),sa.Column('expires_at',sa.BigInteger,nullable=False))
    op.add_column('travel_conversations',sa.Column('owner_id',sa.String(32),nullable=False,server_default='local'))
    op.create_index('ix_travel_conversations_owner','travel_conversations',['owner_id'])

def downgrade():raise RuntimeError('涉及用户与历史数据，不提供自动破坏性降级')
