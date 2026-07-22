"""Fit/check the two-parameter tag-relatedness calibration on the gold set."""

import csv
import math

import numpy as np

from eventindex import config, embeddings


def sigmoid(score: float, center: float, temperature: float) -> float:
    z = (score - center) / temperature
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1 + ez)


def main() -> None:
    path = config.ROOT / "db" / "gold" / "tag_relations.csv"
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    terms = sorted({row[side] for row in rows for side in ("left", "right")})
    vectors = embeddings.embed_tags(terms)
    by_term = dict(zip(terms, vectors))
    examples = [
        (
            float(by_term[row["left"]] @ by_term[row["right"]]),
            float(row["target"]), row["left"], row["right"],
        )
        for row in rows
    ]
    best = None
    for center in np.arange(0.10, 0.951, 0.002):
        for temperature in np.arange(0.010, 0.201, 0.002):
            mse = sum(
                (sigmoid(score, center, temperature) - target) ** 2
                for score, target, _, _ in examples
            ) / len(examples)
            if best is None or mse < best[0]:
                best = (mse, center, temperature)
    mse, center, temperature = best
    alone = embeddings.embed_tags(["Tanzen"])[0]
    mixed = embeddings.embed_tags(["unrelated longer tag", "Tanzen", "x"])[1]
    batch_delta = float(np.max(np.abs(alone - mixed)))
    strong = [score for score, target, _, _ in examples if target >= 0.7]
    unrelated = [score for score, target, _, _ in examples if target == 0]
    correctly_ordered = sum(a > b for a in strong for b in unrelated)
    auc = correctly_ordered / (len(strong) * len(unrelated))
    predicted = [
        (sigmoid(score, center, temperature), target, left, right, score)
        for score, target, left, right in examples
    ]
    false_high = [row for row in predicted if row[1] == 0 and row[0] >= 0.5]
    print(f"pairs={len(examples)} model={embeddings.MODEL_VERSION}")
    print(f"center={center:.3f} temperature={temperature:.3f} mse={mse:.4f}")
    print(f"batch-invariance max_delta={batch_delta:.8f}")
    print(f"strong-vs-unrelated AUC={auc:.3f} false_unrelated@0.5={len(false_high)}")
    for relatedness, target, left, right, cosine in sorted(
        predicted, key=lambda row: abs(row[0] - row[1]), reverse=True
    )[:12]:
        print(
            f"target={target:.1f} related={relatedness:.3f} cos={cosine:.3f} "
            f"{left!r} <-> {right!r}"
        )
    calibration_drift = (
        abs(center - embeddings.CALIBRATION_CENTER) > 0.003
        or abs(temperature - embeddings.CALIBRATION_TEMPERATURE) > 0.003
    )
    if batch_delta > 1e-6 or calibration_drift or auc < 0.90 or len(false_high) > 3:
        raise SystemExit("tag relation quality gate failed")


if __name__ == "__main__":
    main()
