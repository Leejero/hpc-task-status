#!/usr/bin/env python3
"""Monitor the prepared subtasks of the currently running VASP job.

Task trees are discovered dynamically from pairs.tsv files, but only the tree
owning the live VASP process is counted. Within that tree, only the slide
directory of the running job is considered, and only distance directories whose
VASP input set (INCAR/KPOINTS/POSCAR/POTCAR) has been prepared are included.
Other slides, archived trees and unfinished input directories stay out of the
totals. Subtask names keep their real slide/distance identifiers.
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
from collections import defaultdict
from pathlib import Path

SCHEMA_VERSION = 2
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
EVENT_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+(RELAX ok|RELAX FAIL|SCF\s+ok|SCF\s+FAIL|SCF SKIP)\s+([^\s/]+)/([^\s]+)(?:\s+\(?rc=(\d+)\)?)?")
CLOCK_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
# [11-21 16:27] FAIL s010 d-2.40  rc=7
FLAT_EVENT_RE = re.compile(r"^\[(\d{2})-(\d{2})\s+(\d{2}:\d{2})\]\s+(OK|FAIL|SKIP)\s+(\S+)\s+(\S+)(?:\s+rc=(\d+))?\s*$")
FATAL_RE = re.compile(r"ERROR:|segmentation|killed|out of memory|BAD TERMINATION|forrtl: error|internal error", re.IGNORECASE)
SLIDE_RE = re.compile(r"^(?:slide-[0-9]{3,}|s[0-9]{3,})$")
DIST_DIR_RE = re.compile(r"^d-?([0-9]+(?:\.[0-9]+)?)$")
FRESH_SECONDS = 45 * 60
TAIL_BYTES = 256 * 1024
RECENT_WINDOW_SECONDS = 24 * 3600
FUTURE_TOLERANCE_SECONDS = 5 * 60
MAX_SUCCESS = 5
MAX_ISSUES = 30
MAX_SCOPE = 10
LOG_GLOB = "run*.log"
PRUNE_DIRS = {"soft", ".git", "node_modules", "__pycache__", ".venv", "venv", "miniconda3", "anaconda3", ".cache", "build"}
STATE_ORDER = ("success", "warning", "failed", "running", "pending", "unknown")


def iso_time(timestamp):
    return dt.datetime.fromtimestamp(timestamp, SHANGHAI).isoformat(timespec="seconds")


def now_iso():
    return dt.datetime.now(SHANGHAI).isoformat(timespec="seconds")


def normalize_distance(value):
    match = DIST_DIR_RE.fullmatch(value.strip())
    return match.group(1) if match else value.strip()


def file_has(path, marker):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return any(marker in line for line in handle)
    except OSError:
        return False


def tail_text(path, limit=TAIL_BYTES):
    """Read only the end of a large VASP output; all final markers live there."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def outcar_evidence(directory, phase):
    """Tail scan: convergence marker plus last reported total energy."""
    import math
    marker = "reached required accuracy" if phase == "relax" else "EDIFF is reached"
    if not directory:
        return "missing", None
    outcar = directory / "OUTCAR"
    if not outcar.is_file() or outcar.stat().st_size == 0:
        return "missing", None
    text = tail_text(outcar)
    energy = None
    for line in text.splitlines():
        if "energy  without entropy=" in line:
            try:
                energy = float(line.split("=", 1)[1].split()[0].replace("D", "E"))
            except (ValueError, IndexError):
                pass
    if energy is not None and not math.isfinite(energy):
        energy = None
    return ("converged" if marker in text else "unconverged"), energy


def convergence_state(directory, phase):
    if not directory:
        return "missing"
    outcar = directory / "OUTCAR"
    if not outcar.is_file() or outcar.stat().st_size == 0:
        return "missing"
    return outcar_evidence(directory, phase)[0]


def output_has(directory, pattern):
    if not directory:
        return False
    for name in ("vasp.out", "OUTCAR"):
        path = directory / name
        if path.is_file() and any(pattern.search(line) for line in tail_text(path).splitlines()):
            return True
    return False


def is_fresh(directory):
    if not directory:
        return False
    threshold = dt.datetime.now().timestamp() - FRESH_SECONDS
    return any((directory / name).is_file() and (directory / name).stat().st_mtime >= threshold for name in ("vasp.out", "OUTCAR"))


def valid_energy(directory):
    import math
    if not directory:
        return False
    try:
        fields = (directory / "energy.txt").read_text(encoding="ascii").strip().split()
        return len(fields) == 1 and math.isfinite(float(fields[0].replace("D", "E")))
    except (OSError, ValueError, UnicodeError):
        return False


def output_mtime(directory):
    """Return the date-bearing mtime of the phase's primary output file."""
    if not directory:
        return None
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
    date, clock = event.get("date"), event.get("clock")
    if not date or not clock:
        return None
    try:
        moment = dt.datetime.strptime(date + " " + clock, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        return moment.timestamp()
    except ValueError:
        return None


def run_logs(root):
    """All current-job run logs, newest first (run.log, run_v6.log, ...)."""
    if not root or not root.is_dir():
        return []
    paths = [path for path in root.glob(LOG_GLOB) if path.is_file()]
    return sorted(paths, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)


def read_log_events(path):
    """Reconstruct phased event dates by walking a clock-only log backwards."""
    events = {}
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


def read_flat_log_events(path):
    """Parse [MM-DD HH:MM] OK/FAIL/SKIP <slide> d-<distance> rc=<n> logs."""
    events = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        anchor = dt.datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI)
    except OSError:
        return events
    for line in lines:
        match = FLAT_EVENT_RE.match(line.strip())
        if not match:
            continue
        month, day, clock, action, slide, distance, code = match.groups()
        try:
            moment = dt.datetime(anchor.year, int(month), int(day), int(clock[:2]), int(clock[3:]), 0, tzinfo=SHANGHAI)
            if moment > anchor + dt.timedelta(days=1):
                moment = moment.replace(year=moment.year - 1)
        except ValueError:
            continue
        events.setdefault((slide, distance), {})["RELAX"] = {
            "clock": clock + ":00", "date": moment.date().isoformat(),
            "code": code, "failed": action == "FAIL",
        }
    return events


def read_events(root):
    """Overlay all current-job logs, oldest first so newer logs win."""
    events = {}
    for path in reversed(run_logs(root)):
        for key, phases in read_log_events(path).items():
            events.setdefault(key, {}).update(phases)
        for key, phases in read_flat_log_events(path).items():
            events.setdefault(key, {}).update(phases)
    return events


def find_events(events, slide, distance):
    """Accept raw d-2.10, normalized 2.10, and re-prefixed event keys."""
    normalized = normalize_distance(distance)
    for key in ((slide, distance), (slide, normalized), (slide, "d-" + normalized)):
        if key in events:
            return events[key]
    return {}


def last_event_slide(root):
    """Last slide of the newest current-job log; never uses inherited archives."""
    for path in run_logs(root):
        slides = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    match = EVENT_RE.match(line.strip())
                    if match:
                        slides.append(match.group(3))
                    match = FLAT_EVENT_RE.match(line.strip())
                    if match:
                        slides.append(match.group(5))
        except OSError:
            continue
        if slides:
            return (path.stat().st_mtime, slides[-1])
    return None


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


def detail_for_failure(event, default="存在未完成输出"):
    if event.get("failed"):
        return ("failed", "非零退出码") if event.get("code") not in (None, "0") else ("warning", "正常退出但未收敛")
    return "unknown", default


def classify(relax_dir, scf_dir, relax, scf, event, fresh_scf, fresh_relax, active_phase, single_phase):
    """Strict state machine shared by explicit-job and discovered task paths."""
    def untouched():
        status, detail = detail_for_failure(event)
        if status == "unknown" and relax == "missing" and scf == "missing":
            return "pending", "relax", "尚未开始"
        return status, "relax", detail

    if active_phase:
        return "running", active_phase, "静态计算进行中" if active_phase == "scf" else "弛豫计算进行中"
    fatal = output_has(relax_dir, FATAL_RE) or output_has(scf_dir, FATAL_RE)
    if single_phase:
        energy = outcar_evidence(relax_dir, "relax")[1]
        if relax == "converged" and energy is not None:
            return "success", "relax", "计算成功"
        if fatal:
            return "failed", "relax", "计算异常终止"
        if relax == "converged":
            return "warning", "relax", "能量记录缺失或无效"
        if relax == "unconverged":
            if fresh_relax:
                return "running", "relax", "弛豫计算进行中"
            return untouched()
        return untouched()
    if relax == "converged" and scf == "converged" and valid_energy(scf_dir):
        return "success", "scf", "计算成功"
    if fatal:
        return "failed", "scf" if relax == "converged" else "relax", "计算异常终止"
    if scf == "converged":
        if relax != "converged":
            return "unknown", "scf", "计算证据不完整"
        return "warning", "scf", "能量记录缺失或无效"
    if relax == "converged":
        if fresh_scf:
            return "running", "scf", "静态计算进行中"
        return "warning", "scf", "静态计算未收敛"
    if relax == "unconverged":
        if fresh_relax:
            return "running", "relax", "弛豫计算进行中"
        return untouched()
    return untouched()


def evaluate(root, inherit, row, events, inherited_events, active_directories, allow_time_fallback):
    """Explicit-tree evaluation (used by tasks.json with fixed roots)."""
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
    task_events = find_events(events, slide, distance) or find_events(inherited_events, slide, distance)
    relax_event = task_events.get("RELAX", {})
    scf_event = task_events.get("SCF", {})
    event = relax_event
    active_phase = "scf" if str(scf_dir) in active_directories else "relax" if str(relax_dir) in active_directories else None
    fresh_scf = allow_time_fallback and is_fresh(scf_dir)
    fresh_relax = allow_time_fallback and is_fresh(relax_dir)
    status, phase, detail = classify(relax_dir, scf_dir, relax, scf, event, fresh_scf, fresh_relax, active_phase, False)
    if not active_phase and status == "pending" and relax_dir.is_dir():
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
        "status": status, "phase": phase, "detail": detail,
        "time": iso_time(timestamp) if timestamp is not None else "",
        "sort_key": timestamp or 0,
    }
    return record


def walk_files(base, filename, max_depth=12):
    base = Path(base)
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        relative_depth = len(Path(dirpath).relative_to(base).parts)
        if relative_depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [name for name in dirnames if name not in PRUNE_DIRS]
        if filename in filenames:
            yield Path(dirpath) / filename


def resolve_search_roots(config):
    roots = config.get("search_roots") or ["auto"]
    bases = []
    for item in roots:
        candidates = []
        if item == "auto":
            user = os.environ.get("USER") or Path.home().name
            candidates = [Path("/scratch") / user, Path.home()]
        else:
            candidates = [Path(item)]
        for candidate in candidates:
            path = candidate.resolve()
            if path.is_dir() and path not in bases:
                bases.append(path)
    return bases


def read_pair_rows(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return [], "phased"
    header = next((line for line in lines if line.startswith("#")), "")
    flat = "case" in header.lower()
    rows = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if len(parts) < (3 if flat else 2):
            continue
        case, slide, distance = ("", parts[0], parts[1]) if not flat else (parts[0], parts[1], parts[2])
        if not SLIDE_RE.fullmatch(slide):
            continue
        if not DIST_DIR_RE.fullmatch(distance) and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", distance):
            continue
        rows.append({"case": case, "slide": slide, "distance": normalize_distance(distance)})
    return rows, "flat" if flat else "phased"


def discover_trees(bases):
    trees, seen = [], set()
    for base in bases:
        for pair_file in walk_files(base, "pairs.tsv"):
            root = pair_file.parent
            if root in seen:
                continue
            seen.add(root)
            rows, layout = read_pair_rows(pair_file)
            if rows:
                trees.append({"base": base, "root": root, "rows": rows, "layout": layout})
    return trees


def owner_tree(directory, tree_roots):
    for parent in (directory, *directory.parents):
        if parent in tree_roots:
            return parent
    return None


def index_outputs(bases, tree_roots):
    index = defaultdict(list)
    for base in bases:
        for outcar in walk_files(base, "OUTCAR"):
            directory = outcar.parent
            parts = directory.parts
            slide = next((part for part in parts if SLIDE_RE.fullmatch(part)), None)
            distance = next((normalize_distance(part) for part in parts if DIST_DIR_RE.fullmatch(part)), None)
            if not slide or not distance:
                continue
            tree = owner_tree(directory, tree_roots)
            if tree is None:
                continue
            phase = "scf" if "scf" in parts else "relax"
            index[(tree, slide, distance)].append((directory, phase))
    return index


def tree_group(base, root, case):
    """Use the task tree folder name as the batch label (ASCII, no separators)."""
    return root.name[:20]


def select_current_batch(trees, active_directories, config=None):
    """Pick the single task tree that owns the live VASP work directory.

    No folder is hard-coded: the running process cwd is walked upwards to the
    nearest pairs.tsv tree. If no process is visible (job gap), the private
    config hint, then the newest run log, then the newest pairs.tsv decide.
    """
    roots = {tree["root"]: tree for tree in trees}
    for item in sorted(active_directories):
        path = Path(item)
        for parent in (path, *path.parents):
            if parent in roots:
                return roots[parent]
    hint = (config or {}).get("batch_root")
    if hint:
        hint_path = Path(hint).resolve()
        for root, tree in roots.items():
            if root == hint_path or hint_path in root.parents or root in hint_path.parents:
                return tree
    log_candidates = [
        (log.stat().st_mtime, tree["root"])
        for tree in trees for log in run_logs(tree["root"])
    ]
    if log_candidates:
        return roots[max(log_candidates, key=lambda item: item[0])[1]]
    pair_candidates = [
        ((tree["root"] / "pairs.tsv").stat().st_mtime, tree["root"])
        for tree in trees if (tree["root"] / "pairs.tsv").is_file()
    ]
    return roots[max(pair_candidates, key=lambda item: item[0])[1]] if pair_candidates else trees[0]


def evaluate_discovered(tree, row, outs, events, active_directories, allow_time_fallback):
    slide, distance = row["slide"], row["distance"]
    if row["case"]:
        outs = [item for item in outs if row["case"] in item[0].parts]
    relax_dir = next((directory for directory, phase in outs if phase == "relax"), None)
    scf_dir = next((directory for directory, phase in outs if phase == "scf"), None)
    task_dir = relax_dir or scf_dir
    active_phase = None
    for item in active_directories:
        parts = Path(item).parts
        if slide in parts and ("d-" + distance) in parts and (not row["case"] or row["case"] in parts):
            active_phase = "scf" if "scf" in parts else "relax"
            break
        if task_dir and str(item) == str(task_dir):
            active_phase = "relax" if relax_dir else "scf"
            break
    relax, relax_energy = outcar_evidence(relax_dir, "relax")
    scf, scf_energy = outcar_evidence(scf_dir, "scf")
    task_events = find_events(events, slide, distance)
    event = task_events.get("SCF") or task_events.get("RELAX") or {}
    status, phase, detail = classify(
        relax_dir, scf_dir, relax, scf, event,
        allow_time_fallback and is_fresh(scf_dir),
        allow_time_fallback and is_fresh(relax_dir),
        active_phase, scf_dir is None,
    )
    if status == "success" and scf_dir is not None and scf_energy is None and not valid_energy(scf_dir):
        status, detail = "warning", "能量记录缺失或无效"
    timestamp = event_timestamp(event)
    if timestamp is None and status in {"success", "warning", "failed", "unknown"}:
        timestamp = clock_timestamp(output_mtime(task_dir), event.get("clock"))
    return {
        "name": slide + "/" + distance,
        "group": tree_group(tree["base"], tree["root"], row["case"]),
        "status": status, "phase": phase, "detail": detail,
        "time": iso_time(timestamp) if timestamp is not None else "",
        "sort_key": timestamp or 0,
    }


def finalize(records, all_slides, log_candidates, active, process_scan, active_directories, history_path):
    now_ts = dt.datetime.now().timestamp()
    window_start = now_ts - RECENT_WINDOW_SECONDS

    def is_recent(record):
        key = record.get("sort_key", 0)
        return window_start <= key <= now_ts + FUTURE_TOLERANCE_SECONDS

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
    # Local history stays complete; only outputs that no longer exist are pruned.
    history = {key: value for key, value in history.items() if key in current_keys}
    completed = sorted(history.values(), key=lambda record: record.get("sort_key", 0), reverse=True)
    active_slides = slides_from_directories(active_directories)
    fallback_slides = {max(log_candidates)[1]} if log_candidates else set()
    recent_slides = {record["name"].split("/", 1)[0] for record in completed if is_recent(record)}
    raw_scope = active_slides or fallback_slides or recent_slides or all_slides
    scope = sorted(slide for slide in raw_scope if SLIDE_RE.fullmatch(slide))[:MAX_SCOPE] or sorted(all_slides)[:1] or ["slide-000"]
    current_records = [record for record in completed if record["name"].split("/", 1)[0] in scope and is_recent(record)]
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


def collect(config_path, active=None, history_path=None, active_directories=None):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if active is None:
        active = read_slurm_active()
    process_scan = active_directories is None
    if process_scan:
        active_directories = process_directories()
    if config.get("discover"):
        return collect_current_batch(config, active, process_scan, active_directories, history_path)
    if not isinstance(config.get("jobs"), list) or not config["jobs"]:
        raise ValueError("tasks.json requires discover=true or a non-empty jobs list")
    records, all_slides, log_candidates = [], set(), []
    for job in config["jobs"]:
        if not job.get("enabled", True):
            continue
        root = Path(job["root"])
        inherit = Path(job["inherit"]) if job.get("inherit") else None
        with (root / "pairs.tsv").open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [row for row in csv.reader(handle, delimiter="\t") if row and not row[0].lstrip().startswith("#")]
        all_slides.update(row[0] for row in rows)
        candidate = last_event_slide(root)
        if candidate:
            log_candidates.append(candidate)
        events, inherited_events = read_events(root), read_events(inherit) if inherit else {}
        records.extend(evaluate(root, inherit, row, events, inherited_events, active_directories, not process_scan) for row in rows)
    return finalize(records, all_slides, log_candidates, active, process_scan, active_directories, history_path)


def slide_directory(root, case, slide):
    """Directory holding the distance subfolders for one slide."""
    return root / case / slide if case else root / slide


def active_slide_targets(tree, active_directories):
    """(case, slide) pairs that own a live VASP working directory."""
    root = tree["root"]
    cases = {row["case"] for row in tree["rows"]}
    targets = []
    for item in sorted(active_directories):
        path = Path(item)
        for parent in (path, *path.parents):
            case_dir = parent.parent
            if case_dir.parent == root and case_dir.name in cases and SLIDE_RE.fullmatch(parent.name):
                targets.append((case_dir.name, parent.name))
                break
            if parent.parent == root and SLIDE_RE.fullmatch(parent.name) and "" in cases:
                targets.append(("", parent.name))
                break
    seen = set()
    return [target for target in targets if not (target in seen or seen.add(target))]


def prepared_distance_dirs(root, case, slide):
    """Distance folders whose VASP input set is ready for the current job."""
    directory = slide_directory(root, case, slide)
    prepared = {}
    if not directory.is_dir():
        return prepared
    for child in directory.iterdir():
        if not child.is_dir() or not DIST_DIR_RE.fullmatch(child.name):
            continue
        if all((child / name).exists() for name in ("INCAR", "KPOINTS", "POSCAR", "POTCAR")):
            prepared[normalize_distance(child.name)] = child
    return prepared


def newest_case_for_slide(root, tree, slide):
    """Pick the case whose prepared inputs for a slide were touched most recently."""
    candidates = []
    for case in {row["case"] for row in tree["rows"] if row["slide"] == slide}:
        distances = prepared_distance_dirs(root, case, slide)
        if not distances:
            continue
        mtime = max(path.stat().st_mtime for path in distances.values())
        candidates.append((mtime, case))
    return max(candidates)[1] if candidates else None


def collect_current_batch(config, active, process_scan, active_directories, history_path):
    """Monitor only prepared subtasks in the slide of the currently running batch."""
    bases = resolve_search_roots(config)
    trees = discover_trees(bases)
    if not trees:
        raise ValueError("no pairs.tsv task trees discovered")
    tree = select_current_batch(trees, active_directories, config)
    root = tree["root"]
    targets = active_slide_targets(tree, active_directories)
    if not targets:
        candidate = last_event_slide(root)
        if candidate:
            slide = candidate[1]
            case = newest_case_for_slide(root, tree, slide)
            if case is not None:
                targets = [(case, slide)]
    rows, seen = [], set()
    for case, slide in targets:
        prepared = prepared_distance_dirs(root, case, slide)
        directory = slide_directory(root, case, slide)
        for item in active_directories:
            path = Path(item)
            if path.parent == directory and DIST_DIR_RE.fullmatch(path.name):
                prepared.setdefault(normalize_distance(path.name), path)
        if not prepared:
            prepared = {
                normalize_distance(row["distance"]): None
                for row in tree["rows"] if row["case"] == case and row["slide"] == slide
            }
        for row in tree["rows"]:
            if row["case"] != case or row["slide"] != slide:
                continue
            distance = normalize_distance(row["distance"])
            if distance not in prepared:
                continue
            key = (case, slide, distance)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    if not rows:
        rows = tree["rows"]
    outputs = index_outputs([root], {root})
    events = read_events(root)
    records, all_slides, log_candidates = [], {row["slide"] for row in rows}, []
    candidate = last_event_slide(root)
    if candidate and candidate[1] in all_slides:
        log_candidates.append(candidate)
    for row in rows:
        outs = outputs.get((root, row["slide"], row["distance"]), [])
        records.append(evaluate_discovered(tree, row, outs, events, active_directories, not process_scan))
    return finalize(records, all_slides, log_candidates, active, process_scan, active_directories, history_path)


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
