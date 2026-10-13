"""Every command the daemon or the job queue runs is dispatched by `python -m arkwatch`:
a renamed branch in __main__.py would ship green and fail only at its scheduled hour."""

import ast
import re
from pathlib import Path

from arkwatch import daemon
from arkwatch.qa.jobs_runner import JOB_KINDS

MAIN = Path(daemon.__file__).with_name("__main__.py")


def _dispatched() -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(MAIN.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Compare) and getattr(node.left, "id", None) == "cmd":
            for comp in node.comparators:
                for c in ast.walk(comp):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        names.add(c.value)
    return names


def test_every_scheduled_queued_and_lane_job_is_dispatched():
    dispatched = _dispatched()
    lanes = set(
        re.findall(r'_run_job\("([\w-]+)"', Path(daemon.__file__).read_text(encoding="utf-8"))
    )
    needed = (
        {cmd.split()[0] for *_hm, cmd, _desc in daemon.SCHEDULE}
        | {cmd.split()[0] for cmd in daemon.BOOTSTRAP_JOBS}
        | {argv[0] for argv in JOB_KINDS.values()}
        | lanes
    )
    assert needed - dispatched == set()
