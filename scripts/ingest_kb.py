"""Chunk and embed documents into the knowledge base vector store.

    python -m scripts.ingest_kb [docs_dir]   (default: KB_DOCS_PATH, i.e. data/kb; .md, .txt, .pdf)

Re-run after adding or editing documents; each file replaces its earlier version.
"""

import sys

from app.config import get_settings
from app.tools.knowledge import _collection, ingest_dir


def main() -> None:
    docs_dir = sys.argv[1] if len(sys.argv) > 1 else get_settings().kb_docs_path
    counts = ingest_dir(docs_dir)
    for name, n in counts.items():
        print(f"{name}: {n} chunks")
    print(f"Ingested {sum(counts.values())} chunks; collection now holds {_collection().count()}")


if __name__ == "__main__":
    main()
