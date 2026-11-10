"""Hybrid retrieval over the shipped KB documents, in an isolated in-memory Chroma collection."""

import uuid

import chromadb
import pytest

from app.tools import knowledge


@pytest.fixture(scope="module")
def kb():
    col = chromadb.EphemeralClient().get_or_create_collection(f"t{uuid.uuid4().hex}", metadata={"hnsw:space": "cosine"})
    mp = pytest.MonkeyPatch()
    mp.setattr(knowledge, "_collection", lambda: col)
    mp.setattr(knowledge, "_keyword_index", None)
    counts = knowledge.ingest_dir("data/kb")
    yield counts
    mp.undo()


@pytest.mark.parametrize(
    "query, source",
    [
        ("Nimbus Mobile screen repair duration", "stores_and_appointments.md"),  # missed by vector-only search
        ("how much is the unlimited plan", "plans_and_pricing.md"),
        ("can I return an opened phone", "returns_and_shipping.md"),
        ("is the downtown store open on sunday", "stores_and_appointments.md"),
        ("I forgot my PIN", "account_security.md"),
        ("late fee", "billing_and_payments.md"),
    ],
)
async def test_relevant_document_ranks_first(kb, query, source):
    hits = await knowledge.search_knowledge_base(query)
    assert hits and hits[0]["source"] == source


@pytest.mark.parametrize("query", ["recipe for pasta", "Nimbus Mobile weather forecast"])
async def test_off_topic_returns_nothing(kb, query):
    assert await knowledge.search_knowledge_base(query) == []


def test_chunking_keeps_headings_and_splits_long_paragraphs():
    text = "# Title\n\n" + ("A sentence about refunds. " * 80)
    chunks = knowledge.chunk_text(text, max_chars=300)
    assert len(chunks) > 3 and all(c.startswith("Title") and len(c) <= 320 for c in chunks)
