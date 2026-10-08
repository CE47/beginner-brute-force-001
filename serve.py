#!/usr/bin/env python3
"""
serve.py — lab server for beginner-brute-force-001.

Two jobs, one process, zero dependencies (Python 3 standard library only):

  1. Serve this folder over HTTP so guided-walkthrough.html can be opened from
     any browser, on any device, at http://127.0.0.1:<port>/
  2. Bridge a REAL bash PTY to the browser over a WebSocket at /term, so every
     command in the walkthrough (head, awk, grep, pipes, redirects) runs in
     genuine bash against the real .log files. Nothing is simulated.

Students never type a command: they double-click "start-lab.desktop" on the
Kali desktop, or run ./start-lab.sh from a terminal. Both start this server and
open the browser. The page itself discovers the server automatically, so it also
works when opened straight from the file system.

Usage:
    python3 serve.py                 # auto port from 8777, do not open browser
    python3 serve.py --open          # start and open the browser automatically
    python3 serve.py 9000            # try a specific port first
    python3 serve.py --open --port 9000

Ctrl+C stops the server.
"""
import base64
import errno
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LAB_ID = "beginner-brute-force-001"
PAGE = "guided-walkthrough.html"
DEFAULT_PORT = 8777
PORT_ATTEMPTS = 24


# ---------------------------------------------------------------------------
# WebSocket frame helpers (RFC 6455, minimal server-side implementation)
# ---------------------------------------------------------------------------
def ws_accept_key(key):
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = conn.recv(n - len(buf))
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def ws_recv_frame(conn):
    """Read a single frame. Returns (opcode, payload_bytes) or (None, None)."""
    try:
        hdr = recv_exact(conn, 2)
    except OSError:
        return None, None
    if not hdr or len(hdr) < 2:
        return None, None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else b"\x00\x00\x00\x00"
    data = recv_exact(conn, length) if length else b""
    if masked:
        data = bytes(d ^ mask[i % 4] for i, d in enumerate(data))
    return opcode, data


def ws_send_frame(conn, data, opcode=0x1):
    """Send a frame (opcode 0x1 text, 0x2 binary, 0x8 close)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    header = bytearray()
    header.append(0x80 | opcode)
    n = len(data)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    try:
        conn.sendall(bytes(header) + data)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# PTY bridge: spawn real bash, pump bytes both ways
# ---------------------------------------------------------------------------
def set_winsize(fd, cols, rows):
    """Resize the PTY window so full-screen programs and wrapped output behave."""
    try:
        cols = int(cols)
        rows = int(rows)
    except (TypeError, ValueError):
        return
    if cols <= 0 or rows <= 0:
        return
    try:
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except Exception:
        pass


def handle_ws_terminal(conn):
    """Spawn an interactive bash shell in HERE and bridge it to the socket."""
    pid, master_fd = pty.fork()
    if pid == 0:
        # child
        try:
            os.chdir(HERE)
            os.environ["TERM"] = "xterm-256color"
            # Short, coloured prompt: the full path would eat half a phone screen.
            os.environ["PS1"] = (
                r"\[\e[38;5;45m\]analyst\[\e[38;5;109m\]@\[\e[38;5;45m\]kali"
                r"\[\e[0m\]:\[\e[38;5;111m\]\W\[\e[0m\]\$ "
            )
            os.environ["HISTFILE"] = os.path.join(
                os.environ.get("TMPDIR", "/tmp"), "ctia-lab-%s.history" % LAB_ID
            )
            os.environ["PAGER"] = "cat"
            os.environ["SYSTEMD_PAGER"] = "cat"
            try:
                os.execvp("bash", ["bash", "--norc", "--noprofile", "-i"])
            except FileNotFoundError:
                os.execvp("sh", ["sh", "-i"])
        except Exception:
            pass
        os._exit(1)

    # parent: pump master_fd <-> websocket
    stop = threading.Event()

    def pty_to_ws():
        while not stop.is_set():
            try:
                r, _, _ = select.select([master_fd], [], [], 0.2)
            except OSError:
                break
            if master_fd in r:
                try:
                    data = os.read(master_fd, 8192)
                except OSError:
                    break
                if not data:
                    break
                ws_send_frame(conn, data, opcode=0x1)
        stop.set()
        ws_send_frame(conn, b"", opcode=0x8)

    threading.Thread(target=pty_to_ws, daemon=True).start()

    while not stop.is_set():
        opcode, data = ws_recv_frame(conn)
        if opcode is None or opcode == 0x8:  # closed
            break
        if opcode in (0x1, 0x2):
            if data[:1] == b"{":
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("type") == "resize":
                    set_winsize(master_fd, msg.get("cols"), msg.get("rows"))
                    continue
            try:
                os.write(master_fd, data)
            except OSError:
                break
    stop.set()
    for closer in (os.close,):
        try:
            closer(master_fd)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP handler: static files + /term upgrade + /healthz probe
# ---------------------------------------------------------------------------
class LabHandler(SimpleHTTPRequestHandler):
    server_version = "CTIALab/2.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        pass  # quiet

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def end_headers(self):
        # The lab folder is edited by the instructor between classes — never let
        # a browser serve a stale walkthrough from cache.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def _route(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        route = self._route()

        if route == "/healthz":
            payload = json.dumps(
                {"lab": LAB_ID, "port": self.server.server_address[1],
                 "folder": os.path.basename(HERE), "shell": "real-bash-pty"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if route == "/":
            self.path = "/" + PAGE
            return super().do_GET()

        if route == "/term":
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400, "Missing Sec-WebSocket-Key")
                return
            accept = ws_accept_key(key)
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            conn = self.connection
            try:
                handle_ws_terminal(conn)
            except Exception:
                pass
            try:
                conn.close()
            except OSError:
                pass
            return

        return super().do_GET()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
class LabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """Clients (and the browser's port probing) disconnect abruptly; that is
        normal and must not print a traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def bind_server(preferred):
    """Bind the first free port at or after `preferred`, so two open lab
    windows never fight over the same port."""
    last = None
    for offset in range(PORT_ATTEMPTS):
        port = preferred + offset
        try:
            return LabServer(("127.0.0.1", port), LabHandler), port
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                last = exc
                continue
            raise
    raise SystemExit("No free port found between %d and %d (%s)"
                     % (preferred, preferred + PORT_ATTEMPTS - 1, last))


def banner(url, port):
    line = "=" * 68
    print(line)
    print("  CTIA Lab  ·  %s" % LAB_ID)
    print("  Real bash terminal, real logs, nothing simulated.")
    print()
    print("  Open this URL:      %s" % url)
    print("  Lab folder:        %s" % HERE)
    print("  Terminal endpoint: ws://127.0.0.1:%d/term" % port)
    print()
    print("  Keep this window open while you work.  Ctrl+C to stop.")
    print(line)
    sys.stdout.flush()


def main(argv):
    port = DEFAULT_PORT
    open_browser = "--open" in argv
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    for arg in argv:
        if arg.isdigit():
            port = int(arg)
            break

    httpd, port = bind_server(port)
    url = "http://127.0.0.1:%d/" % port
    banner(url, port)

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        try:
            httpd.server_close()
        except OSError:
            pass


if __name__ == "__main__":
    main(sys.argv[1:])