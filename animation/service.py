"""
Animation Service — Vector DB for animation clip retrieval.

Stores animation clips with human-readable descriptions in ChromaDB.
The bridge queries this service to find the best clip for a given
natural-language action description from the LLM.

Exposes:
  POST /ingest      — bulk-load clips from unityactions.txt
  POST /query       — semantic search for best matching clip(s)
  POST /annotate    — add designer feedback to a clip
  GET  /clips       — list all stored clips
  GET  /health      — health check
  DELETE /clear     — wipe collection
"""

import logging
import time
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from animation.ingest import parse_actions_file

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("animation")

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())["animation"]

app = FastAPI(title="Parrot Animation")
collection = None
client = None


class QueryRequest(BaseModel):
    text: str
    n_results: int = 3
    conversational_only: bool = True


class AnnotateRequest(BaseModel):
    clip: str
    note: str


@app.on_event("startup")
async def init_db():
    global client, collection
    import chromadb

    db_path = str(ROOT / config["db_path"])
    log.info(f"Initializing ChromaDB at {db_path}")

    client = chromadb.PersistentClient(path=db_path)
    if config.get("reset_on_start"):
        # Wipe ONLY the animation collection; memory service uses a different db_path/collection.
        try:
            client.delete_collection(config["collection"])
            log.info("reset_on_start=true: deleted animation collection '%s'", config["collection"])
        except Exception:
            # Collection may not exist yet; safe to ignore.
            log.info("reset_on_start=true: collection '%s' did not exist (ok)", config["collection"])

    collection = client.get_or_create_collection(
        name=config["collection"],
        metadata={"hnsw:space": "cosine"},
    )
    count = collection.count()
    log.info(f"Animation collection '{config['collection']}' loaded with {count} clips.")

    if count == 0:
        log.info("Collection empty — ingesting from unityactions.txt...")
        _do_ingest()


def _do_ingest(path: str | None = None):
    clips = parse_actions_file(path)
    if not clips:
        log.warning("No clips found to ingest.")
        return 0

    ids, docs, metas = [], [], []
    for c in clips:
        ids.append(c["clip"])
        docs.append(c["description"])
        metas.append({
            "category": c["category"],
            "phase": c["phase"] or "",
            "conversational": c["conversational"],
            "user_notes": "",
            "ingested_at": time.time(),
        })

    collection.upsert(ids=ids, documents=docs, metadatas=metas)
    log.info(f"Ingested {len(clips)} clips.")
    return len(clips)


@app.get("/health")
async def health():
    count = collection.count() if collection else 0
    return {"status": "ok", "clips": count}


@app.post("/ingest")
async def ingest(path: Optional[str] = None):
    """Re-ingest clips from unityactions.txt (or a custom path)."""
    count = _do_ingest(path)
    return {"ingested": count, "total": collection.count()}


@app.post("/query")
async def query(req: QueryRequest):
    """Find the best animation clip(s) for a natural-language action."""
    if collection.count() == 0:
        return {"clips": [], "query": req.text}

    where = None
    if req.conversational_only:
        where = {"conversational": True}

    try:
        results = collection.query(
            query_texts=[req.text],
            n_results=min(req.n_results, 10),
            where=where,
        )
    except Exception:
        results = collection.query(
            query_texts=[req.text],
            n_results=min(req.n_results, 10),
        )

    clips = []
    if results["ids"] and results["ids"][0]:
        for clip_id, doc, meta, dist in zip(
            results["ids"][0],
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            clips.append({
                "clip": clip_id,
                "description": doc,
                "category": meta.get("category", ""),
                "score": round(1 - dist, 4),
                "user_notes": meta.get("user_notes", ""),
            })

    return {"clips": clips, "query": req.text}


@app.post("/annotate")
async def annotate(req: AnnotateRequest):
    """Add a designer note to a clip. Appends to existing notes and re-embeds."""
    existing = collection.get(ids=[req.clip], include=["documents", "metadatas"])
    if not existing["ids"]:
        return JSONResponse(
            status_code=404,
            content={"error": f"Clip '{req.clip}' not found"},
        )

    old_doc = existing["documents"][0]
    old_meta = existing["metadatas"][0]
    prev_notes = old_meta.get("user_notes", "")
    new_notes = f"{prev_notes}\n{req.note}".strip() if prev_notes else req.note
    new_doc = f"{old_doc}. Designer note: {new_notes}"

    old_meta["user_notes"] = new_notes
    collection.update(
        ids=[req.clip],
        documents=[new_doc],
        metadatas=[old_meta],
    )
    log.info(f"Annotated '{req.clip}': {req.note}")
    return {"clip": req.clip, "notes": new_notes, "description": new_doc}


@app.get("/clips")
async def list_clips(category: Optional[str] = None, conversational: Optional[bool] = None):
    """List all clips, optionally filtered."""
    count = collection.count()
    if count == 0:
        return {"clips": [], "total": 0}

    where = {}
    if category:
        where["category"] = category
    if conversational is not None:
        where["conversational"] = conversational

    result = collection.get(
        limit=count,
        include=["documents", "metadatas"],
        where=where if where else None,
    )

    clips = []
    for clip_id, doc, meta in zip(result["ids"], result["documents"], result["metadatas"]):
        clips.append({
            "clip": clip_id,
            "description": doc,
            "category": meta.get("category", ""),
            "conversational": meta.get("conversational", False),
            "user_notes": meta.get("user_notes", ""),
        })

    return {"clips": clips, "total": len(clips)}


@app.delete("/clear")
async def clear():
    global collection
    client.delete_collection(config["collection"])
    collection = client.get_or_create_collection(
        name=config["collection"],
        metadata={"hnsw:space": "cosine"},
    )
    log.info("Animation collection cleared.")
    return {"status": "cleared"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
