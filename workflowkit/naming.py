from __future__ import annotations

import hashlib
import re

from .model import SpecError


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise SpecError(f"cannot derive snapshot name from {value!r}")
    return slug


def snapshot_filename(value: str, *, disambiguate: bool) -> str:
    slug = slugify(value)
    if not disambiguate:
        return slug + ".json"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.json"
