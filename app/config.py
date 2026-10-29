import os
from dataclasses import dataclass
from pathlib import Path

from .common import digest, read, sha

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    data: Path
    lock: Path
    components: Path
    runtimes: dict
    threads: int = 4
    max_upload_bytes: int = 250 * 1024 * 1024
    max_files: int = 60
    max_pixels: int = 60_000_000
    cancel_grace: int = 45

    @classmethod
    def load(cls):
        mapping = os.getenv("PAPER_RUNTIMES")
        runtimes = (
            read(mapping)
            if mapping
            else {
                "stage1": "/opt/venvs/stage12/bin/python",
                "stage2": "/opt/venvs/stage12/bin/python",
                "stage3": "/opt/venvs/stage3/bin/python",
                "stage4": "/opt/venvs/stage4/bin/python",
            }
        )
        return cls(
            Path(os.getenv("PAPER_DATA", str(ROOT / ".local/data"))).absolute(),
            Path(
                os.getenv("PAPER_LOCK", str(ROOT / "composition/pipeline.lock.json"))
            ).absolute(),
            Path(os.getenv("PAPER_COMPONENTS", "/opt/components")).absolute(),
            runtimes,
            threads=max(1, int(os.getenv("PAPER_CPU_THREADS", "4"))),
        )

    @property
    def lock_hash(self):
        return sha(self.lock)

    @property
    def application_hash(self):
        files = list((ROOT / "app").glob("*.py")) + list(
            (ROOT / "composition/adapter").glob("*.py")
        )
        files += [
            ROOT / "tools/stage_io.py",
            ROOT / "composition/profiles/capture.json",
        ]
        files += [ROOT / "requirements.txt"] + list(
            (ROOT / "composition/runtime").glob("*.txt")
        )
        hashes = {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}
        installed = Path("/opt/build/runtime.json")
        if installed.exists():
            hashes["installed_runtime"] = sha(installed)
        return digest(hashes)

    def python(self, stage):
        return str(self.runtimes[stage])  # Deliberately do not resolve venv symlinks.

    def job(self, job_id):
        return self.data / "jobs" / job_id

    def attempt(self, job):
        return self.job(job["id"]) / "attempts" / f"{job['attempt']:04d}"

    def environment(self, stage):
        env = os.environ.copy()
        env.update(
            PYTHONUTF8="1",
            PYTHONUNBUFFERED="1",
            PYTHONDONTWRITEBYTECODE="1",
            OMP_NUM_THREADS=str(self.threads),
            OPENBLAS_NUM_THREADS=str(self.threads),
            MKL_NUM_THREADS=str(self.threads),
        )
        env.pop("PYTHONPATH", None)
        return env
