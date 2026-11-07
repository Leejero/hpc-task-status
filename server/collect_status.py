#!/usr/bin/env python3
"""Collect VASP states into counts and a current-slide completion summary.

Only running tasks and completion records belonging to the active slide are
published; pending tasks remain counts. Subtask names retain slide/distance
identifiers. Dates in the clock-only, multi-day run.log are reconstructed from
its chronological order; output-file mtimes are used only when no log event is
available.
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 2
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
EVENT_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(RELAX ok|RELAX FAIL|SCF\s+ok|SCF\s+FAIL|SCF SKIP)\s+([^\s/]+)/([^\s]+)(?:\s+\(?rc=(\d+)\)?)?")
CLOCK_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
FATAL_RE = re.compile(r"ERROR:|segmentation|killed|out of memory|BAD TERMINATION|forrtl: error|internal error", re.IGNORECASE)
SLIDE_RE = re.compile(r"^slide-[0-9]{3,}$")
FRESH_SECONDS = 45 * 60
MAX_SUCCESS = 5
MAX_ISSUES = 30
STATE_ORDER = ("success", "warning", "failed", "running", "pending", "unknown")


def iso_time(timestamp):
    return dt.datetime.fromtimestamp(timestamp, SHANGHAI).isoformat(timespec="seconds")


def now_iso():
    return dt.datetime.now(SHANGHAI).isoformat(timespec="seconds")


def file_has(path, marker):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return any(marker in line for line in handle)
    except OSError:
        return False


def convergence_state(directory, phase):
    outcar = directory / "OUTCAR"
    if not outcar.is_file() or outcar.stat().st_size == 0:
        return "missing"
    marker = "reached required accuracy" if phase == "relax" else "EDIFF is reached"
    return "converged" if file_has(outcar, marker) else "unconverged"


def output_has(directory, pattern):
    for name in ("vasp.out", "OUTCAR"):
        try:
            with (directory / name).open("r", encoding="utf-8", errors="replace") as handle:
                if any(pattern.search(line) for line in handle):
                    return True
        except OSError:
            pass
    return False


def is_fresh(directory):
    threshold = dt.datetime.now().timestamp() - FRESH_SECONDS
    return any((directory / name).is_file() and (directory / name).stat().st_mtime >= threshold for name in ("vasp.out", "OUTCAR"))


def valid_energy(directory):
    import math
    try:
        fields = (directory / "energy.txt").read_text(encoding="ascii").strip().split()
        return len(fields) == 1 and math.isfinite(float(fields[0].replace("D", "E")))
    except (OSError, ValueError, UnicodeError):
        return False


def output_mtime(directory):
    """Return the date-bearing mtime of the phase's primary output file."""
    for name in ("OUTCAR", "vasp.out", "energy.txt"):
        try:
            return (directory / name).stat().st_mtime
        except OSError:
            continue
    return None


def clock_seconds(clock):
    hour, minute, second = map(int, clock.split(":"))
    return hour * 3600 + minute * 60 + second


def clock_timestamp(reference, clock):
    """Attach a run.log clock to the calendar date of that task's output."""
    if reference is None:
        return None
    if not clock:
        return reference
    reference_time = dt.datetime.fromtimestamp(reference, SHANGHAI)
    try:
        hour, minute, second = map(int, clock.split(":"))
        moment = reference_time.replace(hour=hour, minute=minute, second=second, microsecond=0)
    except ValueError:
        return reference
    if moment > reference_time + dt.timedelta(minutes=5):
        moment -= dt.timedelta(days=1)
    return moment.timestamp()


def event_timestamp(event):
    """Use the calendar date reconstructed from the log's chronology."""
    date, clock = event.get("date"), event.get("clock")
    if not date or not clock:
        return None
    try:
        moment = dt.datetime.strptime(date + " " + clock, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        return moment.timestamp()
    except ValueError:
        return None


def read_events(root):
    """Reconstruct event dates by walking a clock-only log backwards in time."""
    events = {}
    path = root / "run.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        anchor = dt.datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI)
    except OSError:
        return events
    timestamped = [(line.strip(), CLOCK_RE.match(line.strip())) for line in lines]
    timestamped = [(line, match) for line, match in timestamped if match]
    if not timestamped:
        return events
    day = anchor.date()
    anchor_seconds = anchor.hour * 3600 + anchor.minute * 60 + anchor.second + 1
    last_seconds = clock_seconds(timestamped[-1][1].group(1))
    if last_seconds > anchor_seconds:
        day -= dt.timedelta(days=1)
    later = last_seconds
    for line, clock_match in reversed(timestamped):
        clock = clock_match.group(1)
        value = clock_seconds(clock)
        if value > later:
            day -= dt.timedelta(days=1)
        later = value
        match = EVENT_RE.match(line)
        if match:
            _, event, slide, distance, code = match.groups()
            events.setdefault((slide, distance), {})[event.split()[0]] = {
                "clock": clock, "date": day.isoformat(),
                "code": code, "failed": event.endswith("FAIL"),
            }
    return events


def last_event_slide(root):
    """Find the most recently updated log's last slide, for idle periods."""
    if not root:
        return None
    path = root / "run.log"
    result = None
    try:
        mtime = path.stat().st_mtime
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = EVENT_RE.match(line.strip())
                if match:
                    result = match.group(3)
    except OSError:
        return None
    return (mtime, result) if result else None


def slides_from_directories(directories):
    return {part for value in directories for part in Path(value).parts if SLIDE_RE.fullmatch(part)}


def process_directories():
    """Read this user's real VASP working directories, not other users' jobs."""
    directories = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return directories
    uid = os.geteuid()
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            executable = (process / "exe").resolve()
            if not executable.name.startswith("vasp_"):
                continue
            directories.add(str((process / "cwd").resolve()))
        except OSError:
            continue
    return directories


def read_slurm_active():
    try:
        result = subprocess.run(["squeue", "-h", "-u", os.environ.get("USER", ""), "-t", "RUNNING,PENDING,CONFIGURING", "-o", "%T"], capture_output=True, text=True, timeout=15, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def previous_summary(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if data.get("schema_version") == 2 else {}
    except (OSError, ValueError):
        return {}


def distance_name(value):
    return value[2:] if value.startswith("d-") else value


def evaluate(root, inherit, row, events, inherited_events, active_directories, allow_time_fallback):
    slide, distance = row[:2]
    direction = row[2] if len(row) > 2 else "f1_1"
    stacking = row[3] if len(row) > 3 else "norm"
    relative = Path(slide) / distance / direction / stacking
    relax_dir = root / "relax" / relative
    scf_dir = root / "scf" / relative
    inherited_relax = inherit / "relax" / relative if inherit else None
    inherited_scf = inherit / "scf" / relative if inherit else None
    if not (relax_dir / "OUTCAR").is_file() and inherited_relax and (inherited_relax / "OUTCAR").is_file():
        relax_dir = inherited_relax
    if not (scf_dir / "OUTCAR").is_file() and inherited_scf and (inherited_scf / "OUTCAR").is_file():
        scf_dir = inherited_scf
    relax = convergence_state(relax_dir, "relax")
    scf = convergence_state(scf_dir, "scf")
    task_events = events.get((slide, distance)) or inherited_events.get((slide, distance)) or {}
    relax_event = task_events.get("RELAX", {})
    scf_event = task_events.get("SCF", {})
    event = relax_event
    active_phase = "scf" if str(scf_dir) in active_directories else "relax" if str(relax_dir) in active_directories else None
    fresh_scf = allow_time_fallback and is_fresh(scf_dir)
    fresh_relax = allow_time_fallback and is_fresh(relax_dir)
    status, phase, detail = "pending", "relax", "尚未开始"
    if active_phase:
        status, phase = "running", active_phase
        detail = "静态计算进行中" if active_phase == "scf" else "弛豫计算进行中"
    elif relax == "converged" and scf == "converged" and valid_energy(scf_dir):
        status, phase, detail = "success", "scf", "计算成功"
    elif output_has(relax_dir, FATAL_RE) or output_has(scf_dir, FATAL_RE):
        status, phase, detail = "failed", "scf" if relax == "converged" else "relax", "计算异常终止"
    elif scf == "converged":
        phase = "scf"
        status, detail = ("unknown", "计算证据不完整") if relax != "converged" else ("warning", "能量记录缺失或无效")
    elif relax == "converged":
        phase = "scf"
        if scf == "unconverged":
            status, detail = ("running", "静态计算进行中") if fresh_scf else ("warning", "静态计算未收敛")
        else:
            detail = "静态计算等待开始"
    elif relax == "unconverged":
        if fresh_relax:
            status, detail = "running", "弛豫计算进行中"
        elif event.get("failed") and event.get("code") not in (None, "0"):
            status, detail = "failed", "非零退出码"
        elif event.get("failed"):
            status, detail = "warning", "正常退出但未收敛"
        else:
            status, detail = "unknown", "存在未完成输出"
    elif event.get("failed"):
        status, detail = ("failed", "非零退出码") if event.get("code") not in (None, "0") else ("warning", "正常退出但未收敛")
    elif relax_dir.is_dir():
        detail = "已准备，等待开始"
    event = scf_event if status == "success" and scf_event else relax_event
    timestamp = event_timestamp(event)
    if timestamp is None and status in {"success", "warning", "failed", "unknown"}:
        phase_dir = scf_dir if phase == "scf" else relax_dir
        candidates = (phase_dir, relax_dir, scf_dir) if phase == "scf" else (phase_dir, scf_dir)
        reference = next((value for value in (output_mtime(item) for item in candidates) if value is not None), None)
        timestamp = clock_timestamp(reference, event.get("clock") or relax_event.get("clock"))
    record = {
        "name": slide + "/" + distance_name(distance),
        "group": "2H" if root.name == "2H" else "3C" if root.name == "3C" else root.name,
        "status": status,
        "phase": phase,
        "detail": detail,
        "time": iso_time(timestamp) if timestamp is not None else "",
        "sort_key": timestamp or 0,
    }
    return record


def collect(config_path, active=None, history_path=None, active_directories=None):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config.get("jobs"), list) or not config["jobs"]:
        raise ValueError("tasks.json requires a non-empty jobs list")
    if active is None:
        active = read_slurm_active()
    process_scan = active_directories is None
    if process_scan:
        active_directories = process_directories()
    records = []
    all_slides, log_candidates = set(), []
    for job in config["jobs"]:
        if not job.get("enabled", True):
            continue
        root = Path(job["root"])
        inherit = Path(job["inherit"]) if job.get("inherit") else None
        with (root / "pairs.tsv").open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.reader(handle, delimiter="\t") if row and not row[0].lstrip().startswith("#")]
        all_slides.update(row[0] for row in rows)
        candidate = last_event_slide(root) or (last_event_slide(inherit) if inherit else None)
        if candidate:
            log_candidates.append(candidate)
        events, inherited_events = read_events(root), read_events(inherit) if inherit else {}
        records.extend(evaluate(root, inherit, row, events, inherited_events, active_directories, not process_scan) for row in rows)
    active_slides = slides_from_directories(active_directories)
    fallback_slides = {max(log_candidates)[1]} if log_candidates else set()
    scope = sorted(active_slides or fallback_slides or all_slides)
    counts = {state: sum(record["status"] == state for record in records) for state in STATE_ORDER}
    previous = previous_summary(history_path) if history_path else {}
    history = {}
    for record in previous.get("history", []):
        if isinstance(record, dict) and "name" in record and "group" in record:
            history[(record["group"], record["name"])] = record
    current_keys = {(record["group"], record["name"]) for record in records}
    for record in records:
        key = (record["group"], record["name"])
        if record["status"] in {"success", "warning", "failed", "unknown"}:
            history[key] = record
        elif key in history:
            del history[key]
    history = {key: value for key, value in history.items() if key in current_keys}
    completed = sorted(history.values(), key=lambda record: record.get("sort_key", 0), reverse=True)
    current_records = [record for record in completed if record["name"].split("/", 1)[0] in scope]
    issues = [record for record in current_records if record["status"] in {"failed", "warning", "unknown"}][:MAX_ISSUES]
    successes = [record for record in current_records if record["status"] == "success"][:MAX_SUCCESS]
    running = [{key: value for key, value in record.items() if key not in {"sort_key", "time"}} for record in records if record["status"] == "running"]
    def public(record):
        return {key: value for key, value in record.items() if key != "sort_key"}
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_iso(),
        "timezone": "Asia/Shanghai",
        "collector_status": "ok",
        "job_active": bool(active),
        "total": len(records),
        "counts": counts,
        "scope": scope,
        "running_tasks": running,
        "issues": [public(record) for record in issues],
        "recent_success": [public(record) for record in successes],
        "history": completed,
    }


def public_data(data):
    return {key: value for key, value in data.items() if key != "history"}


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
    parser.add_argument("--history", default=str(Path(__file__).resolve().parent / "data" / "history.json"))
    parser.add_argument("--active", choices=("auto", "yes", "no"), default="auto")
    args = parser.parse_args()
    active = None if args.active == "auto" else args.active == "yes"
    data = collect(args.config, active=active, history_path=args.history)
    atomic_json_write(args.history, data)
    atomic_json_write(args.output, public_data(data))
    counts = data["counts"]
    print("Collected {} subtasks: {} succeeded, {} warnings, {} failed, {} running, {} pending, {} unknown.".format(data["total"], counts["success"], counts["warning"], counts["failed"], counts["running"], counts["pending"], counts["unknown"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Collection failed: {}".format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
