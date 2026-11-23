from pathlib import Path

def test_launch_waits_for_the_port_before_opening_a_browser(tmp_path, monkeypatch):
    """服务没 listen 就不开浏览器；起来了才开（beta3 的 Failed to fetch 就是这个）。"""
    import importlib.util
    import socket

    spec = importlib.util.spec_from_file_location(
        "sheetsage2_launch", Path(__file__).resolve().parents[1] / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert launch.wait_until_ready(port, timeout=0.3) is False

    server = socket.socket()
    server.bind(("127.0.0.1", port))
    server.listen(1)
    try:
        assert launch.wait_until_ready(port, timeout=2.0) is True
    finally:
        server.close()

    opened: list[str] = []
    monkeypatch.setattr(launch, "open_url", opened.append)
    launch.open_browser_when_ready("http://127.0.0.1:%d" % port, port, timeout=0.3)
    import time as _time
    _time.sleep(0.8)
    assert opened == []            # 端口已关，超时后不应该开
