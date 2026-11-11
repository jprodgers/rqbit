#!/usr/bin/env python3
"""Downloads a torrent with a bare libtorrent session from one given peer.

A second way to put libtorrent on the other end of rqbit, without
qBittorrent: run it with a Python that has the libtorrent bindings of the
version to test (2.0.x is what qBittorrent ships; `pip install libtorrent`
gives 2.1.x).

    lt_download.py TORRENT SAVE_DIR --peer 127.0.0.1:PORT [--timeout 120]

Prints progress and the alerts that explain a stall, and exits 0 only if the
whole torrent arrived.
"""

import argparse
import sys
import time
import warnings

import libtorrent as lt

warnings.filterwarnings("ignore", category=DeprecationWarning)

p = argparse.ArgumentParser()
p.add_argument("torrent")
p.add_argument("save_dir")
p.add_argument("--peer", required=True)
p.add_argument("--timeout", type=float, default=120)
args = p.parse_args()

session = lt.session(
    {
        "listen_interfaces": "127.0.0.1:0",
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        "allow_multiple_connections_per_ip": True,
        # rqbit does not listen for uTP by default; do not wait for it.
        "enable_outgoing_utp": False,
        "enable_incoming_utp": False,
        "alert_mask": lt.alert.category_t.error_notification
        | lt.alert.category_t.peer_notification
        | lt.alert.category_t.status_notification,
    }
)
with open(args.torrent, "rb") as f:
    info = lt.torrent_info(f.read())
handle = session.add_torrent({"ti": info, "save_path": args.save_dir})
host, port = args.peer.rsplit(":", 1)
handle.connect_peer((host, int(port)))

print(f"libtorrent {lt.version}")
deadline = time.time() + args.timeout
seen = {}
last_progress = -1.0
while time.time() < deadline:
    status = handle.status()
    for alert in session.pop_alerts():
        text = alert.message()
        kind = type(alert).__name__
        if kind in ("peer_error_alert", "peer_disconnected_alert", "invalid_request_alert",
                    "hash_failed_alert", "torrent_error_alert", "file_error_alert"):
            if kind == "peer_disconnected_alert":
                # Reconnect, as a client that got the peer from a tracker would.
                handle.connect_peer((host, int(port)))
            seen[f"{kind}: {text}"] = seen.get(f"{kind}: {text}", 0) + 1
    if status.is_seeding:
        print("complete")
        sys.exit(0)
    if status.progress != last_progress:
        last_progress = status.progress
    time.sleep(0.5)

print(f"INCOMPLETE: {last_progress * 100:.2f}% after {args.timeout:.0f} s")
for text, count in sorted(seen.items(), key=lambda kv: -kv[1])[:12]:
    print(f"  {count}x {text}")
sys.exit(1)
