#!/usr/bin/env python3
"""AirDrop Web: recibe archivos del iPhone (o de cualquier navegador) en esta PC por la red local.

Uso:
  python3 server.py             inicia el servidor
  python3 server.py url         muestra las direcciones para abrir en el iPhone
  python3 server.py new-token   genera un token nuevo (los enlaces anteriores dejan de funcionar)

Variables de entorno:
  AIRDROP_PORT     puerto (8765)
  AIRDROP_DIR      carpeta donde se guardan los archivos (~/Downloads/AirDrop)
  AIRDROP_NOTIFY   0 para no mostrar notificaciones de escritorio

Sin dependencias: solo la librería estándar de Python.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
PORT = int(os.environ.get("AIRDROP_PORT", "8765"))
DEST = Path(os.environ.get("AIRDROP_DIR", "~/Downloads/AirDrop")).expanduser()
NOTIFY = os.environ.get("AIRDROP_NOTIFY", "1") != "0"
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "airdrop-web"
TOKEN_FILE = CONF_DIR / "token"
HOST = socket.gethostname()
CHUNK = 1 << 20  # 1 MiB
TOKEN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # sin 0/o ni 1/l para escribirlo fácil
FILE_FIELDS = {"file", "files", "archivo", "archivos"}
STATIC = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "icon.png": ("icon.png", "image/png"),
    "icon.svg": ("icon.svg", "image/svg+xml"),
}
TOKEN = ""


class BadUpload(Exception):
    """El cliente mandó algo que no se puede interpretar."""


# ---------------------------------------------------------------- utilidades

def load_token(regenerate: bool = False) -> str:
    if not regenerate:
        try:
            token = TOKEN_FILE.read_text().strip()
            if re.fullmatch(r"[a-z0-9]{4,64}", token):
                return token
        except FileNotFoundError:
            pass
    token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(6))
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def pretty(path: Path) -> str:
    home = str(Path.home())
    return "~" + str(path)[len(home):] if str(path).startswith(home) else str(path)


def clean_name(raw: str) -> str:
    """Deja solo el nombre del archivo (sin rutas ni caracteres de control)."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c if c.isprintable() else (" " if c.isspace() else "") for c in name)
    name = name.strip().lstrip(".").strip()
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:
        stem, ext = name, ""
    stem = stem.encode()[: 200 - len(ext.encode())].decode("utf-8", "ignore").rstrip()
    return stem + ext if stem else ""


def guess_ext(head: bytes) -> str:
    """Extensión a partir de los primeros bytes, para archivos que llegan sin ella."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head.startswith(b"ID3"):
        return ".mp3"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"):
            return ".heic"
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand == b"qt  ":
            return ".mov"
        if brand in (b"M4A ", b"M4B "):
            return ".m4a"
        return ".mp4"
    for cut in range(4):  # ¿texto UTF-8? (el último carácter puede venir cortado)
        try:
            text = head[: len(head) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text and all(c.isprintable() or c.isspace() for c in text):
            return ".txt"
        break
    return ""


def place(tmp: Path, name: str) -> Path:
    """Mueve tmp a DEST/name sin pisar nada: 'foto.jpg', 'foto (2).jpg', ..."""
    stem, ext = os.path.splitext(name)
    for i in range(1, 100_000):
        target = DEST / (name if i == 1 else f"{stem} ({i}){ext}")
        try:
            os.link(tmp, target)  # atómico: falla si ya existe
        except FileExistsError:
            continue
        except OSError:  # sistema de archivos sin hardlinks
            if target.exists():
                continue
            os.replace(tmp, target)
            return target
        tmp.unlink()
        return target
    raise OSError("demasiados archivos con el mismo nombre")


def lan_ips() -> list[str]:
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"],
                             capture_output=True, text=True, timeout=3).stdout
        ifaces = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    skip = ("lo", "docker", "br-", "veth", "virbr")
    return [a["local"] for i in ifaces if not i.get("ifname", "").startswith(skip)
            for a in i.get("addr_info", []) if a.get("family") == "inet"]


def avahi_active() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"], timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def session_env() -> dict[str, str]:
    """Entorno gráfico actual (el servicio puede arrancar antes que Sway)."""
    env = dict(os.environ)
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return env
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in {"WAYLAND_DISPLAY", "DISPLAY", "SWAYSOCK", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE"} and value:
            env[key] = value
    return env


# ------------------------------------------------------------- notificaciones

class Notifier:
    """Agrupa los archivos que llegan seguidos en una sola notificación."""

    def __init__(self, delay: float = 1.5):
        self.delay = delay
        self.lock = threading.Lock()
        self.pending: list[Path] = []
        self.timer: threading.Timer | None = None

    def add(self, paths: list[Path]) -> None:
        if not NOTIFY or not shutil.which("notify-send"):
            return
        with self.lock:
            self.pending.extend(paths)
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self._flush)
            self.timer.daemon = True
            self.timer.start()

    def _flush(self) -> None:
        with self.lock:
            paths, self.pending, self.timer = self.pending, [], None
        if not paths:
            return
        if len(paths) == 1:
            title, text = "Archivo recibido", paths[0].name
        else:
            title = f"{len(paths)} archivos recibidos"
            text = "\n".join(p.name for p in paths[:4])
            if len(paths) > 4:
                text += f"\n… y {len(paths) - 4} más"
        cmd = ["notify-send", "--app-name=AirDrop Web", "--icon=folder-download",
               "--action=default=Abrir", "--wait", title, html.escape(text, quote=False)]
        try:
            clicked = subprocess.run(cmd, capture_output=True, text=True, timeout=3600).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return
        if clicked == "default":
            self._open(paths[0] if len(paths) == 1 else DEST)

    @staticmethod
    def _open(target: Path) -> None:
        env = session_env()
        try:
            proc = subprocess.Popen(["xdg-open", str(target)], env=env, start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc.wait(timeout=5) != 0 and target != DEST:  # sin app para ese tipo: abre la carpeta
                subprocess.Popen(["xdg-open", str(DEST)], env=env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass  # la app quedó abierta: todo bien
        except OSError:
            pass


NOTIFIER = Notifier()


# ------------------------------------------------------------ recepción

class Body:
    """Lee el cuerpo de la petición por bloques (Content-Length o chunked)."""

    def __init__(self, rfile, headers):
        self.rfile = rfile
        self.chunked = "chunked" in headers.get("Transfer-Encoding", "").lower()
        self.remaining = 0 if self.chunked else int(headers.get("Content-Length") or 0)
        self.chunk_left = 0
        self.done = False

    def read(self, size: int = CHUNK) -> bytes:
        if self.done:
            return b""
        if not self.chunked:
            if self.remaining <= 0:
                self.done = True
                return b""
            data = self.rfile.read(min(size, self.remaining))
            if not data:
                raise ConnectionError("la conexión se cortó")
            self.remaining -= len(data)
            return data
        if self.chunk_left == 0:
            line = self.rfile.readline(1024)
            if not line:
                raise ConnectionError("la conexión se cortó")
            self.chunk_left = int(line.split(b";", 1)[0].strip(), 16)
            if self.chunk_left == 0:  # último chunk; saltar trailers
                while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                self.done = True
                return b""
        data = self.rfile.read(min(size, self.chunk_left))
        if not data:
            raise ConnectionError("la conexión se cortó")
        self.chunk_left -= len(data)
        if self.chunk_left == 0:
            self.rfile.readline(8)  # CRLF que cierra el chunk
        return data


class Incoming:
    """Archivo en camino: se escribe como .part y se renombra solo si llega completo."""

    def __init__(self, name: str):
        DEST.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.tmp = DEST / f".{secrets.token_hex(6)}.part"
        self.fh = open(self.tmp, "wb")
        self.head = b""
        self.size = 0

    def write(self, data) -> None:
        if len(self.head) < 16:
            self.head += bytes(data[: 16 - len(self.head)])
        self.fh.write(data)
        self.size += len(data)

    def abort(self) -> None:
        self.fh.close()
        self.tmp.unlink(missing_ok=True)

    def finish(self) -> Path | None:
        self.fh.close()
        if not self.name and self.size == 0:  # campo de archivo vacío
            self.tmp.unlink(missing_ok=True)
            return None
        name = self.name or time.strftime("Recibido %Y-%m-%d %H-%M-%S")  # sin puntos: no es extensión
        if not os.path.splitext(name)[1]:
            name += guess_ext(self.head)
        return place(self.tmp, name)


def receive_raw(body: Body, name: str) -> list[Path]:
    sink = Incoming(clean_name(name))
    try:
        while data := body.read():
            sink.write(data)
    except BaseException:
        sink.abort()
        raise
    path = sink.finish()
    return [path] if path else []


def parse_disposition(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for m in re.finditer(r';\s*([\w*.-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]*))', value):
        key = m.group(1).lower()
        params[key] = re.sub(r"\\(.)", r"\1", m.group(2)) if m.group(2) is not None else m.group(3).strip()
    if "filename*" in params:  # RFC 5987: filename*=UTF-8''nombre%20codificado
        charset, _, encoded = params["filename*"].partition("'")
        _, _, encoded = encoded.partition("'")
        try:
            params["filename"] = urllib.parse.unquote(encoded, encoding=charset or "utf-8")
        except LookupError:
            pass
    return params


def multipart_boundary(content_type: str) -> bytes | None:
    if not content_type.lower().startswith("multipart/form-data"):
        return None
    m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.I)
    if not m:
        raise BadUpload("Formulario sin boundary")
    return (m.group(1) or m.group(2)).encode("latin-1")


def receive_multipart(body: Body, boundary: bytes) -> list[Path]:
    """Parser multipart/form-data en streaming (no carga archivos en memoria)."""
    delim = b"--" + boundary
    sep = b"\r\n" + delim
    keep = len(sep) - 1
    buf = bytearray()
    saved: list[Path] = []

    def more() -> None:
        data = body.read()
        if not data:
            raise BadUpload("El envío llegó incompleto")
        buf.extend(data)

    while (i := buf.find(delim)) < 0:  # preámbulo
        del buf[: max(0, len(buf) - len(delim))]
        more()
    del buf[: i + len(delim)]

    while True:
        while len(buf) < 2:
            more()
        if buf[:2] == b"--":  # delimitador final
            break
        while (i := buf.find(b"\r\n")) < 0:
            more()
        del buf[: i + 2]

        while True:  # cabeceras de la parte
            if buf.startswith(b"\r\n"):
                raw_headers = b""
                del buf[:2]
                break
            i = buf.find(b"\r\n\r\n")
            if i >= 0:
                raw_headers = bytes(buf[:i])
                del buf[: i + 4]
                break
            if len(buf) > 65536:
                raise BadUpload("Cabeceras demasiado grandes")
            more()

        headers = {}
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            key, colon, value = line.partition(":")
            if colon:
                headers[key.strip().lower()] = value.strip()
        disp = parse_disposition(headers.get("content-disposition", ""))
        part_type = headers.get("content-type", "").lower()
        is_file = ("filename" in disp or disp.get("name", "").lower() in FILE_FIELDS
                   or (part_type != "" and not part_type.startswith("text/plain")))
        sink = Incoming(clean_name(disp.get("filename", ""))) if is_file else None

        try:
            while (i := buf.find(sep)) < 0:
                if len(buf) > keep:
                    if sink:
                        sink.write(buf[:-keep])
                    del buf[:-keep]
                more()
            if sink:
                sink.write(buf[:i])
            del buf[: i + len(sep)]
        except BaseException:
            if sink:
                sink.abort()
            raise
        if sink and (path := sink.finish()):
            saved.append(path)

    while body.read():  # epílogo
        pass
    return saved


# ----------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AirDropWeb/1.0"
    sys_version = ""
    timeout = 120  # segundos sin datos antes de cortar una conexión colgada

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.client_address[0].removeprefix('::ffff:')}  {fmt % args}\n")

    def reply(self, code: int, body: bytes | str = b"", ctype: str = "text/plain; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def route(self) -> tuple[str | None, urllib.parse.SplitResult]:
        """Devuelve la ruta tras el token, o None si el token no es válido."""
        url = urllib.parse.urlsplit(self.path)
        token, _, rest = url.path.lstrip("/").partition("/")
        valid = secrets.compare_digest(token.encode(), TOKEN.encode())
        return (rest if valid else None), url

    def do_GET(self):
        rest, url = self.route()
        if rest is None:
            return self.reply(404, "No encontrado\n")
        if url.path == f"/{TOKEN}":
            return self.reply(301, headers={"Location": f"/{TOKEN}/"})
        if rest == "info":
            info = {"host": HOST, "dest": pretty(DEST)}
            return self.reply(200, json.dumps(info), "application/json", {"Cache-Control": "no-store"})
        if rest in STATIC:
            fname, ctype = STATIC[rest]
            try:
                body = (WEB / fname).read_bytes()
            except FileNotFoundError:
                return self.reply(404, "No encontrado\n")
            cache = "max-age=86400" if fname.startswith("icon") else "no-cache"
            return self.reply(200, body, ctype, {"Cache-Control": cache})
        return self.reply(404, "No encontrado\n")

    do_HEAD = do_GET

    def do_POST(self):
        rest, url = self.route()
        if rest != "upload":
            self.close_connection = True
            return self.reply(404, "No encontrado\n")
        started = time.monotonic()
        try:
            body = Body(self.rfile, self.headers)
            boundary = multipart_boundary(self.headers.get("Content-Type", ""))
            if boundary:
                saved = receive_multipart(body, boundary)
            else:
                name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
                name = name or urllib.parse.parse_qs(url.query).get("name", [""])[0]
                saved = receive_raw(body, name)
        except BadUpload as e:
            self.close_connection = True
            return self.reply(400, f"{e}\n")
        except (OSError, ValueError) as e:  # conexión cortada, disco lleno, cuerpo mal formado...
            self.close_connection = True
            self.log_message("envío fallido: %s", e)
            try:
                self.reply(500, "La PC no pudo guardar el archivo\n")
            except OSError:
                pass
            return
        if not saved:
            return self.reply(400, "No llegó ningún archivo\n")
        secs = max(time.monotonic() - started, 1e-3)
        total = 0
        for path in saved:
            size = path.stat().st_size
            total += size
            self.log_message("recibido  %s  (%s)", path.name, human(size))
        if total > 4 * CHUNK:
            self.log_message("velocidad %s/s", human(total / secs))
        NOTIFIER.add(saved)
        names = ", ".join(p.name for p in saved)
        return self.reply(200, f"Enviado a {HOST}: {names}\n")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # sin getfqdn(): evita esperas de DNS al arrancar
        self.server_name, self.server_port = HOST, self.server_address[1]

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # el iPhone cerró una conexión inactiva: es normal, no ensuciar el log
        super().handle_error(request, client_address)


class Server6(Server):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)  # IPv4 + IPv6
        super().server_bind()


def base_urls(token: str) -> list[str]:
    urls = [f"http://{HOST}.local:{PORT}/{token}/"] if avahi_active() else []
    return urls + [f"http://{ip}:{PORT}/{token}/" for ip in lan_ips()]


def print_urls(token: str) -> None:
    urls = base_urls(token)
    if not urls:
        print("No hay conexión de red.")
        return
    print("Abre esto en el iPhone (Safari):")
    for url in urls:
        print(f"  {url}")
    print("\nURL para el Atajo de Compartir:")
    print(f"  {urls[0]}upload")
    if not avahi_active():
        print(f"\nTip: con 'sudo systemctl enable --now avahi-daemon' tendrás una dirección fija:"
              f"\n  http://{HOST}.local:{PORT}/{token}/  (no cambia aunque cambie tu IP)")
    if shutil.which("qrencode"):
        print()
        subprocess.run(["qrencode", "-t", "ansiutf8", "-m", "2", urls[0]])


def serve() -> None:
    global TOKEN
    TOKEN = load_token()
    DEST.mkdir(parents=True, exist_ok=True)
    for stale in DEST.glob(".*.part"):  # restos de envíos interrumpidos
        if re.fullmatch(r"\.[0-9a-f]{12}\.part", stale.name):
            stale.unlink(missing_ok=True)
    try:
        server = Server6(("::", PORT), Handler)
    except OSError:
        server = Server(("0.0.0.0", PORT), Handler)
    print(f"AirDrop Web escuchando en el puerto {PORT} · guardando en {pretty(DEST)}\n")
    print_urls(TOKEN)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "url":
        print_urls(load_token())
    elif cmd == "new-token":
        print_urls(load_token(regenerate=True))
        print("\nReinicia el servicio para usarlo: systemctl --user restart airdrop-web")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
