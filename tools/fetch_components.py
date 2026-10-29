"""Fetch exact GitHub source/model objects. Network is used only by build tooling."""

import argparse
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


def download(url):
    for attempt in range(3):
        try:
            with urlopen(
                Request(url, headers={"User-Agent": "paper-orchestrator/0.1"}),
                timeout=120,
            ) as response:
                return response.read()
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(lock, target):
    target.mkdir(parents=True, exist_ok=True)
    for name, component in lock["components"].items():
        root = target / name
        commit = component["commit"]
        repo = component["repository"].removeprefix("https://github.com/")
        if not (root / ".source-commit").is_file():
            if root.exists():
                raise ValueError(
                    f"Refusing nonempty unverified source directory {root}"
                )
            payload = download(f"https://codeload.github.com/{repo}/tar.gz/{commit}")
            with tempfile.TemporaryDirectory(dir=target) as folder:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                    archive.extractall(folder, filter="data")
                children = list(Path(folder).iterdir())
                if len(children) != 1 or not children[0].is_dir():
                    raise ValueError("Unexpected source archive")
                shutil.move(str(children[0]), root)
            (root / ".source-commit").write_text(commit)
        elif (root / ".source-commit").read_text() != commit:
            raise ValueError(
                "Source cache has a different commit; use a fresh build directory"
            )
        for model in component["models"]:
            path = root / model["path"]
            if not path.exists() or file_sha(path) != model["sha256"]:
                payload = download(
                    f"https://media.githubusercontent.com/media/{repo}/{commit}/{model['path']}"
                )
                if (
                    hashlib.sha256(payload).hexdigest() != model["sha256"]
                    or len(payload) != model["bytes"]
                ):
                    raise ValueError("Model download does not match lock: " + str(path))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            if file_sha(path) != model["sha256"]:
                raise ValueError("Model failed verification")
        metadata = json.loads((root / "compatibility.json").read_text())
        if metadata["release_version"] != component["distribution_version"]:
            raise ValueError("Distribution version differs from source lock")
        print(f"Verified {name}: {commit}", flush=True)
    sdk3 = target / "stage3/contracts/stage3_to_stage4/v2/sdk/src"
    sdk4 = target / "stage4/contracts/sdk/src"
    for relative, expected in lock["sdk"]["files"].items():
        if (
            file_sha(sdk3 / relative) != expected
            or file_sha(sdk4 / relative) != expected
        ):
            raise ValueError("Contract SDK bytes differ")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lock", type=Path, default=Path("composition/pipeline.lock.json")
    )
    parser.add_argument("--output", type=Path, default=Path(".build/components"))
    args = parser.parse_args()
    fetch(json.loads(args.lock.read_text("utf-8-sig")), args.output)
