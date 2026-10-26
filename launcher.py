"""Desktop entry: reuse our server or start it hidden, then open the browser."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def notify(message):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "小车台账", 0x10)
    except Exception:
        print(message, file=sys.stderr)


def running_url():
    info = DATA / "server.json"
    try:
        value = json.loads(info.read_text(encoding="utf-8"))
        port = value["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            return None
        url = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=1) as response:
            health = json.loads(response.read(2048))
        return url if health.get("app") == "car-ledger-local" else None
    except Exception:
        return None


def main():
    DATA.mkdir(exist_ok=True)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    lock_file = (DATA / "launch.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.write(b"0")
                lock_file.flush()
            for _ in range(100):
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("启动尚未完成，请稍后再次双击桌面入口。")
        url = running_url()
        if not url:
            python = ROOT / ".venv" / "Scripts" / "pythonw.exe"
            if not python.is_file():
                python = Path(sys.executable)
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            with (logs / "server.log").open("a", encoding="utf-8") as output:
                process = subprocess.Popen([str(python), str(ROOT / "server.py"), "--port", "0"],
                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                    env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            for _ in range(150):
                url = running_url()
                if url:
                    break
                if process.poll() is not None:
                    raise RuntimeError("本机服务启动失败。请查看程序目录 logs\\server.log。")
                time.sleep(0.1)
            if not url:
                raise RuntimeError("本机服务启动超时，请稍后重试。日志位于 logs\\server.log。")
        webbrowser.open(url)
    finally:
        lock_file.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        notify(str(exc))
