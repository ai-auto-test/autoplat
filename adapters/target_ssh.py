"""Target access over OpenSSH with product-specific commands taken from the bench file.

Each command must print JSON (or plain lines for logs). Example bench options:
  host: root@10.0.7.50
  commands:
    health: "ivi-ctl health --json"
    identity: "ivi-ctl identity --json"
    configuration: "ivi-ctl config --json"
    view_state: "ivi-ctl camera view --json"
    request_view: "ivi-ctl camera request {view}"
    display_status: "ivi-ctl display status --json"
    indicators: "ivi-ctl hmi indicators --json"
    logs: "journalctl -o short-monotonic --no-pager -S -{since}s"
    restart_service: "systemctl restart {name}"
    reboot: "reboot"
    go_home: "ivi-ctl hmi home"
    dismiss_dialogs: "ivi-ctl hmi dismiss"
    user_data_digest: "ivi-ctl userdata digest"
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any

from autoplat.errors import InfrastructureError, TargetUnreachable
from autoplat.timeline import traced


class SshTarget:
    def __init__(self, host: str, commands: dict[str, str], *, port: int = 22,
                 identity_file: str | None = None, connect_timeout_s: int = 5):
        self.host, self.commands = host, commands
        self._base = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout_s}", "-p", str(port)]
        if identity_file:
            self._base += ["-i", identity_file]

    def run(self, command: str, *, timeout_s: float = 10.0) -> str:
        try:
            proc = subprocess.run(self._base + [self.host, command], capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise TargetUnreachable(f"ssh {self.host}: '{command}' timed out after {timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise InfrastructureError("OpenSSH client not installed on this workstation") from exc
        if proc.returncode == 255:
            raise TargetUnreachable(f"ssh {self.host}: {proc.stderr.strip()}")
        if proc.returncode != 0:
            raise RuntimeError(f"'{command}' exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _cmd(self, key: str, **fmt: Any) -> str:
        if key not in self.commands:
            raise NotImplementedError(f"bench file defines no '{key}' command for this target")
        return self.commands[key].format(**{k: shlex.quote(str(v)) for k, v in fmt.items()})

    def _json(self, key: str, timeout_s: float = 10.0, **fmt: Any) -> Any:
        return json.loads(self.run(self._cmd(key, **fmt), timeout_s=timeout_s))

    def health(self, services: list[str] | None = None, *, timeout_s: float = 5.0) -> dict[str, str]:
        data = self._json("health", timeout_s)
        return {k: v for k, v in data.items() if services is None or k in services}

    def identity(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._json("identity", timeout_s)

    def configuration(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        return self._json("configuration", timeout_s)

    def view_state(self) -> dict[str, Any]:
        return self._json("view_state")

    @traced("target")
    def request_view(self, view: str) -> None:
        self.run(self._cmd("request_view", view=view))

    def display_status(self) -> dict[str, Any]:
        return self._json("display_status")

    def indicators(self) -> set[str]:
        return set(self._json("indicators"))

    def logs(self, since_s: float | None = None) -> list[str]:
        return self.run(self._cmd("logs", since=int(since_s or 60)), timeout_s=20).splitlines()

    @traced("target")
    def restart_service(self, name: str, *, timeout_s: float = 15.0) -> None:
        self.run(self._cmd("restart_service", name=name), timeout_s=timeout_s)

    @traced("target")
    def reboot(self, *, timeout_s: float = 10.0) -> None:
        try:
            self.run(self._cmd("reboot"), timeout_s=timeout_s)
        except TargetUnreachable:
            pass  # the connection drops as the target goes down

    def go_home(self) -> None:
        self.run(self._cmd("go_home"))

    def dismiss_dialogs(self) -> None:
        self.run(self._cmd("dismiss_dialogs"))

    def user_data_digest(self) -> str:
        return self.run(self._cmd("user_data_digest")).strip()
