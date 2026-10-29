import json
import os
import signal
from pathlib import Path

from .common import read


def identity(pid):
    """Linux boot/start identity prevents mistaking a recycled PID for its owner."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start": stat[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except PermissionError:
        raise
    except OSError:
        return {"pid": pid, "start": None, "boot": None}


def alive(record):
    pid = record.get("pid")
    if not isinstance(pid, int) or pid < 1 or record.get("finished"):
        return False
    if os.name == "posix":
        current = identity(pid)
        return current["start"] is not None and current == {
            k: record[k] for k in ("pid", "start", "boot")
        }
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def signal_group(process, force=False):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        elif force:
            process.kill()
        else:
            process.send_signal(signal.CTRL_BREAK_EVENT)
    except ProcessLookupError:
        pass


def recover_locks(attempt):
    records = [read(p) for p in (attempt / "logs").glob("*.process.json")]
    if any(alive(r) for r in records):
        raise ValueError(
            "A recorded stage process is still alive; recovery cannot take over"
        )
    owners = {r["pid"] for r in records}
    for path in attempt.rglob("*"):
        # Stage 2's inference.lock is an OS advisory lock, not a PID marker.
        # Its file remains after normal exit; the OS releases ownership itself.
        if path.is_file() and path.name in {
            ".paper-stage1.lock",
            "run.lock",
            "audit.lock",
        }:
            try:
                value = json.loads(path.read_text())
                pid = int(value["pid"] if isinstance(value, dict) else value)
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(
                    f"Unrecognized lock needs operator inspection: {path.name}"
                ) from error
            if pid not in owners:
                raise ValueError("Lock owner is not an authenticated recorded process")
            path.unlink()  # Only reached by explicit Resume after proving every owner stopped.
