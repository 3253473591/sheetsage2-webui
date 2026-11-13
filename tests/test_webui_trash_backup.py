"""回收站：软删除、恢复、彻底删除、保留策略（7 天 / 2GB / 定时清理）。

删除按钮就在「下载 ▾」旁边，所以"删了还能不能救回来"是产品行为，不是实现细节。
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from support_webui import (  # noqa: E402
    WAV_BYTES,
    fake_runners,
    make_client,
    job_dir,
    wait_for_state,
    upload,
)
from sheetsage2_webui.webui.jobs import (  # noqa: E402
    JobManager,
)


def _trash_entry(manager, name: str) -> Path:
    """按工程名找到它在 `.trash/` 里的目录（条目名带工程名和 id）。"""
    for entry in manager.trash_root.iterdir():
        if name in entry.name:
            return entry
    raise AssertionError(f"回收站里没有 {name}")


def _age(path: Path, days: float) -> None:
    """把目录的修改时间往前拨——`_purge_trash` 判断过期用的就是它。"""
    stamp = time.time() - days * 86400.0
    os.utime(path, (stamp, stamp))


def test_delete_goes_to_the_trash_and_can_be_restored(tmp_path):
    """G2：删除是**软删除**——进回收站，能恢复，产物和参数都回来。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        root = job_dir(manager, job)
        assert (root / "project.json").is_file()

        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404
        assert not root.exists()                            # 目录已经挪走

        entries = client.get("/api/trash").json()["entries"]
        assert len(entries) == 1
        assert entries[0]["name"] == job["name"]
        assert entries[0]["files"] > 0

        restored = client.post(f"/api/trash/{entries[0]['id']}/restore")
        assert restored.status_code == 200
        assert restored.json()["id"] == job["id"]
        assert job_dir(manager, job).is_dir()
        assert client.get(f"/api/jobs/{job['id']}/files/project.json").status_code == 200
        assert client.get("/api/trash").json()["entries"] == []


def test_delete_with_permanent_flag_skips_the_trash(tmp_path):
    """G2：用户确认"彻底删除"时才是真删（不进回收站）。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        response = client.delete(f"/api/jobs/{job['id']}", params={"permanent": "true"})
        assert response.status_code == 200
        assert client.get("/api/trash").json()["entries"] == []
        assert not job_dir(manager, job).exists()


def test_deleted_project_is_not_resurrected_by_trailing_writes(tmp_path):
    """删掉的工程不许被收尾线程写盘复活。

    `_finish` 先把 `state` 置成 `done` 才写 `manifest.json` / `job.json`，而
    `write_bytes_atomic` 会 `mkdir(parents=True)`。用户看到"完成"后立刻删除，
    收尾中的那次写盘就会把刚删掉的目录**重新建出来**（整仓测试间歇失败的根源）。
    """
    manager = JobManager(tmp_path / "data", runners=fake_runners())
    try:
        job = manager.create("歌.wav", io.BytesIO(WAV_BYTES))
        root = job.root
        assert manager.delete(job.id, permanent=True)
        assert not root.exists()

        manager._write_metadata(job)                 # 收尾线程此刻还在写
        manager.write_manifest(job)
        assert not root.exists()
    finally:
        manager.shutdown()


def test_trash_purge_keeps_entries_inside_the_age_window(tmp_path):
    """6 天前删的还在 7 天窗口里，不能被清掉。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, name="六天前.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        _age(_trash_entry(manager, "六天前"), 6)
        manager._purge_trash()                      # 等价于"重启一次服务"
        assert len(client.get("/api/trash").json()["entries"]) == 1


def test_trash_purge_drops_entries_older_than_seven_days(tmp_path):
    """8 天前删的会被真删——不用等七天，把 mtime 拨回去就行。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        job = upload(client, name="八天前.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        entry = _trash_entry(manager, "八天前")
        _age(entry, 8)
        manager._purge_trash()
        assert not entry.exists()
        assert client.get("/api/trash").json()["entries"] == []


def test_trash_purge_evicts_the_oldest_when_over_the_size_cap(tmp_path):
    """总量超上限就砍最旧的——把上限调小即可验，不用真堆 2GB。"""
    client, manager = make_client(tmp_path, fake_runners())
    with client:
        for name in ("旧的.wav", "新的.wav"):
            job = upload(client, name=name, plan="transcribe").json()
            wait_for_state(client, job["id"], {"done"})
            client.delete(f"/api/jobs/{job['id']}")
        old_entry = _trash_entry(manager, "旧的")
        new_entry = _trash_entry(manager, "新的")
        _age(old_entry, 1)
        _age(new_entry, 0)

        def size_of(path: Path) -> int:
            return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

        # 上限卡在"只装得下新的那一份"，于是砍掉旧的之后刚好不超
        manager.trash_max_bytes = size_of(new_entry) + 1
        manager._purge_trash()
        assert not old_entry.exists()               # 最旧的先被砍
        assert new_entry.exists()                   # 剩下的那份还在窗口里
        assert [row["name"] for row in client.get("/api/trash").json()["entries"]] == [
            "新的"]


def test_trash_purge_also_runs_on_a_timer(tmp_path):
    """不重启、也不再删东西：后台定时器到点就会清掉过期条目。

    没有这条定时的话，「7 天自动清」只在启动和删除时生效——服务连开一周而用户
    一个工程都没删，过期条目会一直躺在 `.trash/` 里。
    """
    client, manager = make_client(tmp_path, fake_runners(), trash_purge_interval=0.05)
    with client:
        job = upload(client, name="定时清理.wav", plan="transcribe").json()
        wait_for_state(client, job["id"], {"done"})
        client.delete(f"/api/jobs/{job['id']}")
        entry = _trash_entry(manager, "定时清理")
        _age(entry, 8)                              # 已经过期，但没有触发任何删除
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and entry.exists():
            time.sleep(0.05)
        assert not entry.exists()
