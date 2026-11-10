"""Knowledge base retrieval (RAG) over a local Chroma vector store.

Documents (.md, .txt, .pdf) in KB_DOCS_PATH are ingested automatically at startup when the
store is empty; re-ingest after changing them with `python -m scripts.ingest_kb`. Chroma's
default embedding model (all-MiniLM-L6-v2, ONNX) runs locally and is downloaded on first use.
"""

import asyncio
import logging
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


@lru_cache
def _collection():
    import chromadb

    from ..config import get_settings

    s = get_settings()
    client = chromadb.PersistentClient(path=s.kb_path)
    return client.get_or_create_collection(s.kb_collection, metadata={"hnsw:space": "cosine"})


def warm_up() -> None:
    """Load the embedding model before the first call so the first lookup isn't slow."""
    try:
        _collection().query(query_texts=["warm up"], n_results=1)
    except Exception:
        log.warning("Knowledge base warm-up failed; is it ingested?", exc_info=True)


# Hybrid retrieval: vector similarity (good at paraphrase) fused with keyword scoring (good at
# exact terms the small embedding model under-weights, like "screen repair"), via reciprocal
# rank fusion. The keyword index is built from the collection and rebuilt after ingestion.

_STOPWORDS = set(
    "a an and are as at be by can do does for from how i in is it my of on or our the this to "
    "usually what when where which who why will with you your about long much".split()
)
_keyword_index: list[tuple[str, str, Counter]] | None = None  # (text, source, term counts)


def _terms(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOPWORDS or len(w) < 2:
            continue
        for suffix in ("ing", "es", "s", "ed"):  # crude stemming: repairs/repairing -> repair
            if len(w) > len(suffix) + 3 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.append(w)
    return out


def _keywords() -> list[tuple[str, str, Counter]]:
    global _keyword_index
    if _keyword_index is None:
        data = _collection().get(include=["documents", "metadatas"])
        _keyword_index = [
            (doc, (meta or {}).get("source", "unknown"), Counter(_terms(doc)))
            for doc, meta in zip(data["documents"], data["metadatas"])
        ]
    return _keyword_index


def _keyword_rank(query: str, company_terms: set[str]) -> list[tuple[str, str, float]]:
    index = _keywords()
    q = [t for t in set(_terms(query)) if t not in company_terms]
    if not q or not index:
        return []
    n = len(index)
    df = {t: sum(1 for _, _, tf in index if t in tf) for t in q}
    scored = []
    for doc, source, tf in index:
        score = sum(math.log(1 + n / df[t]) * (tf[t] / (tf[t] + 1.2)) for t in q if tf.get(t))
        matched = sum(1 for t in q if tf.get(t))
        if matched >= min(2, len(q)):  # one stray common word isn't a match
            scored.append((doc, source, score))
    return sorted(scored, key=lambda x: -x[2])


def _search(query: str, k: int, max_distance: float) -> list[dict]:
    from ..config import get_settings

    col = _collection()
    if col.count() == 0:
        return []
    company_terms = set(_terms(get_settings().company_name))
    clean = " ".join(t for t in query.split() if not set(_terms(t)) & company_terms) or query

    res = col.query(query_texts=[clean], n_results=min(k * 2, col.count()))
    vector = [
        (doc, meta.get("source", "unknown"), 1 - dist)
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])
        if dist <= max_distance
    ]
    keyword = _keyword_rank(clean, company_terms)[: k * 2]

    fused: dict[str, dict] = {}
    for ranking in (vector, keyword):
        for rank, (doc, source, _) in enumerate(ranking):
            hit = fused.setdefault(doc, {"source": source, "text": doc, "score": 0.0})
            hit["score"] += 1 / (60 + rank)
    hits = sorted(fused.values(), key=lambda h: -h["score"])[:k]
    for h in hits:
        h["score"] = round(h["score"] * 60, 3)
    return hits


async def search_knowledge_base(query: str, k: int = 4, max_distance: float = 0.7) -> list[dict]:
    return await asyncio.to_thread(_search, query, k, max_distance)


# -- ingestion ---------------------------------------------------------------------------------

SUPPORTED = {".md", ".txt", ".pdf"}


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    """Split on markdown headings, then pack paragraphs up to max_chars, keeping the heading."""
    chunks = []
    for section in re.split(r"\n(?=#{1,3} )", text):
        lines = section.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else ""
        body = "\n".join(lines[1:] if heading else lines).strip()
        current = ""
        for para in re.split(r"\n\s*\n", body):
            while len(para) > max_chars:  # very long paragraphs (common in PDFs)
                cut = para.rfind(". ", 0, max_chars) + 1 or max_chars
                if current:
                    chunks.append(f"{heading}\n{current}".strip())
                    current = ""
                chunks.append(f"{heading}\n{para[:cut]}".strip())
                para = para[cut:].strip()
            if current and len(current) + len(para) > max_chars:
                chunks.append(f"{heading}\n{current}".strip())
                current = ""
            current = f"{current}\n\n{para}".strip()
        if current:
            chunks.append(f"{heading}\n{current}".strip())
    return [c for c in chunks if c.strip()]


def read_document(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8", errors="replace")


def ingest_dir(docs_dir: str | Path) -> dict[str, int]:
    """(Re)index every supported document under docs_dir. Returns chunks per file."""
    col = _collection()
    counts = {}
    for path in sorted(p for p in Path(docs_dir).glob("**/*") if p.suffix.lower() in SUPPORTED):
        chunks = chunk_text(read_document(path))
        col.delete(where={"source": path.name})  # replace any earlier version of this document
        if chunks:
            col.upsert(
                ids=[f"{path.stem}-{i}" for i in range(len(chunks))],
                documents=chunks,
                metadatas=[{"source": path.name}] * len(chunks),
            )
        counts[path.name] = len(chunks)
    global _keyword_index
    _keyword_index = None  # rebuild the keyword index from the updated collection
    return counts


def ensure_ingested(docs_dir: str | Path) -> None:
    if _collection().count() == 0 and Path(docs_dir).exists():
        counts = ingest_dir(docs_dir)
        log.info("Knowledge base built: %d chunks from %d documents", sum(counts.values()), len(counts))
