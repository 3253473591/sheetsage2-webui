"""单实例锁（G1）：同一个数据目录只许一个服务。

**不真起服务**：只测 `InstanceLock` 本身（加锁、名片、释放、残留锁）。
真正的双击验证在 `webui.bat` 上手工做一次即可——那是 shell 行为，不适合放进 pytest。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sheetsage2_webui.webui.singleton import (  # noqa: E402
    AlreadyRunning,
    InstanceLock,
    pid_alive,
    read_info_file,
)


def test_second_instance_on_the_same_data_root_is_rejected(tmp_path):
    first = InstanceLock(tmp_path)
    info = first.acquire(port=8123)
    try:
        assert info["port"] == 8123
        assert info["url"] == "http://127.0.0.1:8123"

        second = InstanceLock(tmp_path)
        with pytest.raises(AlreadyRunning) as caught:
            second.acquire(port=9999)
        assert caught.value.info["port"] == 8123        # 报的是"已经在跑的那个"的地址
        assert caught.value.info["url"] == "http://127.0.0.1:8123"
        assert second.try_acquire() is False
    finally:
        first.release()

    # 释放之后（= 服务退出）下一个实例可以正常拿到锁
    third = InstanceLock(tmp_path)
    assert third.try_acquire() is True
    third.release()


def test_lock_info_is_readable_by_the_second_process(tmp_path):
    """名片（`.webui.port.json`）必须能被另一个进程读到——锁住的那个文件读不了。"""
    first = InstanceLock(tmp_path)
    first.acquire()
    try:
        first.update(port=8124, url="http://127.0.0.1:8124")
        info = json.loads(first.info_path.read_text(encoding="utf-8"))
        assert info["port"] == 8124
        other = InstanceLock(tmp_path)
        assert other.read_info()["url"] == "http://127.0.0.1:8124"
    finally:
        first.release()


def test_update_keeps_the_first_started_at(tmp_path):
    lock = InstanceLock(tmp_path)
    try:
        started = lock.acquire(port=8000)["started_at"]
        again = lock.update(port=8001)
        assert again["started_at"] == started
        assert again["port"] == 8001
        assert again["url"] == "http://127.0.0.1:8001"
    finally:
        lock.release()


def test_a_leftover_lock_file_from_a_dead_process_does_not_block(tmp_path):
    """进程被强杀之后锁**自动**释放（锁跟着进程走），残留文件不该挡住下一次启动。"""
    (tmp_path / ".webui.lock").write_text("garbage", encoding="utf-8")
    lock = InstanceLock(tmp_path)
    assert lock.try_acquire(port=8000) is True
    lock.release()
    # 名片里是这一轮的信息
    assert read_info_file(tmp_path / ".webui.port.json")["port"] == 8000


def test_info_file_survives_garbage(tmp_path):
    (tmp_path / ".webui.port.json").write_text("{not json", encoding="utf-8")
    assert read_info_file(tmp_path / ".webui.port.json") == {}
    assert read_info_file(tmp_path / "nope.json") == {}


def test_pid_alive_agrees_with_the_running_process():
    import os

    assert pid_alive(os.getpid()) is True
    assert pid_alive(None) is False
    assert pid_alive(999_999_999) is False
