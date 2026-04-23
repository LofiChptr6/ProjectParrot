"""
Memory Service — Long-term memory via ChromaDB + sentence-transformers.

Exposes:
  POST /store              — store a text with metadata
  POST /query              — semantic search across stored memories
  GET  /recent             — get N most recent entries
  DELETE /clear            — wipe collection
  GET  /health             — health check
"""

import logging
import time
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("memory")

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())["memory"]

app = FastAPI(title="Parrot Memory")
collection = None
client = None


class StoreRequest(BaseModel):
    text: str
    role: str = "user"  # "user" or "assistant"
    metadata: Optional[dict] = None


class QueryRequest(BaseModel):
    text: str
    n_results: int = 5


@app.on_event("startup")
async def init_db():
    global client, collection
    import chromadb
    from chromadb.config import Settings

    db_path = str(ROOT / config["db_path"])
    log.info(f"Initializing ChromaDB at {db_path}")

    import torch
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log.info(f"Memory embedding device: {device}")
    ef = SentenceTransformerEmbeddingFunction(
        model_name=config["embedding_model"],
        device=device,
    )

    client = chromadb.PersistentClient(path=db_path)
    try:
        collection = client.get_or_create_collection(
            name=config["collection"],
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
    except ValueError as e:
        if "embedding function" in str(e).lower():
            log.warning(
                "Embedding function mismatch on existing collection — recreating. "
                "Existing entries will be lost."
            )
            client.delete_collection(config["collection"])
            collection = client.create_collection(
                name=config["collection"],
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            raise
    count = collection.count()
    log.info(f"Memory collection '{config['collection']}' loaded with {count} entries.")


@app.get("/health")
async def health():
    count = collection.count() if collection else 0
    return {"status": "ok", "entries": count}


@app.post("/store")
async def store(req: StoreRequest):
    doc_id = f"{req.role}_{int(time.time() * 1000)}"
    meta = {"role": req.role, "timestamp": time.time()}
    if req.metadata:
        meta.update(req.metadata)

    collection.add(
        ids=[doc_id],
        documents=[req.text],
        metadatas=[meta],
    )
    log.info(f"Stored memory [{req.role}]: {req.text[:60]}...")
    return {"id": doc_id, "total": collection.count()}


@app.post("/query")
async def query(req: QueryRequest):
    results = collection.query(
        query_texts=[req.text],
        n_results=min(req.n_results, config["max_results"]),
    )

    memories = []
    if results["documents"]:
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            memories.append({
                "text": doc,
                "role": meta.get("role", "unknown"),
                "timestamp": meta.get("timestamp", 0),
                "relevance": round(1 - dist, 4),
            })

    return {"memories": memories, "query": req.text}


@app.get("/recent")
async def recent(n: int = 10):
    count = collection.count()
    if count == 0:
        return {"memories": []}

    result = collection.get(
        limit=min(n, count),
        include=["documents", "metadatas"],
    )

    memories = []
    for doc, meta in zip(result["documents"], result["metadatas"]):
        memories.append({
            "text": doc,
            "role": meta.get("role", "unknown"),
            "timestamp": meta.get("timestamp", 0),
        })

    memories.sort(key=lambda m: m["timestamp"], reverse=True)
    return {"memories": memories[:n]}


@app.delete("/clear")
async def clear():
    global collection
    client.delete_collection(config["collection"])
    collection = client.get_or_create_collection(
        name=config["collection"],
        metadata={"hnsw:space": "cosine"},
    )
    log.info("Memory cleared.")
    return {"status": "cleared"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
