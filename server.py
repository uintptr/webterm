#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["aiohttp"]
# ///
"""Tabbed web terminal: xterm.js in the browser, one dtach session per tab.

Each tab is a detached `dtach -n` master holding a shell. The server keeps one
`dtach -a` client per tab (a Hub) and fans its output out to every browser viewing
that tab, replaying recent output to late joiners. Closing the browser or
restarting the server only detaches; closing the tab kills the session.
"""

import asyncio
import fcntl
import ipaddress
import json
import os
import pwd
import re
import shutil
import socket
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import ClassVar

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
ORDER = SESSIONS / "order.json"
STATIC = ROOT / "static"
XTERM = ROOT / "node_modules" / "@xterm"

DTACH = shutil.which("dtach")
# launchd sets no $SHELL, so fall back to the login shell from the user database.
SHELL = os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
TAB_ID = re.compile(r"^[0-9a-z]{1,32}$")
COLOR = re.compile(r"^#[0-9a-f]{6}$")


def git_commit() -> str:
    """The checkout's commit, read once at startup: the code this server is running."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=8", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


COMMIT = git_commit()
# The machine the shells run on, which the page puts in its title.
HOSTNAME = socket.gethostname()
# (checked at, checkout's commit, server.py changed since COMMIT), refreshed by checkout_head().
_head = (0.0, COMMIT, False)


async def git(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(ROOT), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
    except (OSError, asyncio.TimeoutError):
        return -1, ""
    return proc.returncode, out.decode().strip()


async def checkout_head() -> tuple[str, bool]:
    """The checkout's commit now, and whether server.py differs from the one running.

    The page is read from disk on every request, so it follows the checkout without a
    restart; only a change to server.py needs one. Checked at most every few seconds.
    """
    global _head
    checked, head, stale = _head
    if time.monotonic() - checked < 5:
        return head, stale
    code, new = await git("rev-parse", "--short=8", "HEAD")
    if code == 0 and new != head:
        head = new
        # diff --quiet exits 1 when the file differs.
        stale = bool(COMMIT) and head != COMMIT and \
            (await git("diff", "--quiet", COMMIT, head, "--", "server.py"))[0] == 1
    _head = (time.monotonic(), head, stale)
    return head, stale


def sock_path(tab_id: str) -> Path:
    return SESSIONS / f"{tab_id}.sock"


def name_path(tab_id: str) -> Path:
    return SESSIONS / f"{tab_id}.name"


def color_path(tab_id: str) -> Path:
    return SESSIONS / f"{tab_id}.color"


def read_meta(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def forget(tab_id: str) -> None:
    sock_path(tab_id).unlink(missing_ok=True)
    name_path(tab_id).unlink(missing_ok=True)
    color_path(tab_id).unlink(missing_ok=True)


def read_order() -> list[str]:
    try:
        return [str(x) for x in json.loads(ORDER.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def is_alive(path: Path) -> bool:
    """A dtach socket is live if something accepts on it; stale files are left by crashes."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def list_tabs() -> list[dict[str, str]]:
    tabs: list[dict[str, str]] = []
    for sock in sorted(SESSIONS.glob("*.sock")):
        tab_id = sock.stem
        if not is_alive(sock):
            forget(tab_id)
            continue
        tabs.append({"id": tab_id,
                     "name": read_meta(name_path(tab_id)),
                     "color": read_meta(color_path(tab_id))})
    # Tabs missing from the saved order (new ones) go last, in creation order.
    rank = {tab_id: i for i, tab_id in enumerate(read_order())}
    tabs.sort(key=lambda tab: rank.get(tab["id"], len(rank)))
    return tabs


def valid_id(request: web.Request) -> str:
    tab_id = request.match_info["id"]
    if not TAB_ID.match(tab_id) or not sock_path(tab_id).exists():
        raise web.HTTPNotFound()
    return tab_id


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def make_controlling_tty() -> None:
    # Runs in the child after setsid(): take stdin's pty as the controlling terminal
    # so resizes reach dtach as SIGWINCH.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class Hub:
    """One long-lived `dtach -a` client per tab, shared by every browser viewing it.

    It keeps the tail of the tab's output, so a browser that connects later (or
    reloads) is replayed the recent screen instead of a blank terminal. It stays
    attached with no viewers too, so output produced meanwhile is not lost.
    """

    HISTORY = 512 * 1024
    hubs: ClassVar[dict[str, "Hub"]] = {}

    @classmethod
    def get(cls, tab_id: str) -> "Hub":
        hub = cls.hubs.get(tab_id)
        if hub is None:
            hub = cls.hubs[tab_id] = cls(tab_id)
        return hub

    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id
        self.history = bytearray()
        # ws -> queue of outgoing bytes; None means the session ended.
        self.viewers: dict[web.WebSocketResponse, asyncio.Queue[bytes | None]] = {}
        self.size = (80, 24)
        self.master, slave = os.openpty()
        set_winsize(self.master, *self.size)
        # preexec_fn is the only way subprocess offers to set a controlling tty, and the
        # hook is a single ioctl: none of the locks that make it unsafe with threads.
        self.proc = subprocess.Popen(
            [DTACH, "-a", str(sock_path(tab_id)), "-E", "-z", "-r", "winch"],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, preexec_fn=make_controlling_tty,  # noqa: PLW1509
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        asyncio.get_running_loop().add_reader(self.master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._ended()
            return
        self.history += data
        if len(self.history) > self.HISTORY:
            # Drop the oldest output, cutting at a line break so the replay doesn't
            # start inside an escape sequence.
            cut = self.history.find(b"\n", len(self.history) - self.HISTORY)
            del self.history[: cut + 1 if cut >= 0 else len(self.history) - self.HISTORY]
        for queue in self.viewers.values():
            queue.put_nowait(data)

    def _ended(self) -> None:
        # The dtach client exited on its own: the session's shell is gone.
        self._detach()
        self.proc.wait()
        for queue in self.viewers.values():
            queue.put_nowait(None)

    def _detach(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        Hub.hubs.pop(self.tab_id, None)

    def join(self, ws: web.WebSocketResponse) -> "asyncio.Queue[bytes | None]":
        # Snapshot and register in one step (no await between) so nothing is missed or doubled.
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        queue.put_nowait(bytes(self.history))
        self.viewers[ws] = queue
        return queue

    def leave(self, ws: web.WebSocketResponse) -> None:
        self.viewers.pop(ws, None)

    async def write(self, data: bytes) -> None:
        # The pty's input queue is small; a big paste has to wait for the shell to drain it.
        while data and Hub.hubs.get(self.tab_id) is self:
            try:
                data = data[os.write(self.master, data):]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    def resize(self, cols: int, rows: int) -> None:
        # One pty for everyone: the browser that resized (or focused the tab) last sets the size.
        if (cols, rows) != self.size and Hub.hubs.get(self.tab_id) is self:
            self.size = (cols, rows)
            set_winsize(self.master, cols, rows)

    async def shutdown(self) -> None:
        # Graceful shutdown waits for open WebSockets, which a browser never closes on its
        # own: close them (the page reconnects to the next server) and detach the client.
        for ws in list(self.viewers):
            await ws.close(code=1001, message=b"server restarting")
        self._detach()
        self.proc.terminate()


async def api_list(request: web.Request) -> web.Response:
    # The page polls this list, so the header follows the checkout, and flags when
    # server.py has changed under the running server.
    head, stale = await checkout_head()
    headers = {"X-Commit": head, "X-Hostname": HOSTNAME}
    if stale:
        headers["X-Running"] = COMMIT
    return web.json_response(list_tabs(), headers=headers)


async def api_create(request: web.Request) -> web.Response:
    # Hex milliseconds: unique enough, and sorting by id keeps creation order.
    tab_id = format(int(time.time() * 1000), "x")
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    env.pop("TMUX", None)
    proc = await asyncio.create_subprocess_exec(
        DTACH, "-n", str(sock_path(tab_id)), "-z", SHELL, "-l",
        cwd=os.path.expanduser("~"), env=env,
    )
    if await proc.wait() != 0:
        raise web.HTTPInternalServerError(text="dtach failed to start the session")
    for _ in range(50):
        if is_alive(sock_path(tab_id)):
            break
        await asyncio.sleep(0.02)
    Hub.get(tab_id)
    return web.json_response({"id": tab_id, "name": "", "color": ""})


async def api_update(request: web.Request) -> web.Response:
    """Set a tab's name and/or colour; a key left out of the body is left as it is."""
    tab_id = valid_id(request)
    body = await request.json()
    if "name" in body:
        name_path(tab_id).write_text(str(body["name"]).strip()[:64], encoding="utf-8")
    if "color" in body:
        color = str(body["color"]).strip().lower()
        if color and not COLOR.match(color):
            raise web.HTTPBadRequest(text="color must be #rrggbb or empty")
        color_path(tab_id).write_text(color)
    return web.json_response({"id": tab_id,
                              "name": read_meta(name_path(tab_id)),
                              "color": read_meta(color_path(tab_id))})


async def api_reorder(request: web.Request) -> web.Response:
    """Set the tab order from {"order": [ids]}; tabs it leaves out go after the ones it lists."""
    body = await request.json()
    ids = [str(x) for x in body.get("order", []) if TAB_ID.match(str(x))]
    ORDER.write_text(json.dumps(ids), encoding="utf-8")
    return web.json_response(list_tabs())


async def api_delete(request: web.Request) -> web.Response:
    tab_id = valid_id(request)
    # The detached master is the process started as `dtach -n .../<id>.sock`; killing it
    # closes the shell's pty, which hangs up the shell and everything under it. Match on
    # the id rather than the full path, so sessions survive the project being moved.
    pkill = await asyncio.create_subprocess_exec("pkill", "-TERM", "-f", rf"dtach -n [^ ]*/{tab_id}\.sock ")
    await pkill.wait()  # exit 1 just means it was already gone
    for _ in range(50):
        if not is_alive(sock_path(tab_id)):
            break
        await asyncio.sleep(0.02)
    forget(tab_id)
    return web.json_response({"id": tab_id})


async def ws_attach(request: web.Request) -> web.WebSocketResponse:
    tab_id = valid_id(request)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    hub = Hub.get(tab_id)
    queue = hub.join(ws)

    async def pump_output() -> None:
        while (data := await queue.get()) is not None:
            await ws.send_bytes(data)
        await ws.close(code=4000, message=b"session ended")

    pump = asyncio.create_task(pump_output())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await hub.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                ctl = json.loads(msg.data)
                if ctl.get("type") == "resize":
                    hub.resize(int(ctl["cols"]), int(ctl["rows"]))
    finally:
        pump.cancel()
        hub.leave(ws)
    return ws


def allow_only(spec: str):
    """Middleware refusing clients outside `spec`, a comma-separated list of IPs/CIDRs."""
    try:
        nets = [ipaddress.ip_network(part.strip()) for part in spec.split(",") if part.strip()]
    except ValueError as e:
        sys.exit(f"ALLOW: {e}")

    @web.middleware
    async def check(request: web.Request, handler):
        try:
            ip = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden()
        # A dual-stack listener reports IPv4 clients as ::ffff:a.b.c.d.
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not any(ip in net for net in nets):
            raise web.HTTPForbidden()
        return await handler(request)

    return check


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def start_hubs(app: web.Application) -> None:
    # Re-attach to sessions that outlived a server restart, so they record output again.
    for tab in list_tabs():
        Hub.get(tab["id"])


async def stop_hubs(app: web.Application) -> None:
    for hub in list(Hub.hubs.values()):
        await hub.shutdown()


def main() -> None:
    if not (XTERM / "xterm").is_dir():
        sys.exit(f"xterm.js is missing: run `npm ci` in {ROOT}")
    if DTACH is None:
        sys.exit("dtach is missing: install it (e.g. `apt install dtach` or `brew install dtach`)")
    SESSIONS.mkdir(exist_ok=True)
    allow = os.environ.get("ALLOW", "").strip()
    app = web.Application(middlewares=[allow_only(allow)] if allow else [])
    app.on_startup.append(start_hubs)
    app.on_shutdown.append(stop_hubs)
    app.add_routes([
        web.get("/", index),
        web.get("/api/tabs", api_list),
        web.post("/api/tabs", api_create),
        web.put("/api/tabs", api_reorder),
        web.patch("/api/tabs/{id}", api_update),
        web.delete("/api/tabs/{id}", api_delete),
        web.get("/ws/{id}", ws_attach),
        web.static("/static", STATIC),
        web.static("/vendor", XTERM),
    ])
    host = os.environ.get("BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "7682"))
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
