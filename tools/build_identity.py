"""Record installed interpreter/dependency identities in the final image."""

import json
import subprocess
from pathlib import Path

interpreters = {
    "app": "/opt/venvs/app/bin/python",
    "stage12": "/opt/venvs/stage12/bin/python",
    "stage3": "/opt/venvs/stage3/bin/python",
    "stage4": "/opt/venvs/stage4/bin/python",
}
code = "import sys,platform,json,importlib.metadata as m; print(json.dumps({'python':sys.version,'machine':platform.machine(),'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"
result = {
    name: json.loads(subprocess.check_output([python, "-c", code], text=True))
    for name, python in interpreters.items()
}
Path("/opt/build/runtime.json").write_text(
    json.dumps(result, sort_keys=True, indent=2) + "\n"
)
