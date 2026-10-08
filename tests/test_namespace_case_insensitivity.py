from __future__ import annotations

from pathlib import Path
import pytest

from agent_mem_bridge.storage import MemoryStore


def test_recall_and_stats_case_insensitive(tmp_path: Path) -> None:
    home = tmp_path / "amb-home"
    home.mkdir()
    store = MemoryStore(home / "bridge.db", log_dir=home / "logs")

    # Store memory under mixed-case namespace
    stored = store.store(
        namespace="project:Moebius",
        content="Important trading rule for Moebius",
        title="Moebius rule",
        kind="memory",
    )
    assert stored["id"] is not None

    # Recall using lowercase namespace
    res_lower = store.recall(
        namespace="project:moebius",
        query="trading rule",
        kind="memory",
    )
    assert res_lower["count"] == 1
    assert res_lower["items"][0]["id"] == stored["id"]

    # Recall using uppercase namespace
    res_upper = store.recall(
        namespace="project:MOEBIUS",
        query="trading rule",
        kind="memory",
    )
    assert res_upper["count"] == 1
    assert res_upper["items"][0]["id"] == stored["id"]

    # Browse using lowercase namespace
    browse_lower = store.browse(
        namespace="project:moebius",
        limit=5,
    )
    assert browse_lower["count"] == 1
    assert browse_lower["items"][0]["id"] == stored["id"]

    # Stats using lowercase namespace
    stats_lower = store.stats("project:moebius")
    assert stats_lower["total_count"] == 1
    assert stats_lower["kind_counts"]["memory"] == 1
