from alembic import op
import sqlalchemy as sa
revision='0003'
down_revision='0002'

def upgrade():
    op.create_table('travel_documents',sa.Column('id',sa.String(32),primary_key=True),sa.Column('owner_id',sa.String(32),nullable=False),sa.Column('title',sa.String(200),nullable=False),sa.Column('city',sa.String(100),nullable=False),sa.Column('source_url',sa.Text,nullable=False),sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('updated_at',sa.String(10),nullable=False),sa.Column('expires_on',sa.String(10),nullable=False),sa.Column('facts',sa.JSON,nullable=False))
    op.create_index('ix_travel_documents_owner','travel_documents',['owner_id'])
    op.create_table('travel_chunks',sa.Column('id',sa.Integer,primary_key=True),sa.Column('document_id',sa.String(32),sa.ForeignKey('travel_documents.id'),nullable=False),sa.Column('position',sa.Integer,nullable=False),sa.Column('content',sa.Text,nullable=False))

def downgrade():raise RuntimeError('不自动删除知识库，请备份后人工处理')
