"""持久化：工单落库、接口一致性、知识库索引落盘与复用、分块行为。"""

import pytest
from memory.long_term import LongTermMemory
from persistence import Database, SqlTicketStore


def _new_store(db_path):
    database = Database(db_path)
    database.create_all()
    return database, SqlTicketStore(database)


def test_ticket_survives_new_connection(tmp_dir):
    """新建 Database 实例（模拟进程重启）后仍能读到工单"""
    db_path = tmp_dir / "app.db"

    database, store = _new_store(db_path)
    ticket = store.create("refund", "high", "买错了产品", "详情", "user-A")
    store.update_status(ticket["ticket_id"], "processing")
    database.dispose()

    database2, store2 = _new_store(db_path)
    reloaded = store2.query(ticket["ticket_id"])
    assert reloaded is not None
    assert reloaded["summary"] == "买错了产品"
    assert reloaded["status"] == "processing"
    assert reloaded["user_id"] == "user-A"
    assert len(store2.query_by_user("user-A")) == 1
    assert store2.query("TK-NOPE") is None
    database2.dispose()


def test_both_store_implementations_expose_same_fields(tmp_dir, ticket_store):
    database, sql_store = _new_store(tmp_dir / "app.db")
    memory_ticket = ticket_store.create("refund", "low", "x", "y", "u1")
    sql_ticket = sql_store.create("refund", "low", "x", "y", "u1")
    assert set(memory_ticket.keys()) == set(sql_ticket.keys())
    database.dispose()


def test_sql_store_enforces_transition_rules(tmp_dir):
    database, store = _new_store(tmp_dir / "app.db")
    ticket = store.create("refund", "low", "x", "y", "u1")
    store.update_status(ticket["ticket_id"], "processing")
    _, error = store.update_status(ticket["ticket_id"], "created")
    assert error is not None
    assert store.query(ticket["ticket_id"])["status"] == "processing"
    database.dispose()


def test_table_shape(tmp_dir):
    from sqlalchemy import inspect as sa_inspect

    database, _ = _new_store(tmp_dir / "app.db")
    inspector = sa_inspect(database.engine)
    assert "tickets" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("tickets")}
    assert {"ticket_id", "type", "status", "user_id", "created_at", "updated_at"} <= columns
    assert inspector.get_pk_constraint("tickets")["constrained_columns"] == ["ticket_id"]
    assert any(i["column_names"] == ["user_id"] for i in inspector.get_indexes("tickets"))
    database.dispose()


def test_knowledge_index_is_rebuilt_then_reused(tmp_dir):
    kb_dir = tmp_dir / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "refund.md").write_text(
        "# 退款政策\n\n购买后 7 天内可申请无理由退款。\n\n超过 7 天需提供合理原因。\n",
        encoding="utf-8",
    )
    (kb_dir / "ignore.json").write_text('{"not": "knowledge"}', encoding="utf-8")

    index_path = tmp_dir / "index"
    first = LongTermMemory(index_path=index_path).load_or_rebuild(kb_dir)
    assert first["rebuilt"] is True
    assert first["sources"] == 1  # 只读 md / txt
    assert (index_path.with_suffix(".meta.json")).exists()

    second = LongTermMemory(index_path=index_path).load_or_rebuild(kb_dir)
    assert second["rebuilt"] is False
    assert second["documents"] == first["documents"]


def test_knowledge_search_finds_documents(tmp_dir):
    memory = LongTermMemory(index_path=tmp_dir / "index")
    memory.add_document("退款政策：购买后7天内可申请无理由退款。", source="refund.md")
    memory.add_document("开户流程：准备身份证原件。", source="account.md")

    hits = memory.search("退款 几天")
    assert hits and hits[0]["source"] == "refund.md"
    assert memory.search("开户需要什么材料")[0]["source"] == "account.md"
    assert memory.search("") == []


def test_chunking_is_paragraph_aware():
    chunks = LongTermMemory.chunk_text(
        "# 标题\n\n第一段内容。\n\n第二段内容。", chunk_size=12, overlap=2
    )
    assert len(chunks) >= 2
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 12 + 2 for chunk in chunks)


def test_chunk_text_rejects_bad_parameters():
    with pytest.raises(ValueError):
        LongTermMemory.chunk_text("abc", chunk_size=10, overlap=10)


def test_add_document_is_idempotent(tmp_dir):
    memory = LongTermMemory(index_path=tmp_dir / "index")
    first = memory.add_document("同样的内容", source="a.md")
    second = memory.add_document("同样的内容", source="a.md")
    assert first == second
    assert len(memory._documents) == 1
