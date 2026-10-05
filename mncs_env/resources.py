"""Bounded Linux process observations; no service identity or health policy.

Never opens target handles, attaches to a process, or changes limits. A PID is
an observation address, not an Environment session or ownership identity.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path

MAX_PROCESSES = 8
MAX_DESCRIPTORS = 8192
MAX_WATCH_ENTRIES = 8192


def _fields(path: Path) -> dict[str, str]:
    return dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)


def _executable_observation(root: Path) -> tuple[str | None, str | None]:
    """Observe the image currently exposed by procfs; this is not build proof."""
    try:
        path = os.readlink(root / "exe")
    except OSError:
        return None, None
    digest = hashlib.sha256()
    try:
        with (root / "exe").open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return path, None
    return path, f"sha256:{digest.hexdigest()}"


def process(pid: int, *, proc_root: Path = Path("/proc")) -> dict:
    result = {"pid": pid, "status": "unknown"}
    root = proc_root / str(pid)
    try:
        # Linux stat starttime prevents silently combining a recycled PID.
        birth = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
        fields = _fields(root / "status")
        hwm = fields.get("VmHWM")
        executable, executable_sha256 = _executable_observation(root)
        result.update(name=fields["Name"].strip(), ppid=int(fields["PPid"]),
                      start_ticks=int(birth),
                      rss_bytes=int(fields.get("VmRSS", "0 kB").split()[0]) * 1024,
                      rss_hwm_bytes=(int(hwm.split()[0]) * 1024 if hwm else None),
                      executable=executable, executable_sha256=executable_sha256)
        for line in (root / "limits").read_text().splitlines():
            if line.startswith("Max open files"):
                soft, hard = line[len("Max open files"):].split()[:2]
                result["fd_limit_soft"] = int(soft) if soft != "unlimited" else None
                result["fd_limit_hard"] = int(hard) if hard != "unlimited" else None
        classes = Counter()
        count = deleted = watches = 0
        incomplete = truncated = watches_truncated = False
        with os.scandir(root / "fd") as entries:
            for entry in entries:
                if count == MAX_DESCRIPTORS:
                    truncated = True
                    break
                count += 1
                try:
                    target = os.readlink(entry.path)
                    kind = ("pipe" if target.startswith("pipe:") else
                            "socket" if target.startswith("socket:") else
                            target if target.startswith("anon_inode:") else "file_or_device")
                    classes[kind] += 1
                    deleted += target.endswith(" (deleted)")
                    if "inotify" in target:
                        with (root / "fdinfo" / entry.name).open() as info:
                            for line in info:
                                if watches == MAX_WATCH_ENTRIES:
                                    watches_truncated = True
                                    break
                                watches += line.startswith("inotify wd:")
                except OSError:
                    incomplete = True
        result.update(fd_count=None if truncated else count, fd_count_observed=count,
                      fd_scan_truncated=truncated, fd_classes=dict(classes),
                      deleted_handles=deleted, inotify_watch_entries=watches,
                      watch_scan_truncated=watches_truncated,
                      fd_details_partial=incomplete or truncated or watches_truncated)
        soft = result.get("fd_limit_soft")
        result["fd_headroom"] = max(0, soft - count) if soft is not None and not truncated else None
        result["cgroup"] = (root / "cgroup").read_text().strip()
        after = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if after != birth:
            return {"pid": pid, "status": "unknown", "reason": "pid-reused-during-observation"}
        result["status"] = "observed"
    except (OSError, ValueError, KeyError, IndexError) as error:
        result["reason"] = type(error).__name__
    return result


def observe(pids: list[int] | None = None, *, proc_root: Path = Path("/proc")) -> dict:
    if pids is not None and (not pids or len(pids) > MAX_PROCESSES or any(pid <= 0 for pid in pids)):
        raise ValueError(f"select 1 to {MAX_PROCESSES} positive PIDs")
    rows = []
    ancestry = pids is None
    selected = [os.getpid()] if ancestry else list(dict.fromkeys(pids))
    seen = set()
    while selected and len(rows) < MAX_PROCESSES:
        pid = selected.pop(0)
        if pid in seen:
            break
        seen.add(pid)
        row = process(pid, proc_root=proc_root)
        rows.append(row)
        if ancestry and row.get("ppid", 0) > 0:
            selected.append(row["ppid"])
    system = {"status": "unknown"}
    try:
        memory = _fields(proc_root / "meminfo")
        allocated, unused, maximum = map(int, (proc_root / "sys/fs/file-nr").read_text().split())
        system = {"status": "observed", "memory_total_bytes": int(memory["MemTotal"].split()[0]) * 1024,
                  "memory_available_bytes": int(memory["MemAvailable"].split()[0]) * 1024,
                  "file_handles_allocated": allocated, "file_handles_unused": unused,
                  "file_handles_max": maximum}
    except (OSError, ValueError, KeyError) as error:
        system["reason"] = type(error).__name__
    return {"schema_version": "mncs.environment.resource-observation/1",
            "observation": "live", "observed_at": datetime.now(timezone.utc).isoformat(),
            "scope": "caller-ancestry" if ancestry else "explicit-pids",
            "session_process_mapping": "not-asserted", "processes": rows,
            "process_limit": MAX_PROCESSES, "descriptor_scan_limit": MAX_DESCRIPTORS,
            "watch_scan_limit": MAX_WATCH_ENTRIES,
            "ancestry_truncated": bool(selected), "system": system}
