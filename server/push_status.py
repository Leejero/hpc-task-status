#!/usr/bin/env python3
"""Validate the compact status summary and publish it with GitHub Contents API."""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOP_KEYS = {"schema_version", "updated_at", "timezone", "collector_status", "job_active", "counts", "total", "scope", "running_tasks", "issues", "recent_success"}
RECORD_KEYS = {"name", "group", "status", "phase", "detail", "time"}
RUNNING_KEYS = RECORD_KEYS - {"time"}
STATES = {"success", "warning", "failed", "running", "pending", "unknown"}
DETAILS = {"计算成功", "计算异常终止", "计算证据不完整", "能量记录缺失或无效", "静态计算未收敛", "非零退出码", "正常退出但未收敛", "存在未完成输出", "静态计算进行中", "弛豫计算进行中"}
NAME_RE = re.compile(r"^[^/\s]+/[0-9]+(?:\.[0-9]+)?$")
SCOPE_RE = re.compile(r"^slide-[0-9]{3,}$")


def in_scope(record, scope):
    return isinstance(record, dict) and isinstance(record.get("name"), str) and record["name"].split("/", 1)[0] in scope


def validate(data):
    if set(data) != TOP_KEYS or data.get("schema_version") != 2:
        raise ValueError("unexpected compact status schema")
    if data.get("timezone") != "Asia/Shanghai" or data.get("collector_status") != "ok" or not isinstance(data.get("job_active"), bool):
        raise ValueError("invalid collection metadata")
    if not isinstance(data.get("total"), int) or data["total"] < 1:
        raise ValueError("invalid total")
    if set(data.get("counts", {})) != STATES or sum(data["counts"].values()) != data["total"]:
        raise ValueError("counts do not match total")
    if any(not isinstance(value, int) or value < 0 for value in data["counts"].values()):
        raise ValueError("invalid counts")
    if any(not isinstance(data.get(key), list) for key in ("scope", "issues", "recent_success", "running_tasks")):
        raise ValueError("invalid compact record lists")
    if not 1 <= len(data["scope"]) <= 10 or any(not isinstance(slide, str) or not SCOPE_RE.fullmatch(slide) for slide in data["scope"]):
        raise ValueError("invalid current slide scope")
    if len(data["issues"]) > 30 or len(data["recent_success"]) > 5:
        raise ValueError("compact record limit exceeded")
    if len(data["running_tasks"]) != data["counts"]["running"]:
        raise ValueError("running records do not match count")
    for record in data["running_tasks"]:
        if not in_scope(record, data["scope"]):
            raise ValueError("running task is outside current slide scope")
        if set(record) != RUNNING_KEYS or not NAME_RE.fullmatch(record["name"]):
            raise ValueError("invalid running task fields")
        if record["status"] != "running" or record["phase"] not in {"relax", "scf"} or record["detail"] not in DETAILS:
            raise ValueError("invalid running task state")
        if not isinstance(record["group"], str) or len(record["group"]) > 20:
            raise ValueError("invalid running task group")
    for record in data["issues"] + data["recent_success"]:
        if not in_scope(record, data["scope"]):
            raise ValueError("completed record is outside current slide scope")
        if set(record) != RECORD_KEYS or not NAME_RE.fullmatch(record["name"]):
            raise ValueError("invalid compact task name or fields")
        if record["status"] not in {"success", "warning", "failed", "unknown"}:
            raise ValueError("only completed records may be published")
        if record["phase"] not in {"relax", "scf"} or record["detail"] not in DETAILS:
            raise ValueError("invalid record detail")
        if not isinstance(record["group"], str) or len(record["group"]) > 20 or not isinstance(record["time"], str) or len(record["time"]) > 30:
            raise ValueError("invalid record group or time")


def request(url, token, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "personal-task-status-collector"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=45) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raw = args.status.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    validate(data)
    print("Compact public data passed: {} bytes, {} issues, {} recent successes.".format(len(raw), len(data["issues"]), len(data["recent_success"])))
    if args.dry_run:
        return 0
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is not configured; local snapshot retained.", file=sys.stderr)
        return 2
    owner, repo, branch = os.environ.get("GITHUB_OWNER", "Leejero"), os.environ.get("GITHUB_REPO", "hpc-task-status"), os.environ.get("GITHUB_BRANCH", "main")
    if (owner, repo, branch) != ("Leejero", "hpc-task-status", "main"):
        raise ValueError("unexpected repository")
    endpoint = "https://api.github.com/repos/{}/{}/contents/data/status.json".format(owner, repo)
    current = request(endpoint + "?ref=" + branch, token)
    if json.loads(base64.b64decode(current["content"]).decode("utf-8")) == data:
        print("Remote snapshot is already identical.")
        return 0
    payload = {"message": "Update compact subtask summary", "content": base64.b64encode(raw).decode("ascii"), "sha": current["sha"], "branch": branch}
    result = request(endpoint, token, method="PUT", payload=payload)
    committed = request(endpoint + "?ref=" + branch, token)
    if json.loads(base64.b64decode(committed["content"]).decode("utf-8")) != data:
        raise RuntimeError("remote content mismatch")
    print("Published compact status; commit " + result["commit"]["sha"][:8] + ".")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print("Publish failed: {}: {}".format(type(exc).__name__, str(exc)), file=sys.stderr)
        sys.exit(1)
