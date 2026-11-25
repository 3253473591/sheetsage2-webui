"""`tools/install_rocm.py` 的纯逻辑单测（不下载、不装、不建环境）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_rocm", ROOT / "tools" / "install_rocm.py")
install_rocm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_rocm)


def test_wheels_are_the_three_packages_plus_runtime():
    names = [name for name, _ in install_rocm.WHEELS]
    assert any(name.startswith("torch-2.9.1") for name in names)
    assert any(name.startswith("torchaudio-2.9.1") for name in names)
    assert any(name.startswith("rocm_sdk_core") for name in names)
    assert any(name.startswith("rocm_sdk_libraries_custom") for name in names)
    # 版本号必须来自同一个 ROCm 发布，否则运行时和 torch 对不上
    assert all(install_rocm.ROCM_VERSION in name for name in names)


def test_wheel_urls_point_at_the_radeon_index():
    for name, _ in install_rocm.WHEELS:
        url = install_rocm.wheel_url(name)
        assert url.startswith("https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/")
        assert url.endswith(name)
    # torch 的 `+` 必须保持 URL 编码（AMD 目录里就是这么写的，直接拼 `+` 会 404）
    torch_url = install_rocm.wheel_url("torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl")
    assert "%2B" in torch_url
    # 但落到本地要解码：pip 靠文件名认 wheel，`%2B` 会认不出来
    local = install_rocm.wheel_path("torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl")
    assert local.name == "torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl"


def test_install_order_runtime_before_torch(tmp_path):
    venv = tmp_path / ".venv-rocm"
    wheels = [tmp_path / name for name, _ in install_rocm.WHEELS]
    commands = install_rocm.install_commands(venv, wheels)
    assert len(commands) == 3
    assert "uninstall" in commands[0]
    # 第一件事是把 CUDA 版卸干净（不卸的话装不进去，还会把后端搞乱）
    assert commands[0][-2:] == ["torch", "torchaudio"]
    # 运行时包必须排在 torch 前面
    assert any("rocm_sdk_core" in part for part in commands[1])
    assert any("torch-2.9.1" in part for part in commands[2])
    # 一律 `python -m pip`：复制来的 pip.exe 里写的是老路径
    assert commands[0][1:4] == ["-m", "pip", "uninstall"]
    assert all(command[1:3] == ["-m", "pip"] for command in commands)


def test_install_commands_all_pass_no_deps(tmp_path):
    """`--no-deps`：torch 的普通依赖复制过来的环境里已经有了，让 pip 重新解析只会引入风险。"""
    commands = install_rocm.install_commands(tmp_path / ".venv-rocm",
                                             [tmp_path / name for name, _ in install_rocm.WHEELS])
    assert "--no-deps" in commands[1]
    assert "--no-deps" in commands[2]
    assert "--no-deps" not in commands[0]


def test_marker_write_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(install_rocm, "ROOT", tmp_path)
    install_rocm.gpu_backend.write_backend_marker(tmp_path, "rocm")
    assert (tmp_path / ".env-backend").read_text(encoding="utf-8").strip() == "rocm"
