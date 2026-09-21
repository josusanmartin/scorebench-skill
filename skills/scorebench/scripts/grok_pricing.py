"""Price native Grok inferences using the server's pinned public price manifest."""
from __future__ import annotations

import json
import math
from pathlib import Path


def load_pricing(path: str) -> dict:
    price = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(price, dict) or price.get("provider") != "xai":
        raise ValueError("Grok pricing must be an xAI model price manifest")
    for name in ("key", "requested_model", "source_url"):
        if not isinstance(price.get(name), str) or not price[name].strip():
            raise ValueError(f"Grok pricing is missing {name}")
    for name in ("input_per_million", "cached_input_per_million", "output_per_million"):
        value = price.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid Grok pricing {name}")
    threshold = price.get("long_context_threshold_tokens")
    multiplier = price.get("long_context_multiplier")
    if type(threshold) is not int or threshold <= 0:
        raise ValueError("Grok pricing requires a positive long-context threshold")
    if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier <= 1:
        raise ValueError("Grok pricing requires a long-context multiplier greater than one")
    return price


def inference_cost(price: dict, *, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    multiplier = (price["long_context_multiplier"]
                  if input_tokens + cached_tokens >= price["long_context_threshold_tokens"] else 1)
    return multiplier * (input_tokens * price["input_per_million"]
                         + cached_tokens * price["cached_input_per_million"]
                         + output_tokens * price["output_per_million"]) / 1_000_000
