#!/usr/bin/env python3
"""BFH embedding index (.mbed) — pure stdlib, no numpy required.

File layout (little-endian):
  magic:     4 bytes  b'BFMB'
  version:   u32      1
  n:         u32      number of vectors
  d:         u32      embedding dimension
  model_len: u16
  model:     utf-8
  created:   u64      unix seconds (index build/update time)
  ids:       n * u32
  vectors:   n * d * f32   (L2-normalised for cosine ≈ dot product)
"""
from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import time
from pathlib import Path

MAGIC = b"BFMB"
VERSION = 1
HEADER_STRUCT = struct.Struct("<4sIII")  # magic, version, n, d — model_len follows


def l2_normalize(vec: list[float]) -> list[float]:
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 0:
        return list(vec)
    inv = 1.0 / s
    return [x * inv for x in vec]


def write_mbed(
    path: Path | str,
    ids: list[int],
    vectors: list[list[float]],
    *,
    model: str = "mxbai-embed-large",
) -> Path:
    path = Path(path)
    if len(ids) != len(vectors):
        raise ValueError("ids/vectors length mismatch")
    n = len(ids)
    d = len(vectors[0]) if n else 0
    for v in vectors:
        if len(v) != d:
            raise ValueError("ragged vectors")
    model_b = model.encode("utf-8")
    if len(model_b) > 65535:
        raise ValueError("model name too long")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".jokes_mbed_", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(HEADER_STRUCT.pack(MAGIC, VERSION, n, d))
            f.write(struct.pack("<H", len(model_b)))
            f.write(model_b)
            f.write(struct.pack("<Q", int(time.time())))
            if n:
                f.write(struct.pack(f"<{n}I", *[int(i) for i in ids]))
                # pack all floats
                flat: list[float] = []
                for v in vectors:
                    flat.extend(l2_normalize(v))
                f.write(struct.pack(f"<{n * d}f", *flat))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # sidecar meta for humans / rsync triggers
    meta = {
        "path": str(path),
        "n": n,
        "d": d,
        "model": model,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bytes": path.stat().st_size,
    }
    meta_path = Path(str(path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return path


def read_mbed(path: Path | str) -> dict:
    path = Path(path)
    with path.open("rb") as f:
        head = f.read(HEADER_STRUCT.size)
        if len(head) < HEADER_STRUCT.size:
            raise ValueError("truncated mbed header")
        magic, version, n, d = HEADER_STRUCT.unpack(head)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic!r}")
        if version != VERSION:
            raise ValueError(f"unsupported mbed version {version}")
        (model_len,) = struct.unpack("<H", f.read(2))
        model = f.read(model_len).decode("utf-8")
        (created,) = struct.unpack("<Q", f.read(8))
        ids = list(struct.unpack(f"<{n}I", f.read(4 * n))) if n else []
        raw = f.read(4 * n * d) if n and d else b""
        if n and d and len(raw) != 4 * n * d:
            raise ValueError("truncated vector payload")
        flat = list(struct.unpack(f"<{n * d}f", raw)) if n and d else []
        vectors = [flat[i * d : (i + 1) * d] for i in range(n)] if n else []
    return {
        "ids": ids,
        "vectors": vectors,
        "n": n,
        "d": d,
        "model": model,
        "created": created,
        "path": str(path),
    }


def upsert_vectors(
    path: Path | str,
    updates: dict[int, list[float]],
    *,
    model: str | None = None,
) -> dict:
    """Insert or replace vectors by joke id; rewrite file atomically."""
    path = Path(path)
    if path.exists():
        data = read_mbed(path)
        id_to_idx = {jid: i for i, jid in enumerate(data["ids"])}
        ids = list(data["ids"])
        vectors = list(data["vectors"])
        use_model = model or data["model"]
        d = data["d"]
    else:
        id_to_idx = {}
        ids = []
        vectors = []
        use_model = model or "mxbai-embed-large"
        d = 0

    for jid, vec in updates.items():
        vec = l2_normalize(list(vec))
        if d and len(vec) != d:
            raise ValueError(f"dim mismatch for id {jid}: {len(vec)} vs {d}")
        if not d:
            d = len(vec)
        if jid in id_to_idx:
            vectors[id_to_idx[jid]] = vec
        else:
            id_to_idx[jid] = len(ids)
            ids.append(int(jid))
            vectors.append(vec)

    write_mbed(path, ids, vectors, model=use_model)
    return {"n": len(ids), "d": d, "model": use_model}


def remove_ids(path: Path | str, remove: set[int] | list[int]) -> dict:
    path = Path(path)
    remove_set = {int(x) for x in remove}
    if not path.exists() or not remove_set:
        return {"n": 0, "removed": 0}
    data = read_mbed(path)
    ids = []
    vectors = []
    for jid, vec in zip(data["ids"], data["vectors"]):
        if jid not in remove_set:
            ids.append(jid)
            vectors.append(vec)
    removed = data["n"] - len(ids)
    write_mbed(path, ids, vectors, model=data["model"])
    return {"n": len(ids), "removed": removed}


def top_k(
    data: dict,
    query: list[float],
    *,
    k: int = 8,
    min_score: float = 0.72,
    exclude_ids: set[int] | None = None,
) -> list[tuple[int, float]]:
    """Return [(joke_id, cosine), ...] best first. Vectors in file are L2-normed."""
    q = l2_normalize(query)
    if data["d"] and len(q) != data["d"]:
        raise ValueError(f"query dim {len(q)} != index dim {data['d']}")
    exclude = exclude_ids or set()
    scored: list[tuple[int, float]] = []
    for jid, vec in zip(data["ids"], data["vectors"]):
        if jid in exclude:
            continue
        # both unit → cosine = dot
        s = 0.0
        for a, b in zip(q, vec):
            s += a * b
        if s >= min_score:
            scored.append((int(jid), float(s)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]
