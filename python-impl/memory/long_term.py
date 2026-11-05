"""
长期记忆（知识库）— 本地词法检索

未配置真实 Embedding，因此不使用向量：检索依据是「中文按相邻两字切分 + 英文数字按词切分」
得到的词元重合度。

索引来源是磁盘上的知识库目录（默认 knowledge/），分块结果缓存到 `<index_path>.meta.json`；
源文件比索引新时会自动重建，因此知识库改动不需要手工干预。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# 知识库文件后缀：Markdown 为主，同时兼容纯文本
KNOWLEDGE_SUFFIXES = ("*.md", "*.txt")


class LongTermMemory:
    """知识库文档的存储与词法检索"""

    def __init__(self, index_path: str | Path = "./.data/knowledge/index", **kwargs):
        self.index_path = Path(index_path)
        self._documents: list[dict] = []
        metadata = self.index_path.with_suffix(".meta.json")
        if metadata.exists():
            self._documents = json.loads(metadata.read_text(encoding="utf-8"))

    # ── 词元切分与检索 ──

    @staticmethod
    def _terms(text: str) -> set[str]:
        terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        for fragment in re.findall(r"[\u4e00-\u9fff]+", text):
            terms.update(fragment[i:i + 2] for i in range(len(fragment) - 1))
        return terms

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """按词元重合度打分并返回前 top_k 篇（纯计算，无模型调用）"""
        terms = self._terms(query)
        if not terms:
            return []
        scored = []
        for doc in self._documents:
            overlap = terms & self._terms(doc["content"])
            if overlap:
                scored.append(dict(doc, score=len(overlap) / len(terms)))
        return sorted(scored, key=lambda d: d["score"], reverse=True)[:top_k]

    # ── 文档写入 ──

    def add_document(self, content: str, source: str = "", metadata: dict | None = None) -> str:
        """添加一篇文档；用「来源 + 内容」的哈希做 ID，重复添加是幂等的。"""
        doc_id = hashlib.sha256((source + content).encode()).hexdigest()[:16]
        if not any(d["id"] == doc_id for d in self._documents):
            self._documents.append(
                dict(id=doc_id, content=content, source=source, metadata=metadata or {})
            )
        return doc_id

    def add_documents_batch(self, documents: list[dict]) -> list[str]:
        return [
            self.add_document(d.get("content", ""), d.get("source", ""), d.get("metadata"))
            for d in documents
        ]

    # ── 索引落盘与加载 ──

    @property
    def metadata_path(self) -> Path:
        return self.index_path.with_suffix(".meta.json")

    def save(self) -> None:
        """把当前索引写到 `<index_path>.meta.json`"""
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(
            json.dumps(self._documents, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def chunk_text(text: str, chunk_size: int = 512, overlap: int = 128) -> list[str]:
        """
        按**段落边界**分块，而不是按固定字符数硬切。

        优先在空行处断开；单个段落超过 chunk_size 时保持完整，不做句中断裂。
        overlap 用于让相邻块共享一段尾部，避免关键信息正好落在边界上被切断。
        """
        if chunk_size <= overlap or overlap < 0:
            raise ValueError("chunk_size 必须大于 overlap 且 overlap 非负")

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            return []

        chunks: list[str] = []
        buffer = ""
        for paragraph in paragraphs:
            candidate = f"{buffer}\n{paragraph}" if buffer else paragraph
            if buffer and len(candidate) > chunk_size:
                chunks.append(buffer)
                tail = buffer[-overlap:] if overlap else ""
                buffer = f"{tail}\n{paragraph}" if tail else paragraph
            else:
                buffer = candidate
        if buffer:
            chunks.append(buffer)
        return chunks

    def load_knowledge_base(self, kb_dir: str | Path,
                            chunk_size: int = 512, overlap: int = 128) -> int:
        """把知识库目录下的 Markdown / 文本文件分块导入，返回导入的块数。"""
        count = 0
        for pattern in KNOWLEDGE_SUFFIXES:
            for path in sorted(Path(kb_dir).glob(f"**/{pattern}")):
                for chunk in self.chunk_text(
                    path.read_text(encoding="utf-8"), chunk_size, overlap
                ):
                    self.add_document(chunk, path.name)
                    count += 1
        return count

    def load_or_rebuild(self, kb_dir: str | Path,
                        chunk_size: int = 512, overlap: int = 128) -> dict:
        """
        按需从知识库目录重建索引。

        索引文件比所有源文件都新（且非空）时直接复用，否则重新分块并落盘。
        """
        kb_path = Path(kb_dir)
        sources = [p for pattern in KNOWLEDGE_SUFFIXES for p in kb_path.glob(f"**/{pattern}")]
        newest_source = max((p.stat().st_mtime for p in sources), default=0.0)

        reuse = (
            self.metadata_path.exists()
            and bool(self._documents)
            and self.metadata_path.stat().st_mtime >= newest_source
        )
        if reuse:
            return {"rebuilt": False, "documents": len(self._documents), "sources": len(sources)}

        self._documents = []
        for path in sources:
            for chunk in self.chunk_text(
                path.read_text(encoding="utf-8"), chunk_size, overlap
            ):
                self.add_document(chunk, path.name)
        self.save()
        return {"rebuilt": True, "documents": len(self._documents), "sources": len(sources)}
