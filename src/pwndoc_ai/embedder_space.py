"""The vector space of an embedding model: which vectors can be compared, and share an index."""

from __future__ import annotations

import hashlib
import re

MINILM_SPACE = "minilm-l6-v2"  # the vector space of all-MiniLM-L6-v2, ONNX and Ollama alike
SPACE_HASH_CHARS = 8
_MINILM_MODELS = frozenset({"all-minilm", "all-minilm:l6-v2"})
_PLAIN_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def vector_space(model: str) -> str:
    """Id of the vector space a model's vectors live in (vectors of one space are comparable).

    A name made only of letters, digits and single dashes is its own readable id. Any other
    name (``bge_m3``, ``bge.m3``, ``name:v1.5``) also carries a hash of the exact name, because
    slugging alone would give ``bge_m3`` and ``bge-m3`` one id and so one collection and one
    set of cached page vectors for two different models.
    """
    name = model.strip().lower()
    name = name.removesuffix(":latest") if name.count(":") == 1 else name
    if name in _MINILM_MODELS:
        return MINILM_SPACE
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    if _PLAIN_NAME.fullmatch(name):
        return f"ollama-{slug}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:SPACE_HASH_CHARS]
    return f"ollama-{slug}-{digest}" if slug else f"ollama-{digest}"
