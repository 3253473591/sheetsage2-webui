"""启动器的实体（`webui.bat` 只留 ASCII，所有中文都在这里）。

为什么这么分：**cmd.exe 处理不了「UTF-8 的 .bat」**——`chcp 65001` 之后它仍按字节偏移重读
批处理文件，多字节字符会把行切断，尾巴被当成命令执行（实测报
`'bf16' is not recognized as an internal or external command`）。
所以 .bat 里一个非 ASCII 字符都不能有，中文交给 Python 打印
（Python 在 Windows 控制台上走 `WriteConsoleW`，任何代码页都不会乱码）。

    webui.bat              -> python scripts/launch.py
    webui.bat 8010 2       -> python scripts/launch.py 8010 2

做四件事：自检环境 → 端口可用性检查（**避开被别人占掉的端口**）→ 选模式 → 起服务 + 开浏览器。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
STATIC = ROOT / "src" / "sheetsage2_webui" / "webui" / "static" / "index.html"
DEFAULT_PORT = 8000

#: 模式 -> (说明, 透传给服务的参数)
#:
#: 只留两个：**默认**（页面上自己选参数）和 **4G 特供**（服务端把显存预算和参数一起定死）。
#: 原来还有"0.6B 识别模型"和"两个模型常驻"两档——前者现在就是参数面板上的「模型」一栏，
#: 后者（两个模型一起占显存）在本机的 10G 卡上太危险，都不再单独提供。
PROFILES: dict[str, tuple[str, list[str]]] = {
    "1": ("默认：参数在页面上选（精度 / 设备 / 范围 / 量化 / 识别模型 / 音源 / 语言）", []),
    "2": ("4G 特供：扒谱 bf16 权重 + 歌词 1GiB 预算 + 30s 切片，参数面板锁定（慢，但 4G 卡能跑）",
          ["--engine-weights", "bf16", "--asr-max-vram", "1GiB", "--asr-max-slice", "30",
           "--lock-params"]),
}


def use_utf8_stdio() -> None:
    """和 `bootstrap.ensure_utf8_stdio()` 一个口径：统一 UTF-8，别让转发出去的中文变乱码。

    `line_buffering=True` 是给"输出被重定向"的场景：不然横幅和菜单会攒在缓冲区里，
    直到子进程（服务）结束时才一起冒出来，看着像卡住了。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (ValueError, OSError):
            pass


def can_bind(port: int, host: str = "127.0.0.1") -> bool:
    probe = socket.socket()
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def port_owner(port: int) -> str | None:
    """谁占着这个端口？只说名字，够解释了。"""
    try:
        import psutil

        for connection in psutil.net_connections(kind="tcp"):
            if connection.laddr and connection.laddr.port == port and connection.status == "LISTEN":
                if connection.pid:
                    try:
                        return f"{psutil.Process(connection.pid).name()}（PID {connection.pid}）"
                    except Exception:
                        return f"PID {connection.pid}"
    except Exception:
        pass
    return None


def pick_port(preferred: int) -> tuple[int, list[str]]:
    """返回 (实际端口, 说明行)。首选端口绑不上（被占/被系统保留，常见 WinError 10013）就自动换。"""
    notes: list[str] = []
    if can_bind(preferred):
        return preferred, notes
    owner = port_owner(preferred)
    reason = f"被 {owner} 占用" if owner else "绑不上（可能是系统保留段，WinError 10013）"
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    fallback = probe.getsockname()[1]
    probe.close()
    notes.append(f"端口 {preferred} {reason}，自动改用 {fallback}")
    notes.append(f"想固定端口就用：webui.bat {fallback}")
    return fallback, notes


def ensure_frontend() -> bool:
    """前端产物缺了就构建（`npm run build`）。"""
    if STATIC.is_file():
        print(f"  [2/3] 前端产物 OK      {STATIC.relative_to(ROOT)}")
        return True
    print("  [2/3] 没有前端产物，正在构建…")
    if not (ROOT / "web").is_dir():
        print("  [x] 连 web/ 目录都没有，这不是完整的仓库")
        return False

    def npm(*args: str) -> int:
        # shell=True：Windows 上 npm 是 npm.cmd，直接 exec 会找不到
        return subprocess.call(list(args), cwd=ROOT / "web", shell=True)

    if not (ROOT / "web" / "node_modules").is_dir():
        if npm("npm", "install") != 0:
            print("  [x] npm install 失败，看上面的报错")
            return False
    if npm("npm", "run", "build") != 0:
        print("  [x] npm run build 失败，看上面的报错")
        return False
    if not STATIC.is_file():
        print("  [x] 构建完还是没有 index.html")
        return False
    print(f"  [2/3] 前端已构建        {STATIC.relative_to(ROOT)}")
    return True


def ask_profile(arg: str | None) -> str:
    if arg:
        return arg if arg in PROFILES else "1"
    print()
    print("  启动模式（直接回车 = 1）：")
    for key, (note, _) in PROFILES.items():
        print(f"    {key}  {note}")
    print()
    try:
        choice = input("  选一个 1/2 : ").strip()
    except EOFError:                      # 没有终端（管道/重定向）时按默认跑
        choice = ""
    return choice if choice in PROFILES else "1"


def open_browser_later(url: str) -> None:
    """服务本身秒起（模型是懒加载的，要等第一个任务才加载），等几秒再开页面就够。"""
    def open_it() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Timer(4.0, open_it).start()


def main(argv: list[str]) -> int:
    use_utf8_stdio()
    os.chdir(ROOT)

    port_arg = argv[1] if len(argv) > 1 else ""
    profile_arg = argv[2] if len(argv) > 2 else ""
    preferred = int(port_arg) if port_arg.isdigit() else DEFAULT_PORT

    print()
    print("  SheetSage2 Studio")
    print("  " + "-" * 56)

    print(f"  [1/3] 解释器 OK        {PYTHON.relative_to(ROOT)}")
    if not ensure_frontend():
        return 1

    port, notes = pick_port(preferred)
    print(f"  [3/3] 监听端口 {port}")
    for note in notes:
        print(f"        ! {note}")

    if not (ROOT / "models" / "SheetSage2" / "config.json").is_file():
        print("        ! 没看到 models/SheetSage2（会退回 HuggingFace 仓库 id，离线时可能失败）")

    profile = ask_profile(profile_arg)
    note, options = PROFILES[profile]
    url = f"http://127.0.0.1:{port}"

    print()
    print(f"  模式：{note}")
    print(f"  地址：{url}")
    print("  停止：在这个窗口按 Ctrl+C")
    print("  " + "-" * 56)
    print()

    if os.environ.get("SHEETSAGE_NO_BROWSER") != "1":
        open_browser_later(url)

    command = [str(PYTHON), "-m", "sheetsage2_webui.webui", "--port", str(port), *options]
    try:
        code = subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        code = 0
    if code:
        print()
        print("  服务异常退出（退出码 %d），把上面的报错发我。" % code)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
