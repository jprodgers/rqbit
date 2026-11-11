#!/usr/bin/env python3
"""Interop harness for BitTorrent v2 and hybrid torrents: rqbit against
qBittorrent and Transmission, on one machine, with a private tracker.

Nothing here touches a public swarm: every client listens on 127.0.0.1, DHT,
local discovery, peer exchange and port mapping are off, and peers find each
other through a tracker that this script runs itself.

    interop.py --rqbit PATH/TO/rqbit --mktorrent PATH/TO/mktorrent \
        [--work DIR] [--only 1a,3b,...] [--store-total 2G]

See README.md next to this file for what is needed and what each scenario
does. Prints a table of scenario, peers and result, and writes it to
`<work>/matrix.md`. Exits non-zero if a scenario failed.

Standard library only. Written with an AI model (Claude) for the JETCARP
project; see STATUS.md.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"


class Failed(Exception):
    """A scenario did not do what it should."""


class Unsupported(Exception):
    """The other client cannot do this at all (not a failure of rqbit)."""


# ------------------------------------------------------------------ tracker


class Tracker:
    """A minimal HTTP tracker (BEP 3, compact peers). Remembers every announce
    so that scenarios can check which info hashes a client announced."""

    def __init__(self, port: int):
        self.port = port
        self.url = f"http://{HOST}:{port}/announce"
        self.peers = {}  # info hash -> {(ip, port): last seen}
        self.announces = []  # (info hash, port, event)
        self.lock = threading.Lock()
        tracker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                query = urllib.parse.urlsplit(self.path).query
                q = urllib.parse.parse_qs(query, encoding="latin-1")
                try:
                    info_hash = q["info_hash"][0].encode("latin-1")
                    port = int(q["port"][0])
                except (KeyError, ValueError):
                    self.send_error(400)
                    return
                event = q.get("event", [""])[0]
                me = (self.client_address[0], port)
                with tracker.lock:
                    tracker.announces.append((info_hash, port, event))
                    swarm = tracker.peers.setdefault(info_hash, {})
                    if event == "stopped":
                        swarm.pop(me, None)
                    else:
                        swarm[me] = time.time()
                    others = [p for p in swarm if p != me]
                compact = b"".join(
                    socket.inet_aton(ip) + port.to_bytes(2, "big") for ip, port in others
                )
                body = b"d8:intervali5e5:peers%d:%se" % (len(compact), compact)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer((HOST, port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def announced(self, info_hash: bytes) -> set:
        """The ports that announced this (20 byte) info hash."""
        with self.lock:
            return {port for h, port, _ in self.announces if h == info_hash}


# ------------------------------------------------------------------ processes

PROCESSES = []


def spawn(name: str, argv, log_dir: str, env=None) -> subprocess.Popen:
    log = open(os.path.join(log_dir, name + ".log"), "ab")
    proc = subprocess.Popen(
        argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env
    )
    PROCESSES.append(proc)
    return proc


def stop(proc: subprocess.Popen, sig=signal.SIGTERM):
    if proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if proc in PROCESSES:
        PROCESSES.remove(proc)


def stop_all():
    for proc in list(PROCESSES):
        stop(proc)


def web(method: str, url: str, body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


def wait_for(what: str, check, timeout: float, interval=0.5):
    """Calls check() until it returns something true; fails after `timeout`."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = check()
            if last:
                return last
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            last = e
        time.sleep(interval)
    raise Failed(f"timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


# ------------------------------------------------------------------ clients


class Rqbit:
    name = "rqbit"

    def __init__(self, ctx, tag: str, api_port: int, listen_port: int):
        self.ctx = ctx
        self.tag = tag
        self.api = f"http://{HOST}:{api_port}"
        self.listen_port = listen_port
        self.base = os.path.join(ctx.work, "clients", tag)
        self.default_out = os.path.join(self.base, "out")
        self.session = os.path.join(self.base, "session")
        self.log = os.path.join(ctx.logs, tag + ".rqbit.log")
        os.makedirs(self.default_out, exist_ok=True)
        self.proc = None
        self.start()

    def start(self):
        argv = [
            self.ctx.rqbit,
            "--http-api-listen-addr", f"{HOST}:{self.api.rsplit(':', 1)[1]}",
            "--http-api-allow-create",
            "--listen-port", str(self.listen_port),
            "--disable-dht", "--disable-dht-persistence",
            "--disable-upnp-port-forward", "--disable-lsd",
            "--tracker-refresh-interval", "5s",
            "--log-file", self.log,
            "server", "start",
            "--persistence-location", self.session,
            "--fastresume",
            self.default_out,
        ]
        self.proc = spawn(self.tag, argv, self.ctx.logs)
        wait_for(f"{self.tag} HTTP API", lambda: web("GET", self.api + "/torrents")[0] == 200, 30)

    def stop(self, sig=signal.SIGTERM):
        stop(self.proc, sig)

    def create_and_seed(self, path: str, version: str, piece_length: int) -> bytes:
        """rqbit's own torrent creator; the torrent is seeded from `path`."""
        query = urllib.parse.urlencode(
            {
                "output": "torrent",
                "version": version,
                "piece_length": piece_length,
                "trackers": self.ctx.tracker.url,
            }
        )
        _, _, body = web(
            "POST", f"{self.api}/torrents/create?{query}", path.encode(), timeout=600
        )
        return body

    def add(self, torrent, folder: str, only_files=None) -> int:
        """`torrent` is .torrent bytes or a magnet link. Files are read from
        and written to `folder` (for a multi-file torrent: the files' folder
        itself, not its parent)."""
        params = {"overwrite": "true", "output_folder": folder}
        if only_files is not None:
            params["only_files"] = ",".join(str(i) for i in only_files)
        body = torrent.encode() if isinstance(torrent, str) else torrent
        try:
            _, _, resp = web(
                "POST",
                f"{self.api}/torrents?{urllib.parse.urlencode(params)}",
                body,
                timeout=600,
            )
        except urllib.error.HTTPError as e:
            raise Failed(f"rqbit refused the torrent: {e.code} {e.read()[:300]!r}")
        return json.loads(resp)["id"]

    def stats(self, torrent_id: int) -> dict:
        return json.loads(web("GET", f"{self.api}/torrents/{torrent_id}/stats/v1")[2])

    def details(self, torrent_id: int) -> dict:
        return json.loads(web("GET", f"{self.api}/torrents/{torrent_id}")[2])

    def wait_complete(self, torrent_id: int, timeout: float):
        def check():
            s = self.stats(torrent_id)
            if s.get("error"):
                raise Failed(f"rqbit torrent error: {s['error']}")
            return s.get("finished") and s.get("state") == "live"

        wait_for(f"{self.tag} to have the whole torrent", check, timeout)

    wait_seeding = wait_complete

    def log_count(self, needle: str) -> int:
        try:
            with open(self.log, "r", errors="replace") as f:
                return sum(needle in line for line in f)
        except FileNotFoundError:
            return 0


def multipart(fields: dict, files: dict):
    boundary = "----interop" + os.urandom(8).hex()
    out = []
    for name, value in fields.items():
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, data) in files.items():
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
            + data
            + b"\r\n"
        )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class QBittorrent:
    name = "qBittorrent"

    def __init__(self, ctx, tag: str, api_port: int, listen_port: int):
        self.ctx = ctx
        self.tag = tag
        self.api = f"http://{HOST}:{api_port}/api/v2"
        self.base = os.path.join(ctx.work, "clients", tag)
        conf_dir = os.path.join(self.base, "qBittorrent", "config")
        os.makedirs(conf_dir, exist_ok=True)
        with open(os.path.join(conf_dir, "qBittorrent.conf"), "w") as f:
            f.write(
                "[LegalNotice]\nAccepted=true\n\n"
                "[Preferences]\n"
                f"WebUI\\Address={HOST}\nWebUI\\Port={api_port}\n"
                "WebUI\\LocalHostAuth=false\nWebUI\\CSRFProtection=false\n"
                "WebUI\\HostHeaderValidation=false\nWebUI\\UseUPnP=false\n\n"
                "[BitTorrent]\n"
                f"Session\\Port={listen_port}\n"
                "Session\\DHTEnabled=false\nSession\\LSDEnabled=false\n"
                "Session\\PeXEnabled=false\nSession\\MultiConnectionsPerIp=true\n"
                "Session\\Preallocation=false\nSession\\AddTorrentPaused=false\n"
                "Session\\QueueingSystemEnabled=false\n\n"
                "[Network]\nPortForwardingEnabled=false\n"
            )
        self.proc = spawn(tag, [ctx.qbittorrent, f"--profile={self.base}"], ctx.logs)
        wait_for(
            f"{tag} Web API",
            lambda: web("GET", self.api + "/app/version")[0] == 200,
            60,
        )

    def version(self) -> str:
        return web("GET", self.api + "/app/version")[2].decode().strip()

    def stop(self):
        stop(self.proc)

    def add(self, torrent, save_path: str, skip_checking=False):
        """`save_path` is the folder that holds the torrent's folder (or its
        one file)."""
        os.makedirs(save_path, exist_ok=True)
        fields = {
            "savepath": save_path,
            "skip_checking": "true" if skip_checking else "false",
            "paused": "false",
            "contentLayout": "Original",
            "autoTMM": "false",
        }
        files = {}
        if isinstance(torrent, str):
            fields["urls"] = torrent
        else:
            files["torrents"] = ("t.torrent", torrent)
        body, content_type = multipart(fields, files)
        _, _, resp = web(
            "POST", self.api + "/torrents/add", body, {"Content-Type": content_type}
        )
        if resp.strip() != b"Ok.":
            raise Unsupported(f"qBittorrent did not take the torrent: {resp[:200]!r}")

    def info(self):
        torrents = json.loads(web("GET", self.api + "/torrents/info")[2])
        return torrents[0] if torrents else None

    def wait_complete(self, _id, timeout: float):
        def check():
            t = self.info()
            if not t:
                return False
            if t["state"] in ("error", "missingFiles"):
                raise Failed(f"qBittorrent torrent state: {t['state']}")
            return t["progress"] == 1 and t["state"] in (
                "uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP",
            )

        wait_for(f"{self.tag} to have the whole torrent", check, timeout)

    wait_seeding = wait_complete


class Transmission:
    name = "Transmission"

    def __init__(self, ctx, tag: str, rpc_port: int, listen_port: int):
        self.ctx = ctx
        self.tag = tag
        self.rpc = f"http://{HOST}:{rpc_port}/transmission/rpc"
        self.base = os.path.join(ctx.work, "clients", tag)
        self.downloads = os.path.join(self.base, "downloads")
        os.makedirs(self.downloads, exist_ok=True)
        argv = [
            ctx.transmission, "--foreground",
            "--config-dir", os.path.join(self.base, "config"),
            "--port", str(rpc_port), "--no-auth",
            "--rpc-bind-address", HOST,
            "--peerport", str(listen_port),
            "--download-dir", self.downloads,
            "--no-dht", "--no-lpd", "--no-utp", "--no-portmap",
            "--encryption-tolerated",
            "--log-level=info",
        ]
        self.session_id = ""
        self.proc = spawn(tag, argv, ctx.logs)
        wait_for(f"{tag} RPC", lambda: self.call("session-get") is not None, 30)

    def stop(self):
        stop(self.proc)

    def call(self, method: str, **arguments):
        body = json.dumps({"method": method, "arguments": arguments}).encode()
        for _ in range(2):
            try:
                _, _, resp = web(
                    "POST", self.rpc, body, {"X-Transmission-Session-Id": self.session_id}
                )
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
                self.session_id = e.headers["X-Transmission-Session-Id"]
                continue
            reply = json.loads(resp)
            if reply.get("result") != "success":
                raise Unsupported(f"Transmission: {reply.get('result')}")
            return reply.get("arguments", {})
        raise Failed("Transmission RPC kept answering 409")

    def version(self) -> str:
        return self.call("session-get")["version"]

    def add(self, torrent, save_path: str, skip_checking=False):
        os.makedirs(save_path, exist_ok=True)
        args = {"download-dir": save_path, "paused": False}
        if isinstance(torrent, str):
            args["filename"] = torrent
        else:
            args["metainfo"] = base64.b64encode(torrent).decode()
        self.call("torrent-add", **args)

    def info(self):
        fields = ["id", "name", "percentDone", "status", "error", "errorString", "leftUntilDone"]
        torrents = self.call("torrent-get", fields=fields)["torrents"]
        return torrents[0] if torrents else None

    def wait_complete(self, _id, timeout: float):
        def check():
            t = self.info()
            if not t:
                return False
            if t["error"]:
                raise Failed(f"Transmission torrent error: {t['errorString']}")
            # 5 and 6: queued to seed, seeding.
            return t["percentDone"] == 1 and t["status"] in (5, 6)

        wait_for(f"{self.tag} to have the whole torrent", check, timeout)

    wait_seeding = wait_complete


# ------------------------------------------------------------------ data


def run(argv, **kwargs) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise Failed(f"{' '.join(argv[:3])}... failed: {result.stderr.strip()[-400:]}")
    return result.stdout


def file_digests(root: str) -> dict:
    """relative path -> (size, sha256) of every file under root (or of root)."""
    out = {}
    if os.path.isfile(root):
        paths = [("", root)]
    else:
        paths = []
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                full = os.path.join(dirpath, n)
                paths.append((os.path.relpath(full, root), full))
    for rel, full in paths:
        h = hashlib.sha256()
        with open(full, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        out[rel] = (os.path.getsize(full), h.hexdigest())
    return out


def expect_same(source: str, copy: str, only=None):
    """The copy must have the source's files, byte for byte. With `only` (a
    set of relative paths), just those."""
    want = file_digests(source)
    if only is not None:
        want = {k: v for k, v in want.items() if k in only}
    if not os.path.exists(copy):
        raise Failed(f"nothing was written to {copy}")
    have = file_digests(copy)
    # Empty files need not be created by every client.
    missing = [k for k, v in want.items() if k not in have and v[0] > 0]
    wrong = [k for k, v in want.items() if k in have and have[k] != v]
    if missing or wrong:
        raise Failed(
            f"{len(missing)} file(s) missing, {len(wrong)} differ "
            f"(e.g. {(missing + wrong)[0]!r}) of {len(want)}"
        )
    return len(want)


class Context:
    pass


class Data:
    """A data set and its torrents."""

    def __init__(self, ctx, name: str, root: str, piece_length: int):
        self.ctx = ctx
        self.name = name
        self.root = root  # the folder (or file) the torrent is made of
        self.parent = os.path.dirname(root)
        self.piece_length = piece_length
        self.torrents = {}

    def torrent(self, creator: str, mode: str) -> bytes:
        """creator: 'crate' (this repository's create_torrent crate) or
        'libtorrent' (the library under qBittorrent). mode: hybrid or v2."""
        key = (creator, mode)
        if key not in self.torrents:
            out = os.path.join(self.ctx.work, "torrents", f"{self.name}.{creator}.{mode}.torrent")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if creator == "crate":
                argv = [
                    self.ctx.mktorrent, self.root,
                    "--piece-length", str(self.piece_length),
                    "--tracker", self.ctx.tracker.url, "-o", out,
                ]
                if mode == "v2":
                    argv.append("--v2-only")
                run(argv)
            else:
                run(
                    [
                        self.ctx.lt_python, os.path.join(HERE, "lt_create.py"), self.root,
                        "--piece-length", str(self.piece_length), "--mode", mode,
                        "--tracker", self.ctx.tracker.url, "-o", out,
                    ]
                )
            with open(out, "rb") as f:
                self.torrents[key] = f.read()
        return self.torrents[key]


def info_hashes(torrent: bytes):
    """(v1 info hash or None, v2 info hash or None) of .torrent bytes."""
    sys.path.insert(0, os.path.dirname(HERE))
    import bep52_ref

    meta, raw = bep52_ref.bdecode(torrent)
    info = meta[b"info"]
    v1 = hashlib.sha1(raw).digest() if b"pieces" in info else None
    v2 = hashlib.sha256(raw).digest() if info.get(b"meta version") == 2 else None
    return v1, v2


def magnet(torrent: bytes, tracker_url: str, v1=True, v2=True) -> str:
    h1, h2 = info_hashes(torrent)
    parts = []
    if v1 and h1:
        parts.append("xt=urn:btih:" + h1.hex())
    if v2 and h2:
        parts.append("xt=urn:btmh:1220" + h2.hex())
    parts.append("tr=" + urllib.parse.quote(tracker_url, safe=""))
    return "magnet:?" + "&".join(parts)


# ------------------------------------------------------------------ scenarios


class Ports:
    def __init__(self, base: int):
        self.next = base

    def take(self) -> int:
        self.next += 1
        return self.next


def content_root(client, save_path: str, data: Data) -> str:
    """Where a qBittorrent or Transmission download of `data` ends up."""
    return os.path.join(save_path, os.path.basename(data.root))


def with_padding_files(ctx, data: Data, torrent: bytes, tag: str) -> str:
    """A copy of `data` (hard links) that also has the torrent's BEP 47
    padding files as real files of zeros; returns the folder holding it.

    Transmission 4.0 treats padding files as ordinary files: it writes them
    when it downloads, and without them it is not a complete seed."""
    sys.path.insert(0, os.path.dirname(HERE))
    import bep52_ref

    info = bep52_ref.bdecode(torrent)[0][b"info"]
    parent = os.path.join(ctx.work, "clients", tag, "seed")
    root = os.path.join(parent, os.path.basename(data.root))
    for dirpath, _dirs, names in os.walk(data.root):
        target = os.path.join(root, os.path.relpath(dirpath, data.root))
        os.makedirs(target, exist_ok=True)
        for n in names:
            os.link(os.path.join(dirpath, n), os.path.join(target, n))
    for f in info.get(b"files", []):
        if b"p" in f.get(b"attr", b""):
            path = os.path.join(root, *(c.decode() for c in f[b"path"]))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as out:
                out.truncate(f[b"length"])
    return parent


def seed_with(ctx, kind, data: Data, torrent: bytes, tag: str, timeout=300):
    """Starts a client of `kind` seeding `data` from where it is."""
    if kind is Rqbit:
        client = Rqbit(ctx, tag, ctx.ports.take(), ctx.ports.take())
        folder = data.root if os.path.isdir(data.root) else data.parent
        client.torrent_id = client.add(torrent, folder)
    elif kind is Transmission:
        client = kind(ctx, tag, ctx.ports.take(), ctx.ports.take())
        client.add(torrent, with_padding_files(ctx, data, torrent, tag))
        client.torrent_id = None
    else:
        client = kind(ctx, tag, ctx.ports.take(), ctx.ports.take())
        client.add(torrent, data.parent)
        client.torrent_id = None
    client.wait_seeding(client.torrent_id, timeout)
    return client


def download_with(ctx, kind, data: Data, torrent, tag: str, timeout=300, only_files=None):
    """Starts a client of `kind`, downloads `torrent` (bytes or magnet), and
    returns (client, folder the content is in)."""
    if kind is Rqbit:
        client = Rqbit(ctx, tag, ctx.ports.take(), ctx.ports.take())
        dest = os.path.join(client.base, "dl", os.path.basename(data.root))
        folder = dest if os.path.isdir(data.root) else os.path.dirname(dest)
        client.torrent_id = client.add(torrent, folder, only_files=only_files)
    else:
        client = kind(ctx, tag, ctx.ports.take(), ctx.ports.take())
        save = os.path.join(client.base, "dl")
        client.add(torrent, save)
        client.torrent_id = None
        dest = content_root(client, save, data)
    client.wait_complete(client.torrent_id, timeout)
    return client, dest


def s1a(ctx):
    """rqbit creates a hybrid torrent and seeds it; qBittorrent downloads it."""
    seed = Rqbit(ctx, "s1a-rqbit", ctx.ports.take(), ctx.ports.take())
    torrent = seed.create_and_seed(ctx.small.root, "hybrid", ctx.small.piece_length)
    v1, v2 = info_hashes(torrent)
    if not (v1 and v2):
        raise Failed("rqbit did not make a hybrid torrent")
    _, dest = download_with(ctx, QBittorrent, ctx.small, torrent, "s1a-qbt")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s1b(ctx):
    """rqbit creates a v2-only torrent and seeds it; qBittorrent downloads it."""
    seed = Rqbit(ctx, "s1b-rqbit", ctx.ports.take(), ctx.ports.take())
    torrent = seed.create_and_seed(ctx.small.root, "v2", ctx.small.piece_length)
    v1, v2 = info_hashes(torrent)
    if v1 or not v2:
        raise Failed("rqbit did not make a v2-only torrent")
    _, dest = download_with(ctx, QBittorrent, ctx.small, torrent, "s1b-qbt")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s1c(ctx):
    """The create_torrent crate makes a hybrid torrent; rqbit seeds it;
    qBittorrent and Transmission download it."""
    torrent = ctx.small.torrent("crate", "hybrid")
    seed_with(ctx, Rqbit, ctx.small, torrent, "s1c-rqbit")
    _, dest = download_with(ctx, QBittorrent, ctx.small, torrent, "s1c-qbt")
    n = expect_same(ctx.small.root, dest)
    _, dest = download_with(ctx, Transmission, ctx.small, torrent, "s1c-tr")
    expect_same(ctx.small.root, dest)
    return f"{n} files identical in both"


def s1d(ctx):
    """rqbit's hybrid torrent of a folder equals the create_torrent crate's
    and libtorrent's (same info hashes)."""
    seed = Rqbit(ctx, "s1d-rqbit", ctx.ports.take(), ctx.ports.take())
    ours = info_hashes(seed.create_and_seed(ctx.small.root, "hybrid", ctx.small.piece_length))
    crate = info_hashes(ctx.small.torrent("crate", "hybrid"))
    lt = info_hashes(ctx.small.torrent("libtorrent", "hybrid"))
    if not (ours == crate == lt):
        raise Failed(f"info hashes differ: rqbit {ours}, crate {crate}, libtorrent {lt}")
    return "same v1 and v2 info hashes from all three"


def s2(ctx, mode: str):
    torrent = ctx.small.torrent("libtorrent", mode)
    seed_with(ctx, QBittorrent, ctx.small, torrent, f"s2{mode}-qbt")
    client, dest = download_with(ctx, Rqbit, ctx.small, torrent, f"s2{mode}-rqbit")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s2a(ctx):
    """libtorrent 2.0 (qBittorrent's library) creates a hybrid torrent;
    qBittorrent seeds it; rqbit downloads it."""
    return s2(ctx, "hybrid")


def s2b(ctx):
    """The same with a v2-only torrent."""
    return s2(ctx, "v2")


def s3a(ctx):
    """qBittorrent seeds a v2-only torrent; rqbit starts from a magnet link
    with only the v2 hash, so the piece layers come by hash request."""
    torrent = ctx.small.torrent("libtorrent", "v2")
    seed_with(ctx, QBittorrent, ctx.small, torrent, "s3a-qbt")
    link = magnet(torrent, ctx.tracker.url, v1=False)
    client, dest = download_with(ctx, Rqbit, ctx.small, link, "s3a-rqbit")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s3b(ctx):
    """rqbit seeds a v2-only torrent; qBittorrent starts from a magnet link
    with only the v2 hash."""
    torrent = ctx.small.torrent("crate", "v2")
    seed_with(ctx, Rqbit, ctx.small, torrent, "s3b-rqbit")
    link = magnet(torrent, ctx.tracker.url, v1=False)
    _, dest = download_with(ctx, QBittorrent, ctx.small, link, "s3b-qbt")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s3c(ctx):
    """qBittorrent seeds a hybrid torrent; rqbit starts from a magnet link
    with only the v2 hash, and must find out it is a hybrid."""
    torrent = ctx.small.torrent("libtorrent", "hybrid")
    seed_with(ctx, QBittorrent, ctx.small, torrent, "s3c-qbt")
    link = magnet(torrent, ctx.tracker.url, v1=False)
    client, dest = download_with(ctx, Rqbit, ctx.small, link, "s3c-rqbit")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s4a(ctx):
    """A hybrid swarm where the only seed is v1-only (Transmission): rqbit
    downloads over v1."""
    torrent = ctx.small.torrent("crate", "hybrid")
    seed_with(ctx, Transmission, ctx.small, torrent, "s4a-tr")
    _, dest = download_with(ctx, Rqbit, ctx.small, torrent, "s4a-rqbit")
    n = expect_same(ctx.small.root, dest)
    return f"{n} files identical"


def s4b(ctx):
    """A hybrid swarm: rqbit seeds; a v1-only peer (Transmission) and a v2
    peer (qBittorrent) download at the same time. rqbit must announce under
    both info hashes."""
    torrent = ctx.small.torrent("crate", "hybrid")
    seed = seed_with(ctx, Rqbit, ctx.small, torrent, "s4b-rqbit")
    qbt = QBittorrent(ctx, "s4b-qbt", ctx.ports.take(), ctx.ports.take())
    tr = Transmission(ctx, "s4b-tr", ctx.ports.take(), ctx.ports.take())
    qbt_save = os.path.join(qbt.base, "dl")
    tr_save = os.path.join(tr.base, "dl")
    qbt.add(torrent, qbt_save)
    tr.add(torrent, tr_save)
    qbt.wait_complete(None, 300)
    tr.wait_complete(None, 300)
    n = expect_same(ctx.small.root, content_root(qbt, qbt_save, ctx.small))
    expect_same(ctx.small.root, content_root(tr, tr_save, ctx.small))
    v1, v2 = info_hashes(torrent)
    announced_v1 = seed.listen_port in ctx.tracker.announced(v1)
    announced_v2 = seed.listen_port in ctx.tracker.announced(v2[:20])
    if not (announced_v1 and announced_v2):
        raise Failed(f"rqbit announced v1: {announced_v1}, truncated v2: {announced_v2}")
    return f"{n} files identical in both; announced under both hashes"


def s4c(ctx):
    """A hybrid swarm with a v1-only seed (Transmission) and a v2 seed
    (qBittorrent): rqbit downloads from both at once."""
    torrent = ctx.small.torrent("libtorrent", "hybrid")
    seed_with(ctx, Transmission, ctx.small, torrent, "s4c-tr")
    seed_with(ctx, QBittorrent, ctx.small, torrent, "s4c-qbt")
    client, dest = download_with(ctx, Rqbit, ctx.small, torrent, "s4c-rqbit")
    n = expect_same(ctx.small.root, dest)
    peers = json.loads(
        web("GET", f"{client.api}/torrents/{client.torrent_id}/peer_stats?state=all")[2]
    )
    used = sum(
        1 for p in peers.get("peers", {}).values() if p.get("counters", {}).get("fetched_bytes", 0) > 0
    )
    return f"{n} files identical; data fetched from {used} peer(s)"


def corrupt_copy(ctx, tag: str) -> str:
    """A copy of the small data set with bytes flipped in its three largest
    files (same sizes)."""
    copy_parent = os.path.join(ctx.work, "corrupt", tag)
    copy = os.path.join(copy_parent, os.path.basename(ctx.small.root))
    shutil.copytree(ctx.small.root, copy)
    sizes = sorted(
        ((os.path.getsize(os.path.join(d, n)), os.path.join(d, n))
         for d, _s, names in os.walk(copy) for n in names),
        reverse=True,
    )
    for size, path in sizes[:3]:
        with open(path, "r+b") as f:
            for offset in range(0, size, max(size // 8, 1)):
                f.seek(offset)
                byte = f.read(1)
                f.seek(offset)
                f.write(bytes([byte[0] ^ 0xFF]))
    return copy_parent


def s5(ctx, mode: str):
    torrent = ctx.small.torrent("libtorrent", mode)
    good = seed_with(ctx, QBittorrent, ctx.small, torrent, f"s5{mode}-good")
    bad = QBittorrent(ctx, f"s5{mode}-bad", ctx.ports.take(), ctx.ports.take())
    # The bad seed is told not to check, so it offers the corrupt bytes.
    bad.add(torrent, corrupt_copy(ctx, f"s5{mode}"), skip_checking=True)
    bad.wait_seeding(None, 60)
    client, dest = download_with(ctx, Rqbit, ctx.small, torrent, f"s5{mode}-rqbit", timeout=600)
    n = expect_same(ctx.small.root, dest)
    failures = client.log_count("hash does not match")
    if failures == 0:
        return f"{n} files identical, but rqbit never received a corrupt piece (not exercised)"
    return f"{n} files identical; {failures} corrupt piece(s) detected and fetched again"


def s5a(ctx):
    """Two qBittorrent seeds of a hybrid torrent, one with corrupted files:
    rqbit must detect the bad pieces and still end with the right bytes."""
    return s5(ctx, "hybrid")


def s5b(ctx):
    """The same with a v2-only torrent (the merkle check alone)."""
    return s5(ctx, "v2")


def s6(ctx, mode: str):
    torrent = ctx.small.torrent("libtorrent", mode)
    seed_with(ctx, QBittorrent, ctx.small, torrent, f"s6{mode}-qbt")
    lister = Rqbit(ctx, f"s6{mode}-rqbit", ctx.ports.take(), ctx.ports.take())
    dest = os.path.join(lister.base, "dl", os.path.basename(ctx.small.root))
    # List the files first to choose by index, as an application would.
    listing = json.loads(
        web(
            "POST",
            f"{lister.api}/torrents?list_only=true&overwrite=true",
            torrent,
        )[2]
    )
    files = listing["details"]["files"]
    real = [
        (i, f) for i, f in enumerate(files)
        if not f.get("attributes", {}).get("padding") and f["length"] > 0
    ]
    chosen = real[1::3]
    wanted = {os.path.join(*f["components"]) for _, f in chosen}
    torrent_id = lister.add(torrent, dest, only_files=[i for i, _ in chosen])
    lister.wait_complete(torrent_id, 300)
    n = expect_same(ctx.small.root, dest, only=wanted)
    # Files that were not chosen must not be complete on disk.
    source = file_digests(ctx.small.root)
    have = file_digests(dest)
    extra = [k for k, v in have.items() if k not in wanted and v[0] > 0 and source.get(k) == v]
    stray = sum(v[0] for k, v in have.items() if k not in wanted)
    note = f"{n} chosen files identical; {len(extra)} other file(s) complete, {stray} stray bytes on disk"
    if extra:
        raise Failed(note)
    return note


def s6a(ctx):
    """rqbit downloads only some files of a hybrid torrent."""
    return s6(ctx, "hybrid")


def s6b(ctx):
    """rqbit downloads only some files of a v2-only torrent."""
    return s6(ctx, "v2")


def s7(ctx, mode: str):
    data = ctx.store
    torrent = data.torrent("crate", mode)
    client = Rqbit(ctx, f"s7{mode}-rqbit", ctx.ports.take(), ctx.ports.take())
    started = time.time()
    torrent_id = client.add(torrent, data.root)
    client.wait_seeding(torrent_id, 900)
    first = time.time() - started
    time.sleep(2)  # let the session and the bitfield reach the disk
    restarts = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        client.stop(sig)
        started = time.time()
        client.start()
        client.wait_seeding(torrent_id, 900)
        restarts.append(time.time() - started)
        time.sleep(2)
    size = sum(v[0] for v in file_digests(data.root).values()) / (1 << 30)
    note = (
        f"{size:.1f} GiB: first check {first:.1f} s; restart after a clean stop "
        f"{restarts[0]:.1f} s, after kill -9 {restarts[1]:.1f} s"
    )
    if max(restarts) > max(first / 3, 3):
        raise Failed(note + " (a restart looks like a full recheck)")
    # It must still serve the data after the restarts.
    _, dest = download_with(ctx, QBittorrent, data, torrent, f"s7{mode}-qbt", timeout=900)
    expect_same(data.root, dest)
    return note + "; qBittorrent then downloaded it all"


def s7a(ctx):
    """rqbit seeds an existing folder (hybrid), is stopped and killed, and
    restarts without hashing everything again."""
    return s7(ctx, "hybrid")


def s7b(ctx):
    """The same with a v2-only torrent."""
    return s7(ctx, "v2")


def s8a(ctx):
    """JETCARP-shaped store (objects/sha256/ab/<64 hex>, about 200 files from
    0 bytes to 300 MiB), hybrid torrent from the create_torrent crate: rqbit
    seeds, qBittorrent downloads; every file's pieces root matches the
    independent Python computation."""
    data = ctx.store
    torrent = data.torrent("crate", "hybrid")
    roots_ref = run(
        [sys.executable, os.path.join(os.path.dirname(HERE), "bep52_ref.py"), "roots",
         data.root, "--piece-length", str(data.piece_length)]
    )
    roots_crate = run(
        [ctx.mktorrent, data.root, "--piece-length", str(data.piece_length), "--roots"]
    )
    crate_lines = [l for l in roots_crate.splitlines() if "  " in l and not l.startswith(("v1", "v2", "magnet"))]
    if crate_lines != roots_ref.splitlines():
        raise Failed("pieces roots differ from the Python reference")
    seed_with(ctx, Rqbit, data, torrent, "s8a-rqbit", timeout=900)
    _, dest = download_with(ctx, QBittorrent, data, torrent, "s8a-qbt", timeout=1800)
    n = expect_same(data.root, dest)
    return f"{n} files identical; {len(crate_lines)} pieces roots match the reference"


def s8b(ctx):
    """The same store: qBittorrent seeds, rqbit downloads."""
    data = ctx.store
    torrent = data.torrent("crate", "hybrid")
    seed_with(ctx, QBittorrent, data, torrent, "s8b-qbt", timeout=900)
    _, dest = download_with(ctx, Rqbit, data, torrent, "s8b-rqbit", timeout=1800)
    n = expect_same(data.root, dest)
    return f"{n} files identical"


def s9a(ctx):
    """Transmission is given a v2-only torrent (expected: it cannot use it)."""
    torrent = ctx.small.torrent("crate", "v2")
    tr = Transmission(ctx, "s9a-tr", ctx.ports.take(), ctx.ports.take())
    tr.add(torrent, os.path.join(tr.base, "dl"))
    return "Transmission accepted a v2-only torrent"


SCENARIOS = [
    ("1a", "rqbit creates+seeds hybrid", "qBittorrent {qbt} downloads", s1a),
    ("1b", "rqbit creates+seeds v2-only", "qBittorrent {qbt} downloads", s1b),
    ("1c", "crate creates hybrid, rqbit seeds", "qBittorrent {qbt} and Transmission {tr} download", s1c),
    ("1d", "same hybrid from rqbit, crate, libtorrent", "libtorrent {lt}", s1d),
    ("2a", "rqbit downloads hybrid (.torrent)", "qBittorrent {qbt} seeds, libtorrent {lt} created", s2a),
    ("2b", "rqbit downloads v2-only (.torrent)", "qBittorrent {qbt} seeds, libtorrent {lt} created", s2b),
    ("3a", "rqbit downloads v2-only from a btmh magnet", "qBittorrent {qbt} seeds", s3a),
    ("3b", "rqbit seeds v2-only", "qBittorrent {qbt} downloads from a btmh magnet", s3b),
    ("3c", "rqbit downloads a hybrid from a btmh-only magnet", "qBittorrent {qbt} seeds", s3c),
    ("4a", "rqbit downloads hybrid from a v1-only seed", "Transmission {tr} seeds", s4a),
    ("4b", "rqbit seeds hybrid to a v1 and a v2 peer", "Transmission {tr} and qBittorrent {qbt} download", s4b),
    ("4c", "rqbit downloads hybrid from a v1 and a v2 seed", "Transmission {tr} and qBittorrent {qbt} seed", s4c),
    ("5a", "rqbit downloads hybrid, one seed corrupt", "two qBittorrent {qbt} seeds", s5a),
    ("5b", "rqbit downloads v2-only, one seed corrupt", "two qBittorrent {qbt} seeds", s5b),
    ("6a", "rqbit downloads some files of a hybrid", "qBittorrent {qbt} seeds", s6a),
    ("6b", "rqbit downloads some files of a v2-only", "qBittorrent {qbt} seeds", s6b),
    ("7a", "rqbit restarts while seeding (hybrid store)", "qBittorrent {qbt} downloads afterwards", s7a),
    ("7b", "rqbit restarts while seeding (v2-only store)", "qBittorrent {qbt} downloads afterwards", s7b),
    ("8a", "JETCARP store: rqbit seeds hybrid", "qBittorrent {qbt} downloads", s8a),
    ("8b", "JETCARP store: rqbit downloads hybrid", "qBittorrent {qbt} seeds", s8b),
    ("9a", "v2-only torrent given to a v1-only client", "Transmission {tr}", s9a),
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--rqbit", required=True, help="the rqbit binary under test")
    p.add_argument("--mktorrent", required=True, help="the create_torrent crate's mktorrent example")
    p.add_argument("--qbittorrent", default="qbittorrent-nox")
    p.add_argument("--transmission", default="transmission-daemon")
    p.add_argument("--lt-python", default="/usr/bin/python3.12",
                   help="a Python with the libtorrent bindings qBittorrent is built on")
    p.add_argument("--work", default="/tmp/bep52-interop")
    p.add_argument("--only", help="comma separated scenario ids, e.g. 1a,3b")
    p.add_argument("--store", help="use this existing folder as the JETCARP-shaped store")
    p.add_argument("--store-total", default="2G")
    p.add_argument("--port-base", type=int, default=21000)
    args = p.parse_args()

    ctx = Context()
    ctx.work = os.path.abspath(args.work)
    ctx.logs = os.path.join(ctx.work, "logs")
    ctx.rqbit = os.path.abspath(args.rqbit)
    ctx.mktorrent = os.path.abspath(args.mktorrent)
    ctx.qbittorrent = shutil.which(args.qbittorrent) or args.qbittorrent
    ctx.transmission = shutil.which(args.transmission) or args.transmission
    ctx.lt_python = args.lt_python
    ctx.ports = Ports(args.port_base)
    for sub in ("clients", "logs", "torrents", "corrupt"):
        shutil.rmtree(os.path.join(ctx.work, sub), ignore_errors=True)
    os.makedirs(ctx.logs, exist_ok=True)
    ctx.tracker = Tracker(args.port_base)

    make_store = os.path.join(os.path.dirname(HERE), "make_store.py")
    small = os.path.join(ctx.work, "data", "small", "store")
    if not os.path.exists(small):
        run([sys.executable, make_store, small, "--files", "40", "--total", "200M",
             "--max-file", "64M", "--seed", "2"])
    ctx.small = Data(ctx, "small", small, 256 * 1024)
    only = set(args.only.split(",")) if args.only else None
    needs_store = only is None or any(s[0] in ("7", "8") for s in only)
    if args.store:
        store = os.path.abspath(args.store)
    else:
        store = os.path.join(ctx.work, "data", "jetcarp", "store")
        if needs_store and not os.path.exists(store):
            run([sys.executable, make_store, store, "--files", "200", "--total", args.store_total,
                 "--max-file", "300M", "--also", "300M", "--seed", "3"])
    ctx.store = Data(ctx, "jetcarp", store, 1024 * 1024)

    versions = {"qbt": "?", "tr": "?", "lt": "?"}
    try:
        versions["lt"] = run([ctx.lt_python, "-c", "import libtorrent; print(libtorrent.version)"]).strip()
        probe = QBittorrent(ctx, "probe-qbt", ctx.ports.take(), ctx.ports.take())
        versions["qbt"] = probe.version().lstrip("v")
        probe.stop()
        probe = Transmission(ctx, "probe-tr", ctx.ports.take(), ctx.ports.take())
        versions["tr"] = probe.version().split(" ")[0]
        probe.stop()
    except Exception as e:  # noqa: BLE001
        print(f"warning: could not read client versions: {e}", file=sys.stderr)
    rqbit_version = run([ctx.rqbit, "--version"]).strip()

    rows = []
    failed = 0
    for sid, what, peers, func in SCENARIOS:
        if only and sid not in only:
            continue
        started = time.time()
        try:
            result, detail = "pass", func(ctx)
        except Unsupported as e:
            result, detail = "unsupported by peer", str(e)
        except Failed as e:
            result, detail = "FAIL", str(e)
            failed += 1
        except Exception as e:  # noqa: BLE001
            result, detail = "FAIL", f"harness error: {e!r}"
            traceback.print_exc()
            failed += 1
        finally:
            stop_all()
            # Downloads can be large; the logs are kept elsewhere.
            shutil.rmtree(os.path.join(ctx.work, "clients"), ignore_errors=True)
            shutil.rmtree(os.path.join(ctx.work, "corrupt"), ignore_errors=True)
        took = time.time() - started
        rows.append((sid, what, peers.format(**versions), result, detail))
        print(f"[{sid}] {result:19} {took:6.1f} s  {what}: {detail}", flush=True)

    lines = [
        f"rqbit under test: `{rqbit_version}`. Peers: qBittorrent {versions['qbt']} "
        f"(libtorrent {versions['lt']}), Transmission {versions['tr']}.",
        "",
        "| # | rqbit does | Peer | Result | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows]
    with open(os.path.join(ctx.work, "matrix.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        stop_all()
    sys.exit(code)
