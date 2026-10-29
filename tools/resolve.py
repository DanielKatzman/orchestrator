"""Resolve stable compatible versions; explicit candidate pins still need image tests."""

import argparse
import copy
import hashlib
import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml


def fetch(url):
    with urlopen(
        Request(
            url,
            headers={
                "User-Agent": "paper-orchestrator/0.1",
                "Accept": "application/vnd.github+json",
            },
        ),
        timeout=45,
    ) as response:
        return response.read()


def github(repository, endpoint):
    return json.loads(fetch(f"https://api.github.com/repos/{repository}/{endpoint}"))


def raw(repository, commit, path):
    return fetch(f"https://raw.githubusercontent.com/{repository}/{commit}/{path}")


def version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise ValueError("Use a stable three-part version")
    return tuple(map(int, match.groups()))


def compatibility(metadata, candidate=False):
    s1, s2, s3, s4 = [metadata[n] for n in ("stage1", "stage2", "stage3", "stage4")]
    if s1["interface"]["manifest_schema"] != "paper-segmentation-manifest-1.0":
        raise ValueError("Unsupported Stage 1 schema")
    if (
        s2["interface"]["manifest_schema"] != s3["upstream"]["manifest_schema"]
        or s2["interface"]["piece_schema"] != s3["upstream"]["piece_schema"]
    ):
        raise ValueError("Stage 2/3 wire schemas differ")
    if (
        s2["model"]["sha256"] != s3["upstream"]["model_sha256"]
        or s2["model"]["release_manifest_sha256"]
        != s3["upstream"]["release_manifest_sha256"]
    ):
        raise ValueError(
            "Stage 3 does not authenticate this Stage 2 model/configuration"
        )
    if (
        s3["downstream"]["evidence_schema"] != "stage3.assembly-evidence.v2"
        or s3["downstream"]["sdk_version"] != s4["contracts"]["sdk_software_version"]
    ):
        raise ValueError("Stage 3/4 evidence or SDK contract differs")
    if (
        s3["qualified_runtime"]["candidate_implementation_sha256"]
        != s4["stages"]["stage3"]["candidate_implementation_sha256"]
    ):
        raise ValueError("Stage 4 does not declare this Stage 3 implementation")
    if (
        not candidate
        and s3["downstream"]["source_sdk_fingerprint"]
        != s4["stages"]["stage4"]["source_sdk_fingerprint"]
    ):
        raise ValueError(
            "Stage 3's qualified Stage 4 implementation differs; select an explicit candidate to qualify a new solver"
        )
    if (
        s1["runtime"]["python_major_minor"] != "3.14"
        or s2["runtime"]["python_major_minor"] != "3.14"
        or s3["runtime"]["python_major_minor"] != "3.12"
    ):
        raise ValueError(
            "This build does not support a new Python environment grouping"
        )


def resolve(base, selectors, overrides=None, candidate=False):
    overrides = overrides or {}
    candidates, skipped = {}, []
    for name, current in base["components"].items():
        repo = current["repository"].removeprefix("https://github.com/")
        selector = selectors["stages"][name]
        refs = []
        if name in overrides:
            refs = [overrides[name]]
        elif selector.get("channel") == "pinned":
            refs = [selector["ref"]]
        else:
            releases = github(repo, "releases?per_page=30")
            refs = [
                r["tag_name"]
                for r in releases
                if not r["draft"]
                and not r["prerelease"]
                and re.fullmatch(r"v?\d+\.\d+\.\d+", r["tag_name"])
            ]
            # Tags are also supported when maintainers do not create a Release.
            refs += [
                r["name"]
                for r in github(repo, "tags?per_page=30")
                if re.fullmatch(r"v?\d+\.\d+\.\d+", r["name"])
            ]
            refs = sorted(set(refs), key=version, reverse=True)[:8]
            refs.append(current["commit"])
        choices = []
        seen = set()
        for ref in refs:
            try:
                commit = github(repo, "commits/" + quote(ref, safe=""))["sha"]
                if commit in seen:
                    continue
                seen.add(commit)
                meta = json.loads(raw(repo, commit, "compatibility.json"))
                updated = copy.deepcopy(current)
                updated.update(
                    commit=commit,
                    reference=ref,
                    distribution_version=meta["release_version"],
                    source_readme=f"https://github.com/{repo}/blob/{commit}/README.md",
                )
                if commit != current["commit"]:
                    updated["publication"] = {
                        "kind": "candidate_tag",
                        "url": f"https://github.com/{repo}/tree/{ref}",
                    }
                choices.append((updated, meta, repo))
            except (HTTPError, KeyError, ValueError) as error:
                skipped.append({"stage": name, "ref": ref, "reason": str(error)})
                if name in overrides or selector.get("channel") == "pinned":
                    raise ValueError(
                        f"Cannot resolve explicit {name}={ref}: {error}"
                    ) from error
        if not choices:
            raise ValueError("No supported source candidates for " + name)
        choices.sort(key=lambda item: version(item[1]["release_version"]), reverse=True)
        candidates[name] = choices
    selected = None
    names = list(base["components"])
    for combination in itertools.product(*(candidates[n] for n in names)):
        try:
            compatibility(
                {n: item[1] for n, item in zip(names, combination)}, candidate
            )
            selected = dict(zip(names, combination))
            break
        except ValueError as error:
            skipped.append(
                {
                    "tuple": {
                        n: item[0]["reference"] for n, item in zip(names, combination)
                    },
                    "reason": str(error),
                }
            )
    if selected is None:
        raise ValueError("No compatible tuple found: " + json.dumps(skipped[-4:]))
    result = copy.deepcopy(base)
    for name, (component, meta, repo) in selected.items():
        if component["commit"] == base["components"][name]["commit"]:
            result["components"][name] = copy.deepcopy(base["components"][name])
            continue
        if name in {"stage1", "stage2"}:
            model = meta["model"]
            manifest_payload = raw(repo, component["commit"], "models/release.json")
            if (
                hashlib.sha256(manifest_payload).hexdigest()
                != model["release_manifest_sha256"]
            ):
                raise ValueError("Selected model release manifest has different bytes")
            payload = raw(repo, component["commit"], model["path"])
            size = re.search(rb"\nsize (\d+)", payload)
            component["models"] = [
                {
                    "path": model["path"],
                    "sha256": model["sha256"],
                    "bytes": int(size[1]) if size else len(payload),
                }
            ]
            component["component_software_version"] = meta["component_software_version"]
            component["package"]["version"] = meta["release_version"]
        elif name == "stage3":
            component["models"] = [
                {"path": m["path"], "sha256": m["sha256"], "bytes": m["size"]}
                for m in meta["models"]
            ]
            component["component_software_version"] = meta["qualified_runtime"][
                "component_software_version"
            ]
            component["package"]["version"] = meta["release_version"]
            component["model_manifest"]["sha256"] = hashlib.sha256(
                raw(repo, component["commit"], "models/alpha.json")
            ).hexdigest()
        else:
            component["package"]["version"] = meta["stages"]["stage4"][
                "software_version"
            ]
            component["implementation_sdk_fingerprint"] = meta["stages"]["stage4"][
                "source_sdk_fingerprint"
            ]
        result["components"][name] = component
    # A new SDK needs explicit application support; keep the audited v0.1.0
    # reader/schema byte authority until a deliberate compatibility migration.
    return result, {
        "at": datetime.now(UTC).isoformat(),
        "candidate": candidate,
        "skipped": skipped,
        "selected": {n: c["commit"] for n, c in result["components"].items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type=Path, default=Path("composition/pipeline.yaml")
    )
    parser.add_argument(
        "--lock", type=Path, default=Path("composition/pipeline.lock.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".build/candidate.lock.json")
    )
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--latest-compatible", action="store_true")
    args = parser.parse_args()
    base = json.loads(args.lock.read_text("utf-8-sig"))
    overrides = dict(item.split("=", 1) for item in args.set)
    if set(overrides) - set(base["components"]):
        raise SystemExit("Unknown stage override")
    resolved, report = resolve(
        base,
        yaml.safe_load(args.config.read_text("utf-8-sig")),
        overrides,
        args.candidate,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        args.lock.read_bytes()
        if resolved == base
        else (json.dumps(resolved, sort_keys=True, indent=2) + "\n").encode()
    )
    args.output.with_name("resolution.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
