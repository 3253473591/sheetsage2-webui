"""启动环境检测（任务文档 5.1 / 组件 C26 / 显存 5.2）。

设计要点
--------
**Web 服务进程绝不 import torch。** 原因有二：

1. 一旦在服务进程里初始化 CUDA 上下文，就会常驻占用显存，导致文档 C21「显存条」
   显示的「已用」把服务自身算进去，读数失真；
2. torch 导入本身要 1–2 秒，会拖慢服务启动。

因此 torch / CUDA 探测一律交给**短命子进程**；显存快照走 ``nvidia-smi``。

每个检测项产出 ``{id,label,status,value,detail,fix,command}``：

* ``status``：``ok`` 绿勾 / ``warn`` 黄叹号 / ``fail`` 红叉；
* ``fix``：修复建议（中文）；
* ``command``：可一键复制的修复命令（没有则 None）。

``blocking=True`` 的项失败时禁止开始任务（文档 5.1：不满足关键条件时禁止开跑）。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

from app import config

__all__ = ["gpu_snapshot", "run_checks", "port_in_use", "probe_torch", "is_ready", "blocking_failures"]


# --------------------------------------------------------------------------
# 显存快照（廉价，供 C21 显存条轮询）
# --------------------------------------------------------------------------
def gpu_snapshot() -> dict[str, Any]:
    """用 nvidia-smi 读取当前显存，**不 import torch**。

    Returns:
        ``{"available":bool, "name":str, "total_mib":float, "used_mib":float,
        "free_mib":float, "used_ratio":float, "error":str|None}``
    """
    empty = {
        "available": False,
        "name": None,
        "total_mib": None,
        "used_mib": None,
        "free_mib": None,
        "used_ratio": None,
        "error": None,
    }
    exe = shutil.which("nvidia-smi") or r"C:\WINDOWS\system32\nvidia-smi.exe"
    if not exe or not Path(exe).exists():
        empty["error"] = "未找到 nvidia-smi"
        return empty
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        empty["error"] = f"nvidia-smi 执行失败：{exc}"
        return empty
    line = (out.stdout or "").strip().splitlines()
    if not line:
        empty["error"] = "nvidia-smi 无输出"
        return empty
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 4:
        empty["error"] = f"nvidia-smi 输出无法解析：{line[0]!r}"
        return empty
    try:
        total, used, free = float(parts[1]), float(parts[2]), float(parts[3])
    except ValueError:
        empty["error"] = f"nvidia-smi 数值无法解析：{line[0]!r}"
        return empty
    return {
        "available": True,
        "name": parts[0],
        "total_mib": total,
        "used_mib": used,
        "free_mib": free,
        "used_ratio": (used / total) if total else None,
        "error": None,
    }


# --------------------------------------------------------------------------
# torch / CUDA 探测（子进程，避免服务进程持有 CUDA 上下文）
# --------------------------------------------------------------------------
_TORCH_PROBE = r"""
import json
res = {"torch": None, "cuda": False, "cuda_version": None, "device_count": 0,
       "devices": [], "error": None}
try:
    import torch
    res["torch"] = torch.__version__
    res["cuda"] = bool(torch.cuda.is_available())
    res["cuda_version"] = torch.version.cuda
    res["device_count"] = torch.cuda.device_count()
    for i in range(res["device_count"]):
        p = torch.cuda.get_device_properties(i)
        res["devices"].append({
            "index": i,
            "name": p.name,
            "total_mib": round(p.total_memory / 1024 / 1024, 1),
            "capability": f"{p.major}.{p.minor}",
        })
except Exception as exc:
    res["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(res, ensure_ascii=False))
"""


def probe_torch(timeout: float = 90.0) -> dict[str, Any]:
    """在子进程里探测 torch / CUDA，返回结构化结果。"""
    try:
        out = subprocess.run(
            [sys.executable, "-c", _TORCH_PROBE],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"torch": None, "cuda": False, "error": f"探测子进程失败：{exc}", "devices": []}
    for line in reversed((out.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {
        "torch": None,
        "cuda": False,
        "error": f"探测无有效输出；stderr={(out.stderr or '')[-300:]}",
        "devices": [],
    }


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def port_in_use(port: int, host: str = config.HOST) -> bool:
    """检查端口是否被占用（文档 5.1 最后一项）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


# --------------------------------------------------------------------------
# 单项结果构造
# --------------------------------------------------------------------------
def _item(
    cid: str,
    label: str,
    status: str,
    *,
    value: str | None = None,
    detail: str | None = None,
    fix: str | None = None,
    command: str | None = None,
    blocking: bool = False,
) -> dict[str, Any]:
    return {
        "id": cid,
        "label": label,
        "status": status,
        "value": value,
        "detail": detail,
        "fix": fix,
        "command": command,
        "blocking": blocking,
    }


def _check_python() -> dict[str, Any]:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if (sys.version_info.major, sys.version_info.minor) == (3, 12):
        return _item("python", "Python 版本", "ok", value=ver, detail="与任务文档要求一致")
    if sys.version_info[:2] >= (3, 10):
        return _item(
            "python",
            "Python 版本",
            "warn",
            value=ver,
            detail="任务文档要求 3.12；当前版本可运行，但未在文档标注范围内验证",
            fix="整合包自带 runtime\\python（Python 3.12.11），用 start.bat 启动即可",
        )
    return _item(
        "python",
        "Python 版本",
        "fail",
        value=ver,
        detail="低于 3.10，SheetSage2 依赖无法加载",
        fix="整合包自带 runtime\\python（Python 3.12.11），用 start.bat 启动即可",
        blocking=True,
    )


def _check_torch(probe: dict[str, Any]) -> dict[str, Any]:
    if probe.get("torch"):
        return _item("torch", "PyTorch", "ok", value=probe["torch"], detail="已就绪")
    return _item(
        "torch",
        "PyTorch",
        "fail",
        detail=probe.get("error") or "无法导入 torch",
        fix=(
            "整合包自带运行时，正常不会缺。若确实缺失，在包根目录用自带的 uv 重装：\n"
            r"runtime\uv.exe pip install --python runtime\python\python.exe "
            r"torch==2.10.0+cu130 torchaudio==2.10.0+cu130 "
            r"--index-url https://download.pytorch.org/whl/cu130"
        ),
        blocking=True,
    )


def _check_cuda(probe: dict[str, Any]) -> dict[str, Any]:
    """CUDA 检测。

    **没有 N 卡不是错误**：整合包会自动退回 CPU 推理（``app/worker.py::pick_device``），
    只是慢很多。所以这里给 ``warn`` 而不是 ``fail``，并把「会走 CPU」讲明白，
    免得测试者以为环境坏了。
    """
    if probe.get("cuda"):
        return _item("cuda", "CUDA 可用性", "ok", value=probe.get("cuda_version") or "可用")
    err = probe.get("error")
    return _item(
        "cuda",
        "CUDA 可用性",
        "warn",
        value="不可用（将用 CPU）",
        detail=(f"torch 报错：{err}" if err else "torch.cuda.is_available() 为 False")
        + "。不影响使用：会自动改用 CPU 推理，但一首 5 分钟的歌可能要十几分钟以上。",
        fix="想用显卡：装 NVIDIA 驱动（越新越好），再重开本程序；只想出结果：直接跑，慢就慢点。",
    )


def _check_gpu(snap: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not snap.get("available"):
        items.append(
            _item(
                "gpu",
                "GPU 与显存",
                "warn",
                value="未知",
                detail=snap.get("error") or "nvidia-smi 不可用",
                fix="确认已安装 NVIDIA 驱动",
            )
        )
        return items
    items.append(
        _item(
            "gpu",
            "GPU 与显存",
            "ok",
            value=snap["name"],
            detail=f"总 {snap['total_mib']:.0f} MiB · 已用 {snap['used_mib']:.0f} MiB · 空闲 {snap['free_mib']:.0f} MiB",
        )
    )
    free = snap.get("free_mib") or 0
    if free < config.MIN_FREE_GPU_MIB:
        items.append(
            _item(
                "gpu_free",
                "空闲显存",
                "warn",
                value=f"{free:.0f} MiB",
                detail=f"低于阈值 {config.MIN_FREE_GPU_MIB} MiB，可能 OOM",
                fix="关闭其他占用显存的程序，或在「计算精度」选择 BF16 快速、缩短分析时长",
            )
        )
    else:
        items.append(_item("gpu_free", "空闲显存", "ok", value=f"{free:.0f} MiB", detail="满足推理需要"))
    return items


def _check_ffmpeg() -> dict[str, Any]:
    """检查 ffmpeg —— 而且必须检查**推理链路真正会用的那一个**。

    踩过的坑：这个检查原来只看 ``config.ffmpeg_path()``（能找到包内 ffmpeg → 绿勾），
    但 SheetSage2 的 vendored 代码只用 ``shutil.which("ffmpeg")``，
    于是面板说「ffmpeg 正常」、一提交任务却报
    ``RuntimeError: FFmpeg is required to read audio files``。
    现在以 **PATH 可见性**为准，和推理链路保持一致。
    """
    config.ensure_ffmpeg_on_path()
    resolved = config.ffmpeg_path()
    on_path = shutil.which("ffmpeg")

    if not on_path:
        return _item(
            "ffmpeg",
            "ffmpeg",
            "fail",
            value="推理链路不可见",
            detail=(
                f"已解析到 {resolved}，但它不在 PATH 上" if resolved
                else "既没在包内找到 runtime\\ffmpeg\\bin\\ffmpeg.exe，PATH 上也没有 ffmpeg"
            )
            + "；SheetSage2 的解码代码只认 PATH，所以任务会失败",
            fix=(
                "全量包：确认 runtime\\ffmpeg\\bin\\ffmpeg.exe 没被杀毒软件删掉，"
                "或重新解压；也可以设环境变量 FFMPEG_BIN 指向 ffmpeg.exe"
            ),
            blocking=True,
        )

    version = ""
    try:
        out = subprocess.run(
            [on_path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        version = (out.stdout or "").splitlines()[0][:80] if out.stdout else ""
    except (OSError, subprocess.SubprocessError):
        pass

    bundled = str(config.RUNTIME_DIR / "ffmpeg" / "bin")
    origin = "随包" if bundled.lower() in str(on_path).lower() else "系统 PATH"
    return _item(
        "ffmpeg",
        "ffmpeg",
        "ok",
        value=version or on_path,
        detail=f"{on_path}（来源：{origin}；推理链路可直接调用）",
    )


def _check_demucs() -> dict[str, Any]:
    if _module_available("demucs"):
        return _item("demucs", "Demucs / UVR 人声分离", "ok", value="可用", detail="如需前置分离可启用")
    return _item(
        "demucs",
        "Demucs / UVR 人声分离",
        "warn",
        value="未安装",
        detail="不影响主流程：SheetSage2 原生完成人声处理，5–30% 阶段会标注「跳过」",
    )


def _check_runtime() -> dict[str, Any]:
    """整合包便携运行时（随包的 Python）是否存在。

    开发机上跑的是 ``.venv``，没有 ``runtime/python``，此时只记 warn，不算失败。
    """
    bundled = config.RUNTIME_DIR / "python" / "python.exe"
    if bundled.is_file():
        return _item("runtime", "便携运行时", "ok", value="已随包提供", detail=str(bundled))
    venv = config.ROOT / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return _item("runtime", "便携运行时", "ok", value="开发机 .venv", detail=str(venv))
    return _item(
        "runtime",
        "便携运行时",
        "warn",
        value="未找到",
        detail="没有 runtime/python，也没有 .venv；会退回到系统 PATH 里的 python",
        fix=r"运行整合包根目录的「安装依赖.bat」（轻量包）或重新解压全量整合包",
    )


def _check_dir_weights(
    cid: str,
    label: str,
    directory: Path,
    *,
    required_files: tuple[str, ...] = ("model.safetensors", "config.json"),
    blocking: bool,
) -> dict[str, Any]:
    env_hint = {
        "sheetsage2": "SHEETSAGE2_MODEL_DIR",
        "mert": "MERT_MODEL_DIR",
    }.get(cid, "环境变量")
    if not directory.exists():
        return _item(
            cid,
            label,
            "fail",
            value="缺失",
            detail=f"目录不存在：{directory}",
            fix=(
                f"把权重目录放到整合包的 models\\ 下，或用环境变量 {env_hint} 指向它。"
                "（轻量包用户：先跑「安装依赖.bat」下载权重）"
            ),
            blocking=blocking,
        )
    missing = [f for f in required_files if not (directory / f).exists()]
    if missing:
        return _item(
            cid,
            label,
            "fail",
            value="不完整",
            detail=f"{directory} 缺少：{', '.join(missing)}",
            fix="重新补全权重文件（LFS 指针文件不算，必须是真实权重）",
            blocking=blocking,
        )
    size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    return _item(
        cid,
        label,
        "ok",
        value=f"{size / 1024 / 1024:.0f} MiB",
        detail=str(directory),
    )


def _check_svp_reference() -> dict[str, Any]:
    d = config.SVP_REFERENCE_DIR
    if not d.exists():
        return _item(
            "svp_ref",
            "SVP 参考目录",
            "warn",
            value="不可读",
            detail=f"{d} 不存在；SVP 写出不依赖它，仅影响样本校验",
        )
    try:
        count = sum(1 for _ in d.rglob("*.svp"))
    except OSError as exc:
        return _item("svp_ref", "SVP 参考目录", "warn", value="读取失败", detail=str(exc))
    return _item("svp_ref", "SVP 参考目录", "ok", value=f"{count} 个 .svp 样本", detail=str(d))


def _check_port(port: int, own_port: int | None = None) -> dict[str, Any]:
    # 服务自身就在监听这个端口。端口检测的本意是「启动前有没有别人占着」，
    # 一旦服务跑起来，这个端口**必然**被占用——若仍判为失败并作为阻塞项，
    # 服务会把自己锁死、永远无法开始任何任务。故显式识别自身端口。
    if own_port is not None and int(port) == int(own_port):
        return _item(
            "port",
            f"Web 端口 {port}",
            "ok",
            value="服务正在监听",
            detail=f"当前服务已绑定 {config.HOST}:{port}",
        )
    if port_in_use(port):
        if own_port is not None:
            # 服务**已经在** own_port 上跑起来了 —— 那「能不能启动服务」这个问题早就
            # 有答案了，settings 里这个端口只影响「下次启动」，不该拦住本次任务。
            # 实测踩过：按说明书用 `start.bat --port 8888` 换端口启动，
            # 因为 settings.json 里的 8777 被自己另一个实例（或别的程序）占着，
            # 环境检测直接判 blocking、拒绝开始扒谱，而服务其实好好的。
            return _item(
                "port",
                f"Web 端口 {port}",
                "warn",
                value="被占用（不影响本次）",
                detail=(
                    f"当前服务实际监听 {config.HOST}:{own_port}；"
                    f"{port} 是 settings.json 里写的端口，只在**下次启动**时才用到"
                ),
                fix=f"下次启动若报端口冲突，把 settings.json 的 port 改掉，或关掉占用 {port} 的程序",
            )
        return _item(
            "port",
            f"Web 端口 {port}",
            "fail",
            value="被占用",
            detail="无法在此端口启动服务",
            fix="在「设置」里换一个端口，或关掉占用该端口的程序",
            command=f"netstat -ano | findstr :{port}",
            blocking=True,
        )
    return _item("port", f"Web 端口 {port}", "ok", value="空闲", detail=f"将监听 {config.HOST}:{port}")


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def run_checks(
    port: int | None = None,
    *,
    include_torch: bool = True,
    own_port: int | None = None,
) -> dict[str, Any]:
    """执行全部环境检测（文档 5.1），返回面板所需结构与汇总。

    Args:
        port: 要检测的端口；缺省取设置里的值。
        include_torch: 是否跑 torch/CUDA 子进程探测（较重，约 2s）。
        own_port: **当前服务自身正在监听的端口**。传入后该端口不再被判为「被占用」。
    """
    settings = config.load_settings()
    port = port or int(settings.get("port") or config.DEFAULT_PORT)

    probe = probe_torch() if include_torch else {}
    snap = gpu_snapshot()

    checks: list[dict[str, Any]] = [_check_runtime(), _check_python()]
    checks.append(_check_torch(probe) if include_torch else _item("torch", "PyTorch", "warn", value="未检测"))
    checks.append(_check_cuda(probe) if include_torch else _item("cuda", "CUDA 可用性", "warn", value="未检测"))
    checks.extend(_check_gpu(snap))
    checks.append(_check_ffmpeg())
    checks.append(_check_demucs())
    checks.append(
        _check_dir_weights(
            "sheetsage2",
            "SheetSage2 权重与代码",
            config.SHEETSAGE2_DIR,
            blocking=True,
        )
    )
    checks.append(
        _check_dir_weights(
            "mert",
            "MERT-v2-FullSong 主干",
            config.MERT_FULLSONG_DIR,
            required_files=("model.safetensors",),
            blocking=True,
        )
    )
    checks.append(_check_svp_reference())
    checks.append(_check_port(port, own_port))

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    blocking = [c for c in fails if c["blocking"]]

    return {
        "checks": checks,
        "summary": {
            "ok": len([c for c in checks if c["status"] == "ok"]),
            "warn": len(warns),
            "fail": len(fails),
            "total": len(checks),
        },
        "ready": not blocking,
        "blocking": blocking,
        "gpu": snap,
        "torch": {k: v for k, v in probe.items() if k != "devices"} if include_torch else None,
        "devices": probe.get("devices") or [],
        "host": config.HOST,
        "port": port,
    }


def is_ready() -> bool:
    """只判断关键条件是否满足（供提交任务前快速校验）。"""
    return run_checks()["ready"]


def blocking_failures() -> list[dict[str, Any]]:
    """返回阻塞任务的关键失败项，供 API 直接给出明确提示。"""
    return run_checks()["blocking"]
