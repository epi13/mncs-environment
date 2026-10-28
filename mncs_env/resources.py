"""Bounded Linux process observations; no service identity or health policy.

Never opens target handles, attaches to a process, or changes limits. A PID is
an observation address, not an Environment session or ownership identity.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

MAX_PROCESSES = 8
MAX_DESCRIPTORS = 8192
MAX_WATCH_ENTRIES = 8192
MAX_CGROUP_VALUE_BYTES = 4096
MAX_PROCESS_IO_BYTES = 1024
PROCESS_RESOURCE_USAGE_SCHEMA = "mncs.environment.process-resource-usage/1"
PROCESS_RESOURCE_MEASUREMENT_BASES = frozenset({
    "process-counters-bracketing-owner-runner",
    "process-counters-bracketing-result-reference-publication",
})
PROCESS_IO_FIELDS = (
    "rchar", "wchar", "read_bytes", "write_bytes",
    "cancelled_write_bytes",
)


def _fields(path: Path) -> dict[str, str]:
    return dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)


def _process_stat_counters(root: Path) -> dict[str, int]:
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    if len(fields) <= 19:
        raise ValueError("invalid-process-stat")
    values = {
        "cpu_user_ticks": int(fields[11]),
        "cpu_system_ticks": int(fields[12]),
        "process_start_ticks": int(fields[19]),
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("invalid-process-stat-counter")
    return values


def _process_io(root: Path) -> dict[str, Any]:
    try:
        with (root / "io").open("rb") as handle:
            raw = handle.read(MAX_PROCESS_IO_BYTES + 1)
    except OSError as error:
        return {
            "status": "unavailable", "reason_code": type(error).__name__,
            "counters": {field: None for field in PROCESS_IO_FIELDS},
        }
    if len(raw) > MAX_PROCESS_IO_BYTES:
        return {
            "status": "unavailable", "reason_code": "resource-value-too-large",
            "counters": {field: None for field in PROCESS_IO_FIELDS},
        }
    values: dict[str, int] = {}
    invalid = False
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        lines = []
        invalid = True
    for line in lines:
        name, separator, raw_value = line.partition(":")
        if not separator or name not in PROCESS_IO_FIELDS:
            continue
        value_text = raw_value.strip()
        if value_text.isascii() and value_text.isdecimal():
            value = int(value_text)
        else:
            invalid = True
            continue
        if name in values:
            invalid = True
            continue
        values[name] = value
    counters = {field: values.get(field) for field in PROCESS_IO_FIELDS}
    status = (
        "complete" if len(values) == len(PROCESS_IO_FIELDS) and not invalid
        else "partial" if values
        else "unavailable"
    )
    return {
        "status": status,
        "reason_code": "invalid-process-io" if invalid else None,
        "counters": counters,
    }


def process_usage_sample(
    pid: int, *, proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Read bounded counters for one process without opening target handles."""
    if type(pid) is not int or pid <= 0:
        return {
            "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "unavailable", "reason_code": "invalid-pid",
        }
    root = proc_root / str(pid)
    try:
        before_stat = _process_stat_counters(root)
        fields = _fields(root / "status")
        rss_text = fields.get("VmRSS", "").split()
        hwm_text = fields.get("VmHWM", "").split()
        rss_bytes = int(rss_text[0]) * 1024 if rss_text else None
        rss_high_water_bytes = int(hwm_text[0]) * 1024 if hwm_text else None
        if any(value is not None and value < 0
               for value in (rss_bytes, rss_high_water_bytes)):
            raise ValueError("invalid-process-rss")
        io = _process_io(root)
        after_stat = _process_stat_counters(root)
    except (OSError, ValueError, KeyError, IndexError) as error:
        return {
            "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "unavailable", "reason_code": type(error).__name__,
        }
    if before_stat["process_start_ticks"] != after_stat["process_start_ticks"]:
        return {
            "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "unavailable", "reason_code": "pid-reused-during-observation",
        }
    status = (
        "observed"
        if rss_bytes is not None and rss_high_water_bytes is not None
        and io["status"] == "complete"
        else "partial"
    )
    return {
        "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
        "status": status,
        "pid": pid,
        "process_start_ticks": after_stat["process_start_ticks"],
        "cpu_user_ticks": after_stat["cpu_user_ticks"],
        "cpu_system_ticks": after_stat["cpu_system_ticks"],
        "rss_bytes": rss_bytes,
        "rss_high_water_bytes": rss_high_water_bytes,
        "io_status": io["status"],
        "io_reason_code": io["reason_code"],
        "io_counters": io["counters"],
    }


def process_usage_delta(
    before: Any, after: Any, *, elapsed_seconds: float | None = None,
    measurement_basis: str = "process-counters-bracketing-owner-runner",
) -> dict[str, Any]:
    """Derive same-process counter deltas, retaining unavailable measurements."""
    if measurement_basis not in PROCESS_RESOURCE_MEASUREMENT_BASES:
        raise ValueError("unsupported process resource measurement basis")

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "unavailable",
            "measurement_basis": measurement_basis,
            "reason_code": reason,
        }

    if not isinstance(before, dict) or not isinstance(after, dict):
        return unavailable("sample-not-recorded")
    if before.get("schema_version") != PROCESS_RESOURCE_USAGE_SCHEMA or after.get(
        "schema_version"
    ) != PROCESS_RESOURCE_USAGE_SCHEMA:
        return unavailable("sample-schema-mismatch")
    pid = before.get("pid")
    start_ticks = before.get("process_start_ticks")
    if (before.get("status") == "unavailable"
            or after.get("status") == "unavailable"):
        return unavailable(str(after.get("reason_code") or before.get("reason_code")
                              or "process-counter-unavailable"))
    if (type(pid) is not int or pid <= 0 or after.get("pid") != pid
            or type(start_ticks) is not int or start_ticks < 0
            or after.get("process_start_ticks") != start_ticks):
        return unavailable("process-identity-changed")

    def delta(field: str) -> int | None:
        return _counter_delta(before.get(field), after.get(field))

    before_io = before.get("io_counters")
    after_io = after.get("io_counters")
    before_io = before_io if isinstance(before_io, dict) else {}
    after_io = after_io if isinstance(after_io, dict) else {}
    io_deltas = {
        field: _counter_delta(
            before_io.get(field), after_io.get(field),
        )
        for field in PROCESS_IO_FIELDS
    }
    io_status = (
        "measured" if all(value is not None for value in io_deltas.values())
        else "partial" if any(value is not None for value in io_deltas.values())
        else "unavailable"
    )
    io_reason_code = after.get("io_reason_code") or before.get("io_reason_code")
    if (not isinstance(io_reason_code, str)
            or re.fullmatch(r"[A-Za-z0-9-]{1,64}", io_reason_code) is None):
        io_reason_code = None
    user_ticks = delta("cpu_user_ticks")
    system_ticks = delta("cpu_system_ticks")
    rss_before, rss_after = before.get("rss_bytes"), after.get("rss_bytes")
    hwm_before = before.get("rss_high_water_bytes")
    hwm_after = after.get("rss_high_water_bytes")
    hwm_growth = (
        hwm_after - hwm_before
        if (type(hwm_before) is int and type(hwm_after) is int
            and hwm_after >= hwm_before) else None
    )
    elapsed = (
        round(float(elapsed_seconds), 6)
        if type(elapsed_seconds) in (int, float)
        and 0 <= elapsed_seconds <= 86400 else None
    )
    core_counters = [user_ticks, system_ticks, *io_deltas.values()]
    rss_counters = [rss_before, rss_after, hwm_before, hwm_after]
    if all(value is not None for value in core_counters):
        status = "measured"
    elif any(value is not None for value in (*core_counters, *rss_counters)):
        status = "partial"
    else:
        return unavailable("process-counters-unavailable")
    return {
        "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
        "status": status,
        "measurement_basis": measurement_basis,
        "pid": pid,
        "process_start_ticks": start_ticks,
        "elapsed_seconds": elapsed,
        "cpu_user_ticks": user_ticks,
        "cpu_system_ticks": system_ticks,
        "rss_start_bytes": rss_before if type(rss_before) is int else None,
        "rss_end_bytes": rss_after if type(rss_after) is int else None,
        "rss_high_water_start_bytes": (
            hwm_before if type(hwm_before) is int else None
        ),
        "rss_high_water_end_bytes": (
            hwm_after if type(hwm_after) is int else None
        ),
        "rss_high_water_growth_bytes": hwm_growth,
        "io_bytes": {
            "status": io_status,
            "reason_code": io_reason_code,
            **io_deltas,
        },
        "measurement_limits": [
            "Counters cover the scheduler process and exclude spawned child processes.",
            "RSS values are boundary samples; high-water growth is a process-lifetime counter delta.",
            "rchar and wchar include transfers to pipes and sockets; read_bytes and write_bytes use kernel storage-I/O accounting.",
        ],
    }


def _counter_delta(
    before: Any, after: Any,
) -> int | None:
    if type(before) is not int or type(after) is not int:
        return None
    if after < before:
        return None
    return after - before


def compact_process_usage(value: Any) -> dict[str, Any] | None:
    """Validate and bound process deltas before they enter an ECI projection."""
    if (not isinstance(value, dict)
            or value.get("schema_version") != PROCESS_RESOURCE_USAGE_SCHEMA
            or value.get("status") not in {"measured", "partial", "unavailable"}
            or value.get("measurement_basis")
            not in PROCESS_RESOURCE_MEASUREMENT_BASES):
        return None
    status = value["status"]
    if status == "unavailable":
        reason = value.get("reason_code")
        return {
            "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": status,
            "measurement_basis": value["measurement_basis"],
            "reason_code": reason if isinstance(reason, str) else "unavailable",
        }
    pid, start_ticks = value.get("pid"), value.get("process_start_ticks")
    if (type(pid) is not int or pid <= 0 or type(start_ticks) is not int
            or start_ticks < 0):
        return None

    duration = value.get("elapsed_seconds")
    if (type(duration) not in (int, float) or not 0 <= duration <= 86400):
        return None
    nonnegative_fields = (
        "cpu_user_ticks", "cpu_system_ticks", "rss_start_bytes",
        "rss_end_bytes", "rss_high_water_start_bytes",
        "rss_high_water_end_bytes", "rss_high_water_growth_bytes",
    )
    counters = {
        field: _optional_nonnegative(value.get(field))
        for field in nonnegative_fields
    }
    if any(item is False for item in counters.values()):
        return None
    io_raw = value.get("io_bytes")
    if not isinstance(io_raw, dict) or io_raw.get("status") not in {
        "measured", "partial", "unavailable",
    }:
        return None
    io_reason_code = io_raw.get("reason_code")
    if (io_reason_code is not None
            and (not isinstance(io_reason_code, str)
                 or re.fullmatch(r"[A-Za-z0-9-]{1,64}", io_reason_code) is None)):
        return None
    io = {"status": io_raw["status"], "reason_code": io_reason_code}
    for field in PROCESS_IO_FIELDS:
        parsed = _optional_nonnegative(io_raw.get(field))
        if parsed is False:
            return None
        io[field] = parsed
    available_core = [counters["cpu_user_ticks"], counters["cpu_system_ticks"]]
    available_core.extend(io[field] for field in PROCESS_IO_FIELDS)
    available_count = sum(item is not None for item in available_core)
    expected_io_status = (
        "measured" if all(io[field] is not None for field in PROCESS_IO_FIELDS)
        else "partial" if any(io[field] is not None for field in PROCESS_IO_FIELDS)
        else "unavailable"
    )
    if io["status"] != expected_io_status:
        return None
    if (status == "measured" and available_count != len(available_core)):
        return None
    if status == "partial" and available_count == len(available_core):
        return None
    if status == "partial" and available_count == 0 and not any(
        counters[field] is not None for field in (
            "rss_start_bytes", "rss_end_bytes", "rss_high_water_start_bytes",
            "rss_high_water_end_bytes", "rss_high_water_growth_bytes",
        )
    ):
        return None
    limits = value.get("measurement_limits")
    if (not isinstance(limits, list) or len(limits) > 4
            or any(not isinstance(item, str) or len(item) > 256 for item in limits)):
        return None
    return {
        "schema_version": PROCESS_RESOURCE_USAGE_SCHEMA,
        "status": status,
        "measurement_basis": value["measurement_basis"],
        "pid": pid,
        "process_start_ticks": start_ticks,
        "elapsed_seconds": round(float(duration), 6),
        **counters,
        "io_bytes": io,
        "measurement_limits": list(limits),
    }


def _optional_nonnegative(value: Any) -> int | None | bool:
    return value if value is None or type(value) is int and value >= 0 else False


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
        stat_counters = _process_stat_counters(root)
        fields = _fields(root / "status")
        hwm = fields.get("VmHWM")
        executable, executable_sha256 = _executable_observation(root)
        result.update(name=fields["Name"].strip(), ppid=int(fields["PPid"]),
                      start_ticks=stat_counters["process_start_ticks"],
                      cpu_user_ticks=stat_counters["cpu_user_ticks"],
                      cpu_system_ticks=stat_counters["cpu_system_ticks"],
                      rss_bytes=int(fields.get("VmRSS", "0 kB").split()[0]) * 1024,
                      rss_hwm_bytes=(int(hwm.split()[0]) * 1024 if hwm else None),
                      executable=executable, executable_sha256=executable_sha256)
        io = _process_io(root)
        result["io_counters"] = {
            "status": io["status"],
            "reason_code": io["reason_code"],
            **io["counters"],
        }
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
        after = _process_stat_counters(root)["process_start_ticks"]
        if after != stat_counters["process_start_ticks"]:
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
