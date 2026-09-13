from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

SYNC_PATH_MARKERS = (
    "dropbox",
    "google drive",
    "googledrive",
    "icloud drive",
    "icloud~",
    "onedrive",
)

# Filesystems in this set are known to provide remote or host-shared storage.
# SQLite WAL requires local filesystem locking semantics for an authority
# database, so deployment startup must reject them.
NETWORK_FILESYSTEM_TYPES = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "fuse.ceph",
        "fuse.glusterfs",
        "fuse.rclone",
        "fuse.sshfs",
        "gfs",
        "gfs2",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
        "sshfs",
    }
)

# Keep this list deliberately conservative.  An unlisted filesystem must not
# be treated as suitable for a hardened SQLite/WAL deployment.
LOCAL_FILESYSTEM_TYPES = frozenset(
    {
        "btrfs",
        "bcachefs",
        "exfat",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "hfs",
        "hfsplus",
        "jfs",
        "ntfs",
        "ramfs",
        "reiserfs",
        "tmpfs",
        "ubifs",
        "ufs",
        "vfat",
        "xfs",
        "zfs",
    }
)


def _read_mountinfo() -> str:
    return Path("/proc/self/mountinfo").read_text(encoding="utf-8")


def _unescape_mountinfo_field(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _mountinfo_entries(raw_mountinfo: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in raw_mountinfo.splitlines():
        before_separator, separator, after_separator = line.partition(" - ")
        if not separator:
            continue
        before_fields = before_separator.split()
        after_fields = after_separator.split()
        if len(before_fields) < 5 or not after_fields or not before_fields[0].isdigit():
            continue
        entries.append(
            {
                "filesystem_type": after_fields[0].casefold(),
                "device": before_fields[2],
                "mount_id": before_fields[0],
                "mountpoint": _unescape_mountinfo_field(before_fields[4]),
            }
        )
    return entries


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _nearest_existing_path(path: Path) -> Path | None:
    candidate = path
    while True:
        try:
            candidate.stat()
        except FileNotFoundError:
            if candidate == candidate.parent:
                return None
            candidate = candidate.parent
            continue
        except OSError:
            return None
        return candidate


def _classify_filesystem_type(filesystem_type: str) -> str:
    if filesystem_type in NETWORK_FILESYSTEM_TYPES:
        return "network"
    if filesystem_type in LOCAL_FILESYSTEM_TYPES:
        return "local"
    return "unknown"


def inspect_filesystem(path: Path) -> dict[str, Any]:
    """Classify the mounted filesystem serving *path* without mutating it.

    Linux mount metadata is authoritative when it is available.  The target is
    resolved first so a database below an existing symlink is matched against
    its actual mount.  Unsupported platforms, unreadable metadata, and
    unrecognized filesystem types remain ``unknown`` rather than safe.
    """

    candidate = Path(path).expanduser()
    try:
        resolved_path = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return {
            "path": str(candidate),
            "resolved_path": None,
            "classification": "unknown",
            "type": None,
            "filesystem_type": None,
            "mountpoint": None,
            "evidence": {"source": "path-resolution", "reason": type(exc).__name__},
        }

    try:
        entries = _mountinfo_entries(_read_mountinfo())
    except OSError as exc:
        return {
            "path": str(candidate),
            "resolved_path": str(resolved_path),
            "classification": "unknown",
            "type": None,
            "filesystem_type": None,
            "mountpoint": None,
            "evidence": {"source": "/proc/self/mountinfo", "reason": type(exc).__name__},
        }

    matches: list[tuple[Path, dict[str, str]]] = []
    for entry in entries:
        mountpoint = Path(entry["mountpoint"])
        try:
            resolved_mountpoint = mountpoint.resolve(strict=False)
        except OSError:
            continue
        if _path_is_within(resolved_path, resolved_mountpoint):
            matches.append((resolved_mountpoint, entry))
    if not matches:
        return {
            "path": str(candidate),
            "resolved_path": str(resolved_path),
            "classification": "unknown",
            "type": None,
            "filesystem_type": None,
            "mountpoint": None,
            "evidence": {"source": "/proc/self/mountinfo", "reason": "no-matching-mountpoint"},
        }

    deepest_depth = max(len(mountpoint.parts) for mountpoint, _ in matches)
    deepest_matches = [match for match in matches if len(match[0].parts) == deepest_depth]
    existing_path = _nearest_existing_path(resolved_path)
    selection = "latest-mount-id"
    if existing_path is not None:
        try:
            device = existing_path.stat().st_dev
            visible_device = f"{os.major(device)}:{os.minor(device)}"
        except OSError:
            visible_device = None
        if visible_device is not None:
            device_matches = [match for match in deepest_matches if match[1]["device"] == visible_device]
            if device_matches:
                deepest_matches = device_matches
                selection = "stat-device"
    mountpoint, entry = max(deepest_matches, key=lambda match: int(match[1]["mount_id"]))
    filesystem_type = entry["filesystem_type"]
    return {
        "path": str(candidate),
        "resolved_path": str(resolved_path),
        "classification": _classify_filesystem_type(filesystem_type),
        "type": filesystem_type,
        "filesystem_type": filesystem_type,
        "mountpoint": str(mountpoint),
        "evidence": {
            "source": "/proc/self/mountinfo",
            "mount_id": entry["mount_id"],
            "selection": selection,
        },
    }


def require_local_database(path: Path) -> dict[str, Any]:
    """Return filesystem evidence or reject a non-local authority database."""

    report = inspect_filesystem(path)
    if report["classification"] == "local":
        return report
    filesystem_type = report["filesystem_type"] or "unknown"
    raise ValueError(
        "AMB authority storage requires a known local filesystem; "
        f"detected {report['classification']} filesystem type {filesystem_type!r}. "
        "Use a local host volume for the SQLite/WAL database."
    )


def ensure_private_directory(path: Path, *, tighten_existing: bool = False) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and (tighten_existing or not existed):
        path.chmod(0o700)


def ensure_private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


def permission_report(path: Path, *, directory: bool) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "applicable": os.name == "posix",
            "private": True,
            "mode": None,
        }
    if os.name != "posix":
        return {
            "path": str(path),
            "exists": True,
            "applicable": False,
            "private": None,
            "mode": None,
        }
    mode = stat.S_IMODE(path.stat().st_mode)
    disallowed = mode & 0o077
    expected = 0o700 if directory else 0o600
    return {
        "path": str(path),
        "exists": True,
        "applicable": True,
        "private": disallowed == 0,
        "mode": f"{mode:04o}",
        "expected_mode": f"{expected:04o}",
    }


def path_storage_warnings(path: Path) -> list[str]:
    rendered = str(path.expanduser()).replace("\\", "/")
    lowered = rendered.lower()
    warnings: list[str] = []
    if rendered.startswith("//") or rendered.startswith("\\\\"):
        warnings.append("network-share-path")
    if any(marker in lowered for marker in SYNC_PATH_MARKERS):
        warnings.append("sync-folder-path")
    if lowered.startswith("/mnt/") and any(marker in lowered for marker in SYNC_PATH_MARKERS):
        warnings.append("wsl-mounted-sync-folder")
    return warnings
