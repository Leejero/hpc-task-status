#!/usr/bin/env python3
"""Validate a sanitized status snapshot and publish it with GitHub Contents API."""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOP_KEYS = {"schema_version", "updated_at", "timezone", "collector_status", "job_active", "counts", "total", "tasks"}
TASK_KEYS = {"id", "name", "group", "status", "phase", "detail"}
STATES = {"success", "warning", "failed", "running", "pending", "unknown"}
ALLOWED_DETAILS = {
    "尚未开始", "已准备，等待开始", "弛豫、电子自洽与能量记录均通过",
    "检测到计算异常终止", "计算证据不完整，无法确认结果正确",
    "电子自洽已收敛，但能量记录缺失或无效", "静态电子自洽计算进行中",
    "弛豫已收敛，静态计算尚未达到电子收敛", "弛豫已收敛，静态计算等待开始",
    "离子弛豫计算进行中", "上次弛豫正常结束但未达到离子收敛",
    "存在未完成输出，无法判定当前阶段", "上次计算返回非零退出码",
    "上次正常退出但未达到收敛标准",
}


def validate(data):
    if set(data) != TOP_KEYS or data.get("schema_version") != 1:
        raise ValueError("unexpected top-level schema")
    if data.get("timezone") != "Asia/Shanghai" or data.get("collector_status") != "ok":
        raise ValueError("invalid collection metadata")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks or data.get("total") != len(tasks):
        raise ValueError("invalid task list")
    if not isinstance(data.get("job_active"), bool):
        raise ValueError("invalid job activity flag")
    for key, value in data.get("counts", {}).items():
        if key not in STATES or not isinstance(value, int) or value < 0:
            raise ValueError("invalid counts")
    if set(data["counts"]) != STATES or sum(data["counts"].values()) != len(tasks):
        raise ValueError("counts do not match tasks")
    for task in tasks:
        if set(task) != TASK_KEYS:
            raise ValueError("unexpected task fields")
        if task["status"] not in STATES or task["phase"] not in {"relax", "scf"}:
            raise ValueError("invalid task state")
        if task["detail"] not in ALLOWED_DETAILS:
            raise ValueError("non-allowlisted public detail")
        if task["group"] not in {"任务组 A", "任务组 B"}:
            raise ValueError("non-allowlisted public group")
        if not task["id"].startswith("G") or not task["name"].startswith("位置 "):
            raise ValueError("non-allowlisted task alias")


def request(url, token, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "personal-task-status-collector",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raw = args.status.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    validate(data)
    print("Public-data validation passed: {} neutral task records.".format(len(data["tasks"])))
    if args.dry_run:
        return 0

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("GITHUB_TOKEN is not configured; local snapshot retained, no upload attempted.", file=sys.stderr)
        return 2
    owner = os.environ.get("GITHUB_OWNER", "Leejero")
    repo = os.environ.get("GITHUB_REPO", "hpc-task-status")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    if owner != "Leejero" or repo != "hpc-task-status" or branch != "main":
        raise ValueError("refusing to publish to an unexpected repository")
    endpoint = "https://api.github.com/repos/{}/{}/contents/data/status.json".format(owner, repo)
    current = request(endpoint + "?ref=" + branch, token)
    old_bytes = base64.b64decode(current["content"])
    if json.loads(old_bytes.decode("utf-8")) == data:
        print("Remote snapshot is already identical; no commit needed.")
        return 0
    payload = {
        "message": "Update sanitized subtask status",
        "content": base64.b64encode(raw).decode("ascii"),
        "sha": current["sha"],
        "branch": branch,
    }
    result = request(endpoint, token, method="PUT", payload=payload)
    # The Contents PUT response contains a blob SHA but not necessarily file content.
    # Fetch the committed file back before declaring a successful publication.
    committed = request(endpoint + "?ref=" + branch, token)
    remote_bytes = base64.b64decode(committed["content"])
    if json.loads(remote_bytes.decode("utf-8")) != data:
        raise RuntimeError("remote content does not match the validated snapshot")
    print("Published sanitized status; commit " + result["commit"]["sha"][:8] + ".")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        print("Publish failed: {}: {}".format(type(exc).__name__, str(exc)), file=sys.stderr)
        sys.exit(1)
