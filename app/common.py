from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def sha(path: Path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def contained(root, relative, *, exists=True):
    root = Path(root).resolve()
    relative = Path(relative)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "\\" in str(relative)
        or ":" in str(relative)
    ):
        raise ValueError("Artifact path must be relative and contained")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or (exists and not path.is_file()):
        raise ValueError("Artifact is unavailable or outside its job")
    return path


def rows(root):
    path = Path(root) / "segmentation_manifest.jsonl"
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]
