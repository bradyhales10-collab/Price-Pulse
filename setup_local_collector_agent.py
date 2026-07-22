from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

from app.local_agent_credentials import protect_password


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "data" / "private" / "local_collector_agent.json"


def main() -> int:
    print("Part Pulse Desktop Collector Setup")
    server_url = input("Part Pulse web address [http://141.148.156.56]: ").strip() or "http://141.148.156.56"
    username = input("Part Pulse username [brady]: ").strip() or "brady"
    password = getpass.getpass("Part Pulse password: ")
    if not password:
        print("A password is required.")
        return 1
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "server_url": server_url.rstrip("/"),
                "username": username,
                "protected_password": protect_password(password),
                "poll_seconds": 3,
                "headless": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    startup_command = startup_dir / "Part Pulse Collector.cmd"
    startup_command.write_text(
        f'@echo off\nstart "" /min "{executable}" "{ROOT / "local_collector_agent.py"}" --config "{CONFIG_PATH}"\n',
        encoding="ascii",
    )
    subprocess.Popen(
        [str(executable), str(ROOT / "local_collector_agent.py"), "--config", str(CONFIG_PATH)],
        cwd=ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print("Setup complete. The collector is running and will start automatically when you sign in to Windows.")
    print("Return to Price Check and wait a few seconds for Desktop collector connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
