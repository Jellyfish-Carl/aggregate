from __future__ import annotations

import os
import threading
import webbrowser

from config import DEFAULT_HOST, DEFAULT_PORT
from service.server import find_available_port, run


def _open_browser(url: str) -> None:
    if os.environ.get("MPC_DEMO_OPEN_BROWSER", "1") != "0":
        webbrowser.open(url)


def main() -> None:
    host = os.environ.get("MPC_DEMO_HOST", DEFAULT_HOST)
    preferred_port = int(os.environ.get("MPC_DEMO_PORT", str(DEFAULT_PORT)))
    port = find_available_port(host, preferred_port)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = "http://%s:%d" % (browser_host, port)

    if port != preferred_port:
        print("端口 %d 已被占用，改用 %d" % (preferred_port, port))
    print("正在启动储售集合体 Demo: " + url)

    browser_timer = threading.Timer(0.8, _open_browser, args=(url,))
    browser_timer.daemon = True
    browser_timer.start()
    run(host=host, port=port)


if __name__ == "__main__":
    main()
