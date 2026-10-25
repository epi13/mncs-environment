"""Bounded Linux process observations; no service identity or health policy.

Never opens target handles, attaches to a process, or changes limits. A PID is
an observation address, not an Environment session or ownership identity.
"""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_PROCESSES = 8
MAX_DESCRIPTORS = 8192
MAX_WATCH_ENTRIES = 8192
MAX_CGROUP_VALUE_BYTES = 4096


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


def _bounded_cgroup_text(path: Path) -> str:
    """Read one small procfs/cgroup value without accepting unbounded input."""
    with path.open("rb") as handle:
        raw = handle.read(MAX_CGROUP_VALUE_BYTES + 1)
    if len(raw) > MAX_CGROUP_VALUE_BYTES:
        raise ValueError("resource-value-too-large")
    return raw.decode("utf-8").strip()


def _cgroup_value(
    directory: Path, root: Path, filename: str, *, allow_max: bool = False,
) -> dict:
    try:
        path = (directory / filename).resolve(strict=True)
        path.relative_to(root)
        raw = _bounded_cgroup_text(path)
    except ValueError as error:
        reason = ("resource-value-too-large" if str(error) == "resource-value-too-large"
                  else "cgroup-file-outside-root")
        return {"value": None, "status": "unavailable", "reason": reason}
    except (OSError, UnicodeError) as error:
        return {"value": None, "status": "unavailable", "reason": type(error).__name__}
    if raw == "max" and allow_max:
        return {"value": None, "status": "unbounded"}
    if not raw.isascii() or not raw.isdecimal():
        return {"value": None, "status": "unavailable",
                "reason": "invalid-nonnegative-integer"}
    return {"value": int(raw), "status": "observed"}


def _cpu_quota(directory: Path, root: Path) -> dict:
    try:
        path = (directory / "cpu.max").resolve(strict=True)
        path.relative_to(root)
        fields = _bounded_cgroup_text(path).split()
    except ValueError as error:
        reason = ("resource-value-too-large" if str(error) == "resource-value-too-large"
                  else "cgroup-file-outside-root")
        return {"status": "unavailable", "reason": reason}
    except (OSError, UnicodeError) as error:
        return {"status": "unavailable", "reason": type(error).__name__}
    if len(fields) != 2 or not fields[1].isascii() or not fields[1].isdecimal():
        return {"status": "unavailable", "reason": "invalid-cpu-max"}
    period_us = int(fields[1])
    if period_us <= 0:
        return {"status": "unavailable", "reason": "invalid-cpu-period"}
    if fields[0] == "max":
        return {
            "status": "observed",
            "quota_us": None,
            "quota_status": "unbounded",
            "period_us": period_us,
            "quota_cores": None,
            "quota_cores_basis": "derived-from-cpu.max-quota-divided-by-period",
        }
    if not fields[0].isascii() or not fields[0].isdecimal():
        return {"status": "unavailable", "reason": "invalid-cpu-quota"}
    quota_us = int(fields[0])
    if quota_us <= 0:
        return {"status": "unavailable", "reason": "invalid-cpu-quota"}
    return {
        "status": "observed",
        "quota_us": quota_us,
        "quota_status": "observed",
        "period_us": period_us,
        "quota_cores": quota_us / period_us,
        "quota_cores_basis": "derived-from-cpu.max-quota-divided-by-period",
    }


def _cgroup_v2_resource_limits(membership: str, cgroup_root: Path) -> dict:
    """Read bounded cgroup v2 limits without inferring Environment session ownership."""
    unified_paths = []
    for line in membership.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            unified_paths.append(fields[2])
    if len(unified_paths) != 1:
        return {"schema_version": "mncs.environment.cgroup-resource-observation/1",
                "status": "unavailable",
                "reason": "unified-cgroup-v2-membership-not-found"}

    raw_path = unified_paths[0]
    path_parts = PurePosixPath(raw_path).parts
    relative_parts = path_parts[1:] if path_parts and path_parts[0] == "/" else ()
    if (not raw_path.startswith("/") or "\x00" in raw_path
            or any(part in {"", ".", ".."} or "\\" in part
                   for part in relative_parts)):
        return {"schema_version": "mncs.environment.cgroup-resource-observation/1",
                "status": "unavailable", "reason": "invalid-cgroup-v2-path"}
    try:
        root = cgroup_root.resolve(strict=True)
        directory = root.joinpath(*relative_parts).resolve(strict=True)
        try:
            directory.relative_to(root)
        except ValueError:
            return {"schema_version": "mncs.environment.cgroup-resource-observation/1",
                    "status": "unavailable",
                    "reason": "cgroup-directory-outside-root"}
        if not directory.is_dir():
            raise NotADirectoryError
    except OSError as error:
        return {"schema_version": "mncs.environment.cgroup-resource-observation/1",
                "status": "unavailable",
                "reason": type(error).__name__}

    memory_current = _cgroup_value(directory, root, "memory.current")
    memory_max = _cgroup_value(directory, root, "memory.max", allow_max=True)
    cpu = _cpu_quota(directory, root)
    pids_current = _cgroup_value(directory, root, "pids.current")
    pids_max = _cgroup_value(directory, root, "pids.max", allow_max=True)
    states = [
        memory_current["status"], memory_max["status"], cpu["status"],
        pids_current["status"], pids_max["status"],
    ]
    status = (
        "observed" if all(value in {"observed", "unbounded"} for value in states)
        else "partial"
    )
    return {
        "schema_version": "mncs.environment.cgroup-resource-observation/1",
        "status": status,
        "measurement_basis": "read-only-cgroup-v2-files",
        "session_process_mapping": "not-asserted",
        "memory": {
            "current_bytes": memory_current["value"],
            "current_status": memory_current["status"],
            "max_bytes": memory_max["value"],
            "max_status": memory_max["status"],
        },
        "cpu": cpu,
        "pids": {
            "current": pids_current["value"],
            "current_status": pids_current["status"],
            "max": pids_max["value"],
            "max_status": pids_max["status"],
        },
    }


def process(
    pid: int, *, proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict:
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
        try:
            cgroup_membership = _bounded_cgroup_text(root / "cgroup")
        except (OSError, ValueError, UnicodeError) as error:
            result["cgroup"] = None
            if isinstance(error, ValueError):
                reason = "resource-value-too-large"
            else:
                reason = type(error).__name__
            result["cgroup_resource_limits"] = {
                "schema_version": "mncs.environment.cgroup-resource-observation/1",
                "status": "unavailable", "reason": reason,
            }
        else:
            result["cgroup"] = cgroup_membership
            result["cgroup_resource_limits"] = _cgroup_v2_resource_limits(
                cgroup_membership, cgroup_root,
            )
        after = (root / "stat").read_text().rsplit(")", 1)[1].split()[19]
        if after != birth:
            return {"pid": pid, "status": "unknown", "reason": "pid-reused-during-observation"}
        result["status"] = "observed"
    except (OSError, ValueError, KeyError, IndexError) as error:
        result["reason"] = type(error).__name__
    return result


def observe(
    pids: list[int] | None = None, *, proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict:
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
        row = process(pid, proc_root=proc_root, cgroup_root=cgroup_root)
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
