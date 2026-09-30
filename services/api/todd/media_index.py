"""Nearest-neighbour search over MediaAsset embeddings: pgvector on Postgres when the extension is available,
cosine in Python otherwise (SQLite, tests, the compose file's postgres:16-alpine). Every query filters by embedding
model, so vectors from different models never mix.

The compose Postgres stays on the Alpine image for now: moving an existing data volume to a glibc image (such as
pgvector/pgvector) changes text collation, so its indexes would need a REINDEX. Step 1 searches at most a run's own
library in Python; pgvector matters once search spans the whole library."""

from __future__ import annotations

import logging
import math
from typing import Iterable, Sequence

from sqlalchemy import text

from .db import MediaAsset, engine, select, session

log = logging.getLogger("todd.media_index")
DIM = 512
FALLBACK_ROWS = 5000  # newest rows scored in Python without pgvector
_pgvector = False


def ensure() -> None:
    """Called at startup after init_db(): add the pgvector column and index when the database supports it."""
    global _pgvector
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(text(f"ALTER TABLE mediaasset ADD COLUMN IF NOT EXISTS vec vector({DIM})"))
            c.execute(text("CREATE INDEX IF NOT EXISTS mediaasset_vec_hnsw ON mediaasset "
                           "USING hnsw (vec vector_cosine_ops)"))
        _pgvector = True
    except Exception as e:  # noqa: BLE001  (e.g. the plain postgres image: no extension)
        log.info("pgvector not available, media search runs in Python: %s", str(e).splitlines()[0][:200])


def active() -> bool:
    return _pgvector


def _lit(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vector) + "]"


def store_vec(asset_id: str, vector: Sequence[float]) -> None:
    if not _pgvector or len(vector) != DIM:
        return
    try:
        with engine.begin() as c:
            c.execute(text("UPDATE mediaasset SET vec = CAST(:v AS vector) WHERE id = :id"),
                      {"v": _lit(vector), "id": asset_id})
    except Exception as e:  # noqa: BLE001  (the JSON copy still works)
        log.warning("couldn't store vector for %s: %s", asset_id, e)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest(vector: Sequence[float], k: int, source: str | None = None, exclude_ids: Iterable[str] = (),
            model: str | None = None, run_id: str | None = None) -> list[tuple[MediaAsset, float]]:
    """The k assets closest to `vector` as (asset, cosine similarity), best first."""
    exclude = set(exclude_ids)
    if _pgvector and len(vector) == DIM:
        where, params = ["vec IS NOT NULL"], {"q": _lit(vector), "k": k + len(exclude)}
        for col, val in (("source", source), ("model", model), ("run_id", run_id)):
            if val is not None:
                where.append(f"{col} = :{col}")
                params[col] = val
        with engine.connect() as c:
            rows = c.execute(text(f"SELECT id, 1 - (vec <=> CAST(:q AS vector)) AS sim FROM mediaasset "
                                  f"WHERE {' AND '.join(where)} ORDER BY vec <=> CAST(:q AS vector) LIMIT :k"),
                             params).all()
        sims = {r[0]: float(r[1]) for r in rows if r[0] not in exclude}
        q = select(MediaAsset).where(MediaAsset.id.in_(list(sims)))  # type: ignore[attr-defined]
        with session() as s:
            assets = {a.id: a for a in s.exec(q)}
        return [(assets[i], sim) for i, sim in sims.items() if i in assets][:k]
    q = select(MediaAsset)
    if source is not None:
        q = q.where(MediaAsset.source == source)
    if model is not None:
        q = q.where(MediaAsset.model == model)
    if run_id is not None:
        q = q.where(MediaAsset.run_id == run_id)
    with session() as s:
        rows = s.exec(q.order_by(MediaAsset.created_at.desc()).limit(FALLBACK_ROWS)).all()  # type: ignore[attr-defined]
    scored = [(a, cosine(vector, a.embedding)) for a in rows if a.id not in exclude and a.embedding]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]
