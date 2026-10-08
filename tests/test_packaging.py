r"""内测包（alpha 1）的三件事：ffmpeg 解析、清单、下载器。

这套东西的特点是**跑在内测者机器上、跑在环境还没建起来的时候**，所以：

1. `bootstrap.find_ffmpeg()` 必须优先认**随包自带**的那份（本机 ffmpeg 和项目不在一起，
   开发机上那两条 `D:\ffmpeg\bin` / `C:\ffmpeg\bin` 到了同好机器上是空路径）；
2. `downloads.json` 是下载器唯一的数据源，字段/校验必须齐全；
3. `tools/downloader.py` 要能**续传**、要能认出 sha256 不对的残包——用本地 HTTP 服务测，
   不碰外网。

    .\.venv\Scripts\python.exe -m pytest tests -q
"""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra in (PROJECT_ROOT / "src", PROJECT_ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from sheetsage2_webui import bootstrap  # noqa: E402

import downloader  # noqa: E402
import modelscope  # noqa: E402


# --------------------------------------------------------------------- ffmpeg
def test_bundled_ffmpeg_is_preferred_over_path(tmp_path, monkeypatch):
    """包里的 ffmpeg 要压过 PATH 上的——同好机器上装没装别的 ffmpeg 都不该影响。"""
    bundled = tmp_path / "runtime" / "ffmpeg" / "bin"
    bundled.mkdir(parents=True)
    exe = bundled / "ffmpeg.exe"
    exe.write_bytes(b"fake")
    monkeypatch.setattr(bootstrap, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "BUNDLED_FFMPEG_DIRS",
                        (tmp_path / "runtime" / "ffmpeg" / "bin",
                         tmp_path / "runtime" / "ffmpeg"))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: r"C:\other\ffmpeg.exe")
    monkeypatch.delenv("SHEETSAGE_FFMPEG", raising=False)
    monkeypatch.delenv("SHEETSAGE_FFMPEG_DIR", raising=False)
    monkeypatch.setenv("PATH", "C:\\other")

    assert bootstrap.bundled_ffmpeg() == str(exe)
    assert bootstrap.find_ffmpeg() == str(exe)
    assert str(exe.parent) in bootstrap.os.environ["PATH"].split(bootstrap.os.pathsep)


def test_ffmpeg_env_override_wins(tmp_path, monkeypatch):
    override = tmp_path / "somewhere"
    override.mkdir()
    (override / "ffmpeg.exe").write_bytes(b"fake")
    monkeypatch.setattr(bootstrap, "BUNDLED_FFMPEG_DIRS", ())
    monkeypatch.setenv("SHEETSAGE_FFMPEG_DIR", str(override))
    monkeypatch.setenv("SHEETSAGE_FFMPEG", r"C:\nope\ffmpeg.exe")
    assert bootstrap.find_ffmpeg() == str(override / "ffmpeg.exe")


def test_find_ffmpeg_returns_none_when_nothing_exists(monkeypatch):
    monkeypatch.setattr(bootstrap, "BUNDLED_FFMPEG_DIRS", ())
    monkeypatch.setattr(bootstrap, "FFMPEG_DIR_CANDIDATES", ())
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    monkeypatch.delenv("SHEETSAGE_FFMPEG", raising=False)
    monkeypatch.delenv("SHEETSAGE_FFMPEG_DIR", raising=False)
    assert bootstrap.find_ffmpeg() is None


def test_the_bundled_ffmpeg_is_where_the_packager_puts_it():
    """打包脚本写死的位置必须和 `bootstrap` 找的位置一致（否则包就白打了）。"""
    source = (PROJECT_ROOT / "scripts" / "build_alpha_package.py").read_text(encoding="utf-8")
    assert 'package / "runtime" / "ffmpeg" / "bin"' in source
    assert bootstrap.BUNDLED_FFMPEG_DIRS[0] == PROJECT_ROOT / "runtime" / "ffmpeg" / "bin"


# --------------------------------------------------------------------- 清单
def test_downloads_manifest_is_complete():
    manifest = json.loads((PROJECT_ROOT / "downloads.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "alpha1"

    # 随包的两样必须在
    assert manifest["bundled"]["python"]["path"] == "runtime/python"
    assert manifest["bundled"]["ffmpeg"]["path"] == "runtime/ffmpeg/bin/ffmpeg.exe"

    # torch：直链 + sha256 + 大小
    assert len(manifest["torch"]) == 2
    for item in manifest["torch"]:
        assert item["url"].startswith("https://")
        assert item["name"].endswith(".whl")
        assert len(item["sha256"]) == 64 and item["sha256"].isalnum()

    # 模型：**1.7B 必须只是可选**（不在最小集里），而且每个仓库都有目标目录与体积
    models = manifest["models"]
    assert "Qwen/Qwen3-ASR-0.6B" in models["minimal"]
    assert "Qwen/Qwen3-ASR-1.7B" not in models["minimal"]
    assert "Qwen/Qwen3-ASR-1.7B" in models["full"]
    assert set(models["minimal"]) < set(models["full"])           # 最小集是全量的真子集
    for repo, info in models["repositories"].items():
        assert info["target"].startswith("models/")
        assert info["bytes"] > 0 and info["files"] > 0
        assert info["revision"]
    assert models["minimal_bytes"] == sum(
        info["bytes"] for repo, info in models["repositories"].items()
        if repo in models["minimal"])
    assert models["full_bytes"] == sum(
        info["bytes"] for info in models["repositories"].values())


def test_model_target_dirs_match_what_the_app_loads(tmp_path):
    """模型下载目录要和 `engine.resolve_model_path` / ASR 认的目录名一致。"""
    manifest = json.loads((PROJECT_ROOT / "downloads.json").read_text(encoding="utf-8"))
    targets = {repo: Path(info["target"]).name
               for repo, info in manifest["models"]["repositories"].items()}
    assert targets["m-a-p/SheetSage2"] == "SheetSage2"
    assert targets["Qwen/Qwen3-ASR-0.6B"] == "Qwen3-ASR-0.6B"
    assert targets["Qwen/Qwen3-ForcedAligner-0.6B"] == "Qwen3-ForcedAligner-0.6B"


def test_repo_file_url_is_quoted():
    url = modelscope.file_url("m-a-p/SheetSage2", "render_assets/x y/z.json")
    assert url == ("https://www.modelscope.cn/models/m-a-p/SheetSage2/resolve/master/"
                   "render_assets/x%20y/z.json")


# --------------------------------------------------------------------- 下载器
class _Server(http.server.ThreadingHTTPServer):
    """给测试用的本地 HTTP 服务：支持 Range，可以故意返回坏内容。"""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.payload = b""
        self.requests: list[str] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/file.bin"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:                       # noqa: N802 - http.server 的接口
        server: _Server = self.server            # type: ignore[assignment]
        payload = server.payload
        server.requests.append(self.headers.get("Range", ""))
        start = 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
        body = payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:            # 别把访问日志混进测试输出
        pass


@pytest.fixture()
def server():
    httpd = _Server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_download_verifies_sha256_and_writes_the_target(server, tmp_path: Path):
    server.payload = b"hello alpha1" * 1000
    digest = hashlib.sha256(server.payload).hexdigest()
    target = tmp_path / "out.bin"
    downloader.download(server.url, target, sha256=digest, size=len(server.payload),
                        progress=False)
    assert target.read_bytes() == server.payload
    assert not (tmp_path / "out.bin.part").exists()


def test_download_resumes_from_a_partial_file(server, tmp_path: Path):
    server.payload = b"0123456789" * 10000
    digest = hashlib.sha256(server.payload).hexdigest()
    target = tmp_path / "out.bin"
    part = tmp_path / "out.bin.part"
    part.write_bytes(server.payload[:40000])        # 假装上次下了 40000 字节

    downloader.download(server.url, target, sha256=digest, size=len(server.payload),
                        progress=False)
    assert target.read_bytes() == server.payload
    assert server.requests[0] == "bytes=40000-"     # 真的是接着下的，没有从头来


def test_download_redownloads_when_sha256_does_not_match(server, tmp_path: Path):
    server.payload = b"good data"
    target = tmp_path / "out.bin"
    target.write_bytes(b"bad data")                 # 已经有同名文件，但内容不对
    downloader.download(server.url, target, sha256=hashlib.sha256(server.payload).hexdigest(),
                        size=len(server.payload), progress=False)
    assert target.read_bytes() == server.payload


def test_download_raises_after_retries(server, tmp_path: Path, monkeypatch):
    server.payload = b"x" * 100
    monkeypatch.setattr(downloader, "RETRIES", 2)
    with pytest.raises(RuntimeError):
        downloader.download(server.url, tmp_path / "out.bin",
                            sha256="0" * 64, size=len(server.payload), progress=False)


def test_human_bytes_reads_like_a_person_wrote_it():
    assert downloader.human_bytes(512) == "512 B"
    assert downloader.human_bytes(2048) == "2.0 KB"
    assert downloader.human_bytes(1867405006).endswith("GB")


def test_human_seconds_is_short():
    assert downloader.human_seconds(0) == "0s"
    assert downloader.human_seconds(95) == "1m35s"
    assert downloader.human_seconds(3700) == "1h01m"


# --------------------------------------------------------------------- 进度条
class _FakeStream:
    """假的输出流：`isatty` 可控，攒下来的文本可以直接断言。"""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.text = ""

    def write(self, text: str) -> None:
        self.text += text

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self.tty


def test_progress_line_has_percent_size_speed_and_eta():
    """内测者要能从这一行里看出"在下、还有多久"。"""
    stream = _FakeStream(tty=True)
    bar = downloader.Progress("torch.whl", stream=stream)
    bar.update(512 * 1024 * 1024, 1024 * 1024 * 1024)
    line = stream.text
    assert "torch.whl" in line and "%" in line
    assert "MB/1.0 GB" in line                      # 已下/总量
    assert "/s" in line                             # 速度
    assert "剩 " in line                            # 剩余时间
    assert line.startswith("\r")                    # 终端里原地刷新，不刷屏


def test_progress_writes_whole_lines_when_not_a_tty():
    """输出被重定向（日志文件）时不能塞满 \\r，改成每 5% 一行。"""
    stream = _FakeStream(tty=False)
    ticks = iter(range(10_000))
    bar = downloader.Progress("model.bin", stream=stream,
                              clock=lambda: next(ticks) * 0.5)     # 每次调用走 0.5 秒
    for done in range(0, 1024 * 1024 * 1024, 8 * 1024 * 1024):
        bar.update(done, 1024 * 1024 * 1024)
    assert "\r" not in stream.text
    lines = [line for line in stream.text.splitlines() if line.strip()]
    assert 1 < len(lines) <= 25                     # 20 个 5% 桶，不该一 MiB 一行


def test_progress_message_clears_the_line_first():
    stream = _FakeStream(tty=True)
    bar = downloader.Progress("model.bin", stream=stream)
    bar.update(1024, 2048)
    bar.message("      …网络卡住了")
    assert "网络卡住了" in stream.text
    assert stream.text.index("\r" + " " * 110) < stream.text.index("网络卡住了")


def test_sha256_reports_progress(server, tmp_path: Path):
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (3 * 1024 * 1024))
    stream = _FakeStream(tty=True)
    bar = downloader.Progress("校验中", stream=stream)
    digest = downloader.sha256_of(path, progress=bar)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "校验中" in stream.text


def test_download_says_something_before_the_first_byte(server, tmp_path: Path):
    """连接阶段也要说话——不然前几秒窗口是死的。"""
    import io
    import contextlib

    server.payload = b"hello" * 1000
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        downloader.download(server.url, tmp_path / "out.bin",
                            sha256=hashlib.sha256(server.payload).hexdigest(),
                            size=len(server.payload), label="thing.bin", progress=True)
    text = buffer.getvalue()
    assert "连接中" in text or "接着下" in text
    assert "完成" in text


def test_stall_watcher_says_something_when_nothing_arrives():
    """超过 20 秒没有数据要主动喊一声，别让内测者干等。"""
    stream = _FakeStream(tty=True)
    bar = downloader.Progress("model.bin", stream=stream)
    watcher = downloader._StallWatcher(bar, timeout=0.1)
    watcher.start()
    for _ in range(30):
        if "没收到数据" in stream.text:
            break
        import time as _time
        _time.sleep(0.1)
    watcher.stop()
    assert "没收到数据" in stream.text
    assert "接着下" in stream.text                  # 要告诉人家重跑能续


def test_free_space_points_at_an_existing_parent(tmp_path: Path):
    assert downloader.free_space(tmp_path / "not" / "there") > 0


def test_socket_timeout_is_a_retryable_error():
    """断线要能重试：`socket.timeout` 归在 `OSError` 里，别让它直接冒出去。"""
    assert issubclass(socket.timeout, OSError)


# --------------------------------------------------------------------- 双击入口
@pytest.mark.skipif(sys.platform != "win32", reason=".bat 只在 Windows 上跑")
def test_batch_files_never_flash_and_quit(tmp_path: Path):
    """双击 `.bat` 不许"语法错误 + 闪退"：要么干活，要么把人话留在屏幕上。

    **真出过这个 bug**：`if ... ( ... )` 块里的 echo 文本带了括号（`torch (~1.8 GB)`），
    cmd 会提前结束这个块并报 "and was unexpected at this time." ——双击就是一闪而过，
    内测者什么也看不到。现在改成 `goto` 标签 + 不带括号的文本。
    """
    import subprocess

    for name in ("webui.bat", "setup.bat"):
        shutil.copy(PROJECT_ROOT / name, tmp_path / name)

    for name in ("webui.bat", "setup.bat"):
        done = subprocess.run(["cmd", "/c", name], cwd=tmp_path, input="\n",
                              capture_output=True, text=True, timeout=120)
        output = (done.stdout or "") + (done.stderr or "")
        assert "unexpected" not in output.lower(), f"{name} 有批处理语法错误：\n{output}"
        # 空目录（没有 .venv / 没有 runtime）时要**说人话**，而不是直接退出
        assert "runtime" in output.lower() or "incomplete" in output.lower(), \
            f"{name} 没有给出可读的原因：\n{output}"


@pytest.mark.skipif(sys.platform != "win32", reason=".bat 只在 Windows 上跑")
def test_webui_bat_has_no_parentheses_inside_blocks():
    """守卫规则：`if ... (` 块里不能出现没转义的括号（那正是闪退的根因）。"""
    text = (PROJECT_ROOT / "webui.bat").read_text(encoding="utf-8")
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("if ") and stripped.endswith("("):
            in_block = True
            continue
        if in_block:
            if stripped == ")":
                in_block = False
                continue
            body = stripped.split("rem", 1)[0]
            assert "(" not in body and ")" not in body, \
                f"块里出现了括号，会让 cmd 提前结束块：{line!r}"
