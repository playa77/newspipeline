"""SSH local-forward lifecycle (Qt-free; callback-based).

Owns the full tunnel lifecycle for one connection:
  spawn  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
             -L <local_port>:127.0.0.1:<server_port> <user>@<host>
  probe  TCP-connect readiness against the local port
  watch  detect ssh death while connected and surface it
  stop   terminate/kill the process and clean up

The local port is picked at runtime by bind-probing; if ssh loses a race
for that port, one retry with a fresh port is attempted. BatchMode=yes is
added on top of the transport contract above so a missing/broken SSH key
fails fast with a clear error instead of hanging on an interactive
password prompt.
"""

# version: 0.3.1

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from collections import deque
from typing import Callable

logger = logging.getLogger(__name__)

_READINESS_TIMEOUT = 15.0
_PROBE_INTERVAL = 0.3
_PORT_BIND_ATTEMPTS = 2


def find_free_local_port() -> int:
    """Bind-probe a free loopback port (released before ssh claims it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


class TunnelManager:
    """Callback contract (all invoked from the tunnel thread):
    state("connecting") -> state("connected", local_port) | error(message)
    ; a connected tunnel that dies calls error(message); stop() calls
    state("disconnected").
    """

    def __init__(
        self,
        on_state: Callable[..., None],
        on_error: Callable[[str], None],
    ) -> None:
        self._on_state = on_state
        self._on_error = on_error
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stopping = threading.Event()
        self.local_port: int | None = None
        self._stderr_tail: deque[str] = deque(maxlen=15)

    # ---- public API (main thread) ------------------------------------------

    def start(self, host: str, user: str, server_port: int) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Tunnel is already active")
        self._stop.clear()
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._tunnel_main,
            args=(host, user, server_port),
            name="newspipeline-tunnel",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Teardown on exit or crash — idempotent."""
        self._stopping.set()
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("ssh did not terminate within 5s — killing")
                proc.kill()
                proc.wait(timeout=5)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        self._proc = None
        self._thread = None
        self._on_state("disconnected", None)

    # ---- worker side ---------------------------------------------------------

    def _spawn_ssh(self, host: str, user: str, server_port: int) -> int:
        local_port = find_free_local_port()
        cmd = [
            "ssh", "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{server_port}",
            f"{user}@{host}",
        ]
        logger.info("Spawning tunnel: port %d -> 127.0.0.1:%d via %s@%s",
                    local_port, server_port, user, host)
        self._stderr_tail.clear()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        threading.Thread(
            target=self._drain_stderr, daemon=True,
        ).start()
        return local_port

    def _drain_stderr(self) -> None:
        # Read continuously so the pipe cannot fill and wedge ssh; keep a
        # short tail for the human-readable error surface.
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append(line.rstrip())

    def _stderr_text(self) -> str:
        return "\n".join(self._stderr_tail).strip() or "(no output from ssh)"

    def _tunnel_main(self, host: str, user: str, server_port: int) -> None:
        self._on_state("connecting", None)
        for attempt in range(_PORT_BIND_ATTEMPTS):
            if self._stop.is_set():
                return
            try:
                local_port = self._spawn_ssh(host, user, server_port)
            except OSError as exc:
                self._on_error(f"Could not start ssh: {exc}")
                return

            deadline = time.monotonic() + _READINESS_TIMEOUT
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                if self._proc.poll() is not None:
                    # ssh died during startup — usually auth failure or a
                    # lost race for the local forward port.
                    if attempt + 1 < _PORT_BIND_ATTEMPTS and \
                            "already in use" in self._stderr_text().lower():
                        break  # retry with a fresh port
                    self._on_error(
                        "SSH tunnel failed:\n" + self._stderr_text()
                    )
                    return
                if _port_reachable(local_port):
                    self.local_port = local_port
                    logger.info("Tunnel ready on 127.0.0.1:%d", local_port)
                    self._on_state("connected", local_port)
                    self._watch_while_connected(host)
                    return
                time.sleep(_PROBE_INTERVAL)
            else:
                self._on_error(
                    f"Tunnel did not become ready within {_READINESS_TIMEOUT:.0f}s"
                )
                self.stop()
                return
        # Both bind attempts lost their race.
        self._on_error("SSH tunnel failed:\n" + self._stderr_text())

    def _watch_while_connected(self, host: str) -> None:
        """Detect ssh death while connected (network drop, remote reboot)."""
        proc = self._proc
        while proc is not None and not self._stop.is_set():
            code = proc.poll()
            if code is not None:
                self._on_error(
                    f"SSH connection to {host} closed (exit code {code}):\n"
                    + self._stderr_text()
                )
                return
            time.sleep(1.0)
