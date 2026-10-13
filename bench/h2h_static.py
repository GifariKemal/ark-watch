"""Static head-to-head metrics for one source tree (run from its root, inside its own venv).

Usage: python h2h_static.py <label>. Same tool versions and the SAME rule sets for both trees,
so the counts compare code, not configuration.
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

label = sys.argv[1]
root = Path(".")
py = sys.executable
bin_ = Path(py).parent
out: dict = {"label": label}


def sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout


src = [p for p in (root / "arkwatch").rglob("*.py") if "__pycache__" not in p.parts]
out["py_files"] = len(src)
out["py_lines"] = sum(
    len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in src
)
tests = [p for p in (root / "tests").rglob("test_*.py")]
out["test_files"] = len(tests)
out["test_functions"] = sum(
    len(re.findall(r"^\s*def test_", p.read_text(encoding="utf-8", errors="replace"), re.M))
    for p in tests
)

pp = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
out["runtime_deps"] = len(pp["project"].get("dependencies", []))
out["lockfile"] = (root / "uv.lock").exists()

# one common ruff rule set (ours) with --isolated so neither tree's config changes the count
rules = "E,W,F,I,B,UP,SIM,RET"
r = sh([str(bin_ / "ruff"), "check", "arkwatch", "--isolated", "--select", rules,
        "--ignore", "E501,SIM108,SIM102,B008", "--target-version", "py312", "--output-format", "json"])  # fmt: skip
issues = json.loads(r or "[]")
out["ruff_issues"] = len(issues)
by_code: dict[str, int] = {}
for i in issues:
    by_code[i["code"]] = by_code.get(i["code"], 0) + 1
out["ruff_top"] = sorted(by_code.items(), key=lambda kv: -kv[1])[:5]
out["ruff_F_real_bugs"] = sum(v for k, v in by_code.items() if k.startswith("F"))
out["ruff_B_bugbear"] = sum(v for k, v in by_code.items() if k.startswith("B"))

# one common pyright config (basic) for both
cfg = Path("/tmp/prcfg/pyrightconfig.json")
cfg.parent.mkdir(exist_ok=True)
cfg.write_text(json.dumps({"include": [str(Path("arkwatch").resolve())], "typeCheckingMode": "basic",
                           "pythonVersion": "3.12", "venvPath": str(Path(".").resolve()), "venv": ".venv"}))  # fmt: skip
pr = sh([str(bin_ / "pyright"), "-p", str(cfg), "--outputjson"])
try:
    summ = json.loads(pr)["summary"]
    out["pyright_errors"] = summ["errorCount"]
    out["pyright_warnings"] = summ["warningCount"]
except (ValueError, KeyError):
    out["pyright_errors"] = "n/a"

b = sh([str(bin_ / "bandit"), "-r", "arkwatch", "-f", "json", "-q"])
try:
    res = json.loads(b)["results"]
    out["bandit_high"] = sum(x["issue_severity"] == "HIGH" for x in res)
    out["bandit_medium"] = sum(x["issue_severity"] == "MEDIUM" for x in res)
    out["bandit_high_ids"] = sorted({x["test_id"] for x in res if x["issue_severity"] == "HIGH"})
except (ValueError, KeyError):
    out["bandit_high"] = "n/a"

a = sh([py, "-m", "pip_audit", "--progress-spinner", "off", "-f", "json"])
try:
    deps = json.loads(a)["dependencies"]
    vulns = [(d["name"], v["id"]) for d in deps for v in d.get("vulns", [])]
    out["known_vulns"] = len(vulns)
    out["known_vulns_list"] = vulns[:8]
except (ValueError, KeyError):
    out["known_vulns"] = "n/a"

app = root / "arkwatch" / "server" / "app.py"
out["http_routes"] = (
    len(re.findall(r"@(?:v1|app)\.(?:get|post|patch|put|delete)\(", app.read_text()))
    if app.exists()
    else 0
)


def txt(p: str) -> str:
    return (root / p).read_text(encoding="utf-8", errors="replace") if (root / p).exists() else ""


out["table_retention_rules"] = len(
    re.findall(r'^\s+\("\w+", "\w+", \d+,', txt("arkwatch/qa/backup.py"), re.M)
)
out["healthcheck_checks"] = sum(
    k in txt("arkwatch/daemon.py")
    for k in ("HEARTBEAT", "LANE_MAX_AGE_S", "MIN_FREE_BYTES", "BACKUP_MAX_AGE_S", "_heal(")
)
out["redaction_patterns"] = txt("arkwatch/qa/redact.py").count('r"')
out["test_network_guard"] = "unit test tried to reach the network" in txt("tests/conftest.py")
out["ci_workflow"] = (root / ".github" / "workflows").exists()
out["dependabot"] = (root / ".github" / "dependabot.yml").exists()
out["dockerfile"] = (root / "Dockerfile").exists()
out["config_files"] = len(list((root / "config").glob("*.yaml")))
out["fill_rules"] = "FILL_RULES" in txt("arkwatch/signals/playbook_tracker.py")
out["scorecard_net_costs"] = "costs_bps" in txt("arkwatch/signals/scorecard.py")
print(json.dumps(out))
