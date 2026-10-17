import os
import time
import socket
import _thread
import machine

# ============================================================
# Local-network OTA updates for MicroPython files
#
#   PUT  /files/<name>   upload a file (body = file contents)
#   GET  /files          list files on the device
#   GET  /files/<name>   download a file
#   POST /reset          reboot the device
#
# Every request needs the header  X-OTA-Token: <token>
#
# Runs in a background thread started from boot.py, so it keeps
# working even if main.py crashes.
# ============================================================

OTA_PORT = 8080
MAX_FILE_SIZE = 64 * 1024
SOCKET_TIMEOUT_SECONDS = 10

_ALLOWED_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"


def _valid_name(name):
    if not name or name.startswith(".") or len(name) > 32:
        return False
    for c in name:
        if c not in _ALLOWED_CHARS:
            return False
    return True


def _send(conn, status, body=""):
    reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
               404: "Not Found", 411: "Length Required",
               413: "Payload Too Large", 500: "Internal Server Error"}
    if not body.endswith("\n"):
        body += "\n"
    conn.send("HTTP/1.0 {} {}\r\nContent-Type: text/plain\r\n"
              "Content-Length: {}\r\nConnection: close\r\n\r\n".format(
                  status, reasons.get(status, ""), len(body)))
    conn.send(body)


def _read_request(conn):
    """Return (method, path, headers) after reading the header block."""
    f = conn.makefile("rb")
    request_line = f.readline().decode().strip()
    parts = request_line.split(" ")
    if len(parts) < 2:
        raise ValueError("bad request line")

    headers = {}
    while True:
        line = f.readline()
        if not line or line == b"\r\n" or line == b"\n":
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()

    return parts[0], parts[1], headers, f


def _read_body(f, length):
    data = bytearray()
    while len(data) < length:
        chunk = f.read(min(1024, length - len(data)))
        if not chunk:
            raise OSError("connection closed during upload")
        data.extend(chunk)
    return bytes(data)


def _handle_upload(conn, name, headers, f):
    if not _valid_name(name):
        return _send(conn, 400, "invalid file name")

    if "content-length" not in headers:
        return _send(conn, 411, "Content-Length required")

    length = int(headers["content-length"])
    if length > MAX_FILE_SIZE:
        return _send(conn, 413, "file too large")

    data = _read_body(f, length)

    # Refuse Python files that do not compile, so a typo cannot
    # replace a working main.py.
    if name.endswith(".py"):
        try:
            compile(data.decode(), name, "exec")
        except Exception as exc:
            return _send(conn, 400, "syntax error: {}".format(exc))

    # Write to a temporary file, then rename over the original so a
    # dropped connection never leaves a half-written file.
    tmp_name = name + ".tmp"
    with open(tmp_name, "wb") as out:
        out.write(data)

    try:
        os.remove(name)
    except OSError:
        pass
    os.rename(tmp_name, name)

    print("OTA: updated", name, "({} bytes)".format(length))
    _send(conn, 200, "saved {} ({} bytes)".format(name, length))


def _handle_download(conn, name):
    if not _valid_name(name):
        return _send(conn, 400, "invalid file name")

    try:
        size = os.stat(name)[6]
    except OSError:
        return _send(conn, 404, "no such file")

    conn.send("HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n"
              "Content-Length: {}\r\nConnection: close\r\n\r\n".format(size))

    with open(name, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            conn.send(chunk)


def _handle(conn, token):
    method, path, headers, f = _read_request(conn)

    if headers.get("x-ota-token") != token:
        return _send(conn, 401, "missing or wrong X-OTA-Token")

    if method == "PUT" and path.startswith("/files/"):
        return _handle_upload(conn, path[len("/files/"):], headers, f)

    if method == "GET" and path.startswith("/files/"):
        return _handle_download(conn, path[len("/files/"):])

    if method == "GET" and path == "/files":
        lines = []
        for name in sorted(os.listdir("/")):
            lines.append("{}\t{}".format(name, os.stat(name)[6]))
        return _send(conn, 200, "\n".join(lines))

    if method == "POST" and path == "/reset":
        _send(conn, 200, "resetting")
        conn.close()
        print("OTA: reset requested")
        time.sleep(0.5)
        machine.reset()

    _send(conn, 404, "unknown endpoint")


def _serve(token):
    addr = socket.getaddrinfo("0.0.0.0", OTA_PORT)[0][-1]
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(addr)
    server.listen(2)
    print("OTA: listening on port", OTA_PORT)

    while True:
        conn = None
        try:
            conn, _ = server.accept()
            conn.settimeout(SOCKET_TIMEOUT_SECONDS)
            _handle(conn, token)
        except Exception as exc:
            print("OTA error:", exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass


def start(token):
    _thread.stack_size(16 * 1024)
    _thread.start_new_thread(_serve, (token,))
