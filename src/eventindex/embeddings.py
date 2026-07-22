"""Small local multilingual embeddings for normalized event tags only.

The model is deliberately lazy: ordinary chronological/category requests do
not load it. Stored vectors are a derived cache and can always be rebuilt from
event_tag. No user query text is persisted.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

MODEL_REPO = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
MODEL_REVISION = "4328cf26390c98c5e3c738b4460a05b95f4911f5"
MODEL_FILE = "onnx/model_qint8_avx512_vnni.onnx"
MODEL_VERSION = (
    f"{MODEL_REPO}@{MODEL_REVISION[:8]}:qint8-avx512-vnni:stable-tag-v1"
)
DIMENSIONS = 768
# A fixed shape matters for the quantized ONNX graph: with dynamic padding,
# the same short tag produced measurably different vectors depending on the
# longest sibling in its batch. Tags are capped at three words / 60 chars, so
# 32 tokens leaves ample multilingual subword headroom while keeping batches
# cheap and, crucially, deterministic.
TAG_SEQUENCE_LENGTH = 32

# Fitted by scripts/evaluate_tag_embeddings.py against
# db/gold/tag_relations.csv. Model cosines need not be probabilities; this
# monotonic mapping calibrates their useful range. It cannot fix wrong ordering, so the
# relation-set ranking/false-positive gate is separate.
CALIBRATION_CENTER = 0.366
CALIBRATION_TEMPERATURE = 0.098


def normalize_tag(value: str) -> str:
    """Mechanical normalization only; semantic rewriting belongs to the LLM."""
    return " ".join(value.strip().lower().split())


@lru_cache(maxsize=1)
def _runtime():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    path = hf_hub_download(MODEL_REPO, MODEL_FILE, revision=MODEL_REVISION)
    tokenizer_path = hf_hub_download(
        MODEL_REPO, "tokenizer.json", revision=MODEL_REVISION
    )
    tokenizer = Tokenizer.from_file(tokenizer_path)
    tokenizer.enable_truncation(max_length=TAG_SEQUENCE_LENGTH)
    tokenizer.enable_padding(length=TAG_SEQUENCE_LENGTH)
    session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return tokenizer, session


def embed_tags(tags: list[str]) -> np.ndarray:
    """Return normalized 768d vectors for short, symmetric tag comparisons."""
    if not tags:
        return np.empty((0, DIMENSIONS), dtype=np.float32)
    tokenizer, session = _runtime()
    input_names = {item.name for item in session.get_inputs()}
    vectors = []
    for tag in tags:
        # This qint8 graph dynamically quantizes activations across a batch;
        # mixing unrelated rows therefore perturbs a tag's vector. Tags are a
        # small deduplicated vocabulary, so one-row inference gives stable
        # stored/query vectors without meaningful throughput cost.
        encoding = tokenizer.encode(normalize_tag(tag))
        attention_mask = np.asarray([encoding.attention_mask], dtype=np.int64)
        available = {
            "input_ids": np.asarray([encoding.ids], dtype=np.int64),
            "attention_mask": attention_mask,
            "token_type_ids": np.asarray([encoding.type_ids], dtype=np.int64),
        }
        inputs = {name: available[name] for name in input_names}
        hidden = session.run(None, inputs)[0]
        mask = attention_mask[..., None]
        vector = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        vector /= np.maximum(np.linalg.norm(vector, axis=1, keepdims=True), 1e-12)
        vectors.append(vector[0])
    return np.asarray(vectors, dtype=np.float32)


def calibrated_relatedness(cosine: float) -> float:
    """Map compressed model cosine similarity to an operational 0..1 strength."""
    z = (float(cosine) - CALIBRATION_CENTER) / CALIBRATION_TEMPERATURE
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def vector_literal(vector: np.ndarray) -> str:
    """pgvector text input, avoiding another adapter dependency."""
    return "[" + ",".join(f"{float(value):.8g}" for value in vector) + "]"


def store_missing(tx, names: list[str]) -> int:
    """Embed missing/outdated tag names idempotently and return rows written."""
    names = sorted({normalize_tag(name) for name in names if normalize_tag(name)})
    if not names:
        return 0
    existing = {
        row["name"]
        for row in tx.execute(
            "SELECT name FROM tag_embedding WHERE name = ANY(%s) AND model = %s",
            (names, MODEL_VERSION),
        )
    }
    missing = [name for name in names if name not in existing]
    if not missing:
        return 0
    vectors = embed_tags(missing)
    with tx.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tag_embedding (name, embedding, model) "
            "VALUES (%s, %s::vector, %s) "
            "ON CONFLICT (name) DO UPDATE SET embedding = excluded.embedding, "
            "model = excluded.model, created_at = now()",
            [
                (name, vector_literal(vector), MODEL_VERSION)
                for name, vector in zip(missing, vectors)
            ],
        )
    return len(missing)


def warm() -> None:
    """Download/load the model and verify its output contract."""
    vector = embed_tags(["Tanzen"])
    if vector.shape != (1, DIMENSIONS) or not np.isfinite(vector).all():
        raise RuntimeError("tag embedding model returned an invalid vector")
