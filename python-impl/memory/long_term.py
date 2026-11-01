"""本地词法知识检索；未配置真实Embedding，因此不使用随机向量。"""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

class LongTermMemory:
    def __init__(self, index_path="./.data/knowledge/index", **kwargs):
        self.index_path = Path(index_path)
        self._documents = []
        metadata = self.index_path.with_suffix(".meta.json")
        if metadata.exists():
            self._documents = json.loads(metadata.read_text(encoding="utf-8"))

    @staticmethod
    def _terms(text):
        terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        for fragment in re.findall(r"[\u4e00-\u9fff]+", text):
            terms.update(fragment[i:i+2] for i in range(len(fragment)-1))
        return terms

    def add_document(self, content, source="", metadata=None):
        doc_id = hashlib.sha256((source + content).encode()).hexdigest()[:16]
        if not any(d["id"] == doc_id for d in self._documents):
            self._documents.append(dict(id=doc_id, content=content, source=source, metadata=metadata or {}))
        return doc_id

    def add_documents_batch(self, documents):
        return [self.add_document(d.get("content", ""), d.get("source", ""), d.get("metadata")) for d in documents]

    def search(self, query, top_k=5):
        terms = self._terms(query)
        scored = []
        for doc in self._documents:
            overlap = terms & self._terms(doc["content"])
            if overlap:
                scored.append(dict(doc, score=len(overlap)/max(1, len(terms))))
        return sorted(scored, key=lambda d: d["score"], reverse=True)[:top_k]

    def save(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.with_suffix(".meta.json").write_text(json.dumps(self._documents, ensure_ascii=False), encoding="utf-8")

    def load_knowledge_base(self, kb_dir):
        count = 0
        for path in Path(kb_dir).glob("**/*.txt"):
            for chunk in self._chunk_text(path.read_text(encoding="utf-8")):
                self.add_document(chunk, path.name)
                count += 1
        return count

    @staticmethod
    def _chunk_text(text, chunk_size=512, overlap=128):
        if chunk_size <= overlap or overlap < 0:
            raise ValueError("chunk_size 必须大于 overlap 且 overlap 非负")
        return [text[i:i+chunk_size] for i in range(0, len(text), chunk_size-overlap)]
