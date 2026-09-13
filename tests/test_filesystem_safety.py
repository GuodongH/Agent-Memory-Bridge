from __future__ import annotations

from pathlib import Path

import pytest

from agent_mem_bridge import filesystem_safety
from agent_mem_bridge.filesystem_safety import inspect_filesystem, require_local_database
from agent_mem_bridge.storage import MemoryStore


def _mountinfo_line(
    mountpoint: Path,
    filesystem_type: str,
    *,
    mount_id: int = 50,
    device: str = "0:43",
) -> str:
    rendered_mountpoint = str(mountpoint).replace("\\", "\\134").replace(" ", "\\040")
    return f"{mount_id} 25 {device} / {rendered_mountpoint} rw,relatime - {filesystem_type} fixture rw"


def test_inspect_filesystem_classifies_local_mount_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_mount = tmp_path / "local volume"
    local_mount.mkdir()
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: _mountinfo_line(local_mount, "ext4"),
    )

    report = inspect_filesystem(local_mount / "new" / "bridge.db")

    assert report["classification"] == "local"
    assert report["filesystem_type"] == "ext4"
    assert report["mountpoint"] == str(local_mount)
    assert report["evidence"] == {
        "source": "/proc/self/mountinfo",
        "mount_id": "50",
        "selection": "latest-mount-id",
    }


@pytest.mark.parametrize("filesystem_type", ["nfs", "nfs4", "cifs", "smb3", "smbfs", "sshfs", "fuse.sshfs"])
def test_known_network_filesystems_are_rejected_before_database_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filesystem_type: str,
) -> None:
    remote_mount = tmp_path / "innocent-name"
    remote_mount.mkdir()
    db_path = remote_mount / "new" / "bridge.db"
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: _mountinfo_line(remote_mount, filesystem_type),
    )

    with pytest.raises(ValueError, match="known local filesystem"):
        MemoryStore(db_path, require_local_filesystem=True)

    assert not db_path.parent.exists()


def test_deepest_mount_and_existing_symlink_ancestor_win(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    remote_mount = tmp_path / "remote"
    nested_mount = remote_mount / "nested"
    remote_mount.mkdir()
    nested_mount.mkdir()
    symlink = tmp_path / "authority-link"
    symlink.symlink_to(remote_mount, target_is_directory=True)
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: "\n".join(
            (
                _mountinfo_line(tmp_path, "ext4", mount_id=10),
                _mountinfo_line(remote_mount, "nfs4", mount_id=20),
                _mountinfo_line(nested_mount, "cifs", mount_id=30),
            )
        ),
    )

    report = inspect_filesystem(symlink / "nested" / "new" / "bridge.db")

    assert report["classification"] == "network"
    assert report["filesystem_type"] == "cifs"
    assert report["mountpoint"] == str(nested_mount)


def test_unknown_filesystem_and_unavailable_mountinfo_fail_strict_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "unknown" / "bridge.db"
    monkeypatch.setattr(filesystem_safety, "_read_mountinfo", lambda: _mountinfo_line(tmp_path, "fuse.custom"))

    report = inspect_filesystem(db_path)

    assert report["classification"] == "unknown"
    with pytest.raises(ValueError, match="detected unknown filesystem type 'fuse.custom'"):
        require_local_database(db_path)
    assert not db_path.parent.exists()

    def unavailable_mountinfo() -> str:
        raise OSError("fixture metadata unavailable")

    monkeypatch.setattr(filesystem_safety, "_read_mountinfo", unavailable_mountinfo)
    assert inspect_filesystem(db_path)["classification"] == "unknown"


def test_symlink_loop_is_unknown_instead_of_raising(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)

    report = inspect_filesystem(loop / "bridge.db")

    assert report["classification"] == "unknown"
    assert report["evidence"] == {"source": "path-resolution", "reason": "SymlinkLoop"}


def test_missing_posix_device_helpers_keep_network_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote_mount = tmp_path / "remote"
    remote_mount.mkdir()
    monkeypatch.setattr(filesystem_safety, "_stat_device_id", lambda _device: None)
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: _mountinfo_line(remote_mount, "nfs4"),
    )

    report = inspect_filesystem(remote_mount / "new" / "bridge.db")

    assert report["classification"] == "network"
    assert report["filesystem_type"] == "nfs4"
    assert report["evidence"]["selection"] == "latest-mount-id"


def test_visible_overmount_device_wins_over_hidden_local_mount(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mountpoint = tmp_path / "mounted"
    mountpoint.mkdir()
    visible_device_name = filesystem_safety._stat_device_id(mountpoint.stat().st_dev) or "9:99"
    monkeypatch.setattr(filesystem_safety, "_stat_device_id", lambda _device: visible_device_name)
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: "\n".join(
            (
                _mountinfo_line(mountpoint, "ext4", mount_id=10, device="8:1"),
                _mountinfo_line(mountpoint, "nfs4", mount_id=20, device=visible_device_name),
            )
        ),
    )

    report = inspect_filesystem(mountpoint / "new" / "bridge.db")

    assert report["classification"] == "network"
    assert report["filesystem_type"] == "nfs4"
    assert report["evidence"]["selection"] == "stat-device"


def test_from_env_strict_check_happens_before_database_initialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "isolated-amb-home"
    db_path = home / "nested" / "bridge.db"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_HOME", str(home))
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_DB_PATH", str(db_path))
    monkeypatch.setattr(
        filesystem_safety,
        "_read_mountinfo",
        lambda: _mountinfo_line(tmp_path, "nfs"),
    )

    with pytest.raises(ValueError, match="detected network filesystem type 'nfs'"):
        MemoryStore.from_env(require_local_filesystem=True)

    assert not db_path.parent.exists()


def test_hardened_local_constructor_rejects_known_network_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "network" / "bridge.db"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_OPERATING_PROFILE", "hardened-local")
    monkeypatch.setattr(filesystem_safety, "_read_mountinfo", lambda: _mountinfo_line(tmp_path, "nfs4"))

    with pytest.raises(ValueError, match="detected network filesystem type 'nfs4'"):
        MemoryStore(db_path)

    assert not db_path.parent.exists()


def test_hardened_local_constructor_keeps_unknown_filesystem_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "unknown" / "bridge.db"
    monkeypatch.setenv("AGENT_MEMORY_BRIDGE_OPERATING_PROFILE", "hardened-local")
    monkeypatch.setattr(filesystem_safety, "_read_mountinfo", lambda: _mountinfo_line(tmp_path, "fuse.custom"))

    store = MemoryStore(db_path)

    assert store.db_path.is_file()
