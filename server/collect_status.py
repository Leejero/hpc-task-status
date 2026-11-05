#!/usr/bin/env python3
"""Collect paired VASP relax/SCF results into a public-safe status snapshot.

Only neutral aliases, ordinal indices, phase, and state leave this machine.
No Slurm IDs, job names, usernames, paths, physical parameters, energies,
OUTCAR text, or error messages are included in the public JSON.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
VALID_STATES = {"success", "warning", "failed", "running", "pending", "unknown"}
EVENT_RE = re.compile(
    r"^\[[^]]+\]\s+(RELAX ok|RELAX FAIL|SCF\s+ok|SCF\s+FAIL|SCF SKIP)\s+"
    r"([^\s/]+)/([^\s]+)(?:\s+rc=(\d+))?"
)
FATAL_RE = re.compile(
    r"ERROR:|segmentation|killed|out of memory|BAD TERMINATION|forrtl: error|internal error",
    re.IGNORECASE,
)
FRESH_SECONDS = 45 * 60


def shanghai_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def file_has(path, marker):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return any(marker in line for line in handle)
    except (OSError, UnicodeError):
        return False


def convergence_state(stage_dir, phase):
    outcar = stage_dir / "OUTCAR"
    if not outcar.is_file() or outcar.stat().st_size == 0:
        return "missing"
    marker = "reached required accuracy" if phase == "relax" else "EDIFF is reached"
    return "converged" if file_has(outcar, marker) else "unconverged"


def output_has(stage_dir, pattern):
    """Only a boolean is returned; raw errors stay on the server."""
    for name in ("vasp.out", "OUTCAR"):
        try:
            with (stage_dir / name).open("r", encoding="utf-8", errors="replace") as handle:
                if any(pattern.search(line) for line in handle):
                    return True
        except OSError:
            pass
    return False


def is_fresh(stage_dir, now=None):
    now = dt.datetime.now().timestamp() if now is None else now
    for name in ("vasp.out", "OUTCAR"):
        try:
            if now - (stage_dir / name).stat().st_mtime <= FRESH_SECONDS:
                return True
        except OSError:
            pass
    return False


def valid_energy(stage_dir):
    """Require a finite scalar energy record, not merely an exit code of zero."""
    import math
    try:
        fields = (stage_dir / "energy.txt").read_text(encoding="ascii").strip().split()
        return len(fields) == 1 and math.isfinite(float(fields[0].replace("D", "E")))
    except (OSError, ValueError, UnicodeError):
        return False


def read_pairs(manifest):
    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle, delimiter="\t") if row and not row[0].lstrip().startswith("#")]
    if not rows:
        raise ValueError("empty manifest")
    return rows


def read_events(root):
    """Read only in-memory history; never expose its raw contents."""
    events = {}
    log_path = root / "run.log"
    try:
        handle = log_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return events
    with handle:
        for line in handle:
            match = EVENT_RE.match(line.strip())
            if match:
                event, slide, distance, return_code = match.groups()
                phase = "relax" if event.startswith("RELAX") else "scf"
                outcome = "success" if event.endswith("ok") else "failed"
                events[(slide, distance, phase)] = (outcome, return_code)
    return events


def read_slurm_active():
    """Return only a boolean; raw accounting data never enters the public file."""
    command = ["squeue", "-h", "-u", os.environ.get("USER", ""), "-t", "RUNNING,PENDING,CONFIGURING", "-o", "%T"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        return result.returncode == 0 and any(line.strip() for line in result.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        return False


def collect(config_path, active=None):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    jobs = config.get("jobs", [])
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("tasks.json requires a non-empty jobs list")
    if active is None:
        active = read_slurm_active()

    tasks = []
    group_index = 0
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        if group_index >= 26:
            raise ValueError("at most 26 public task groups are supported")
        group_index += 1
        group = job.get("public_group", "任务组 " + chr(64 + group_index))
        # Explicit allowlist: arbitrary labels, real paths, or job names cannot leak.
        if not re.fullmatch(r"任务组 [A-Z]", group):
            raise ValueError("public_group must be a neutral alias such as 任务组 A")
        root = Path(job["root"])
        rows = read_pairs(root / "pairs.tsv")
        events = read_events(root)
        inherited_events = read_events(Path(job["inherit"])) if job.get("inherit") else {}
        slide_names = sorted({row[0] for row in rows})
        distance_names = sorted({row[1] for row in rows})
        slide_numbers = {name: index + 1 for index, name in enumerate(slide_names)}
        distance_numbers = {name: index + 1 for index, name in enumerate(distance_names)}
        for row in rows:
            if len(row) < 2:
                raise ValueError("each manifest row requires slide and distance")
            slide, distance = row[:2]
            direction = row[2] if len(row) > 2 else "f1_1"
            stacking = row[3] if len(row) > 3 else "norm"
            relative = Path(slide) / distance / direction / stacking
            relax_dir = root / "relax" / relative
            scf_dir = root / "scf" / relative
            if not (relax_dir / "OUTCAR").is_file() and job.get("inherit"):
                inherit_relax = Path(job["inherit"]) / "relax" / relative
                if (inherit_relax / "OUTCAR").is_file():
                    relax_dir = inherit_relax
            if not (scf_dir / "OUTCAR").is_file() and job.get("inherit"):
                inherit_scf = Path(job["inherit"]) / "scf" / relative
                if (inherit_scf / "OUTCAR").is_file():
                    scf_dir = inherit_scf
            relax = convergence_state(relax_dir, "relax")
            scf = convergence_state(scf_dir, "scf")
            event = events.get((slide, distance, "scf")) or events.get((slide, distance, "relax"))
            if event is None:
                event = inherited_events.get((slide, distance, "scf")) or inherited_events.get((slide, distance, "relax"))
            event_outcome, return_code = event if event else (None, None)
            scf_fatal = output_has(scf_dir, FATAL_RE)
            relax_fatal = output_has(relax_dir, FATAL_RE)
            phase = "relax"
            detail = "尚未开始"
            status = "pending"

            if relax == "converged" and scf == "converged" and valid_energy(scf_dir):
                status, phase, detail = "success", "scf", "弛豫、电子自洽与能量记录均通过"
            elif relax_fatal or scf_fatal:
                status, detail = "failed", "检测到计算异常终止"
                phase = "scf" if scf_fatal and relax == "converged" else "relax"
            elif scf == "converged":
                phase = "scf"
                if relax != "converged":
                    status, detail = "unknown", "计算证据不完整，无法确认结果正确"
                else:
                    status, detail = "warning", "电子自洽已收敛，但能量记录缺失或无效"
            elif relax == "converged":
                phase = "scf"
                if scf == "unconverged":
                    if is_fresh(scf_dir):
                        status, detail = "running", "静态电子自洽计算进行中"
                    else:
                        status, detail = "warning", "弛豫已收敛，静态计算尚未达到电子收敛"
                else:
                    status, detail = "pending", "弛豫已收敛，静态计算等待开始"
            elif relax == "unconverged":
                if is_fresh(relax_dir):
                    status, detail = "running", "离子弛豫计算进行中"
                elif event_outcome == "failed" and return_code not in (None, "0"):
                    status, detail = "failed", "上次计算返回非零退出码"
                elif event_outcome == "failed":
                    status, detail = "warning", "上次弛豫正常结束但未达到离子收敛"
                else:
                    status, detail = "unknown", "存在未完成输出，无法判定当前阶段"
            elif event_outcome == "failed":
                if return_code not in (None, "0"):
                    status, detail = "failed", "上次计算返回非零退出码"
                else:
                    status, detail = "warning", "上次正常退出但未达到收敛标准"
            elif relax_dir.is_dir():
                detail = "已准备，等待开始"

            position = slide_numbers[slide]
            distance_index = distance_numbers[distance]
            identifier = "G{:02d}-P{:04d}".format(group_index, len(tasks) + 1)
            task = {
                "id": identifier,
                "name": "位置 {:02d} · 距离 {:02d}".format(position, distance_index),
                "group": group,
                "status": status,
                "phase": phase,
                "detail": detail,
            }
            if status not in VALID_STATES:
                raise AssertionError("invalid public status")
            tasks.append(task)

    counts = {state: sum(task["status"] == state for task in tasks) for state in VALID_STATES}
    result = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": shanghai_now(),
        "timezone": "Asia/Shanghai",
        "collector_status": "ok",
        "job_active": bool(active),
        "counts": {state: counts[state] for state in ("success", "warning", "failed", "running", "pending", "unknown")},
        "total": len(tasks),
        "tasks": tasks,
    }
    # Keep the JSON contract deliberately small and stable.
    assert set(result) == {"schema_version", "updated_at", "timezone", "collector_status", "job_active", "counts", "total", "tasks"}
    return result


def atomic_json_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), prefix=".status-", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "tasks.json"))
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "data" / "status.json"))
    parser.add_argument("--active", choices=("auto", "yes", "no"), default="auto")
    args = parser.parse_args()
    active = None if args.active == "auto" else args.active == "yes"
    data = collect(args.config, active=active)
    atomic_json_write(args.output, data)
    counts = data["counts"]
    print("Collected {} subtasks: {} succeeded, {} warnings, {} failed, {} running, {} pending, {} unknown.".format(
        data["total"], counts["success"], counts["warning"], counts["failed"], counts["running"], counts["pending"], counts["unknown"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Collection failed: {}".format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
