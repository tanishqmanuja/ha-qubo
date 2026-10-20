"""Qubo Video Doorbell -> H.264 elementary stream over local P2P.

Speaks the doorbell's peer-to-peer protocol directly, so video never touches
the Qubo cloud. Written for go2rtc's `exec:` source: it writes Annex-B H.264 to
stdout, and go2rtc/ffmpeg consumes it.

Protocol (reverse-engineered from the vendor app, Sep 2026):

  1) Ask the cloud to start a session. ``POST .../p2pCall`` with a client-chosen
     AES key and IV, and our LAN address as the UDP candidate. The cloud only
     signals; from here on the doorbell talks to us directly.

  2) ICE-style handshake over UDP. Every control frame starts with the magic
     ``244a2112`` and ends with a HMAC-SHA1 of the header+attributes keyed by
     the AES key we chose.
       - a Binding Request (0x0001) is answered with a Success (0x0101)
       - check A (attribute 0x7002) and check B (USERNAME 0x0025 = 0x70, then
         attribute 0x0200) are re-sent every two seconds

     Check B is the "go" signal; once the doorbell sees it, it starts sending
     RTP to the address we advertised.

  3) Each RTP packet carries AES-128-GCM ciphertext. The AAD is the 12-byte RTP
     header and the IV is the first 8 bytes of our IV XORed with the first 8
     bytes of the RTP header, followed by our IV's last 8 bytes. The 16-byte GCM
     tag is appended to the ciphertext.

  4) Video (payload type 96) is reassembled from FU-A fragments into Annex-B and
     written out. Audio (payload type 97) is ADTS AAC and is left alone.

Usage:
    qubo_p2p_client.py <out.h264|-> [port]

    Invoke through `sh -c` in a go2rtc `exec:` source. The real media leaves over
    RTSP via `{output}`, so the source writes nothing go2rtc can read; go2rtc
    logs `ERR [exec] timeout` and kills the process it spawned. `sh -c` absorbs
    that and leaves the pipeline orphaned and running, which is what keeps the
    stream alive. The error is cosmetic.

Environment:
    QSP        service provider id          (default: Qubo's public constant)
    QDEVICE    doorbell device UUID         (required)
    QUNIT      unit UUID                    (required)
    QMOB       mobile device UUID, the `mobileUUID` and stream userAppID (required)
    QSOURCE    source device id sent in the request (default: your HA login id)
    QADVERT    LAN IP to advertise          (default: first outbound address)
    QPORT      local UDP port               (default: 49700)
    QQUALITY   low | high                    (default: low)
    QDUR       seconds to run before exiting (default: 60). Raise it well past
               the longest expected viewing session: go2rtc does not restart a
               finished exec source, so exiting under a viewer kills their stream
               with nothing to replace it.
    QMEDIATIMEOUT  seconds to wait for the first media packet before giving up
               (default: 30). A session the doorbell never joins must not
               outlive the process that started it.
    QSTALLTIMEOUT  seconds a working session may go silent before giving up
               (default: 120).
    QSTORE     path to Home Assistant's core.config_entries
    QACCESS / QREFRESH / QUUID / QDEVICEID
               explicit credentials, used instead of reading QSTORE
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://srvcapp.platform.quboworld.com:443"

# Qubo's public application id, the same constant the vendor app sends.
APP_ID = "934488E68332E88B1E0F9AF552840184955629777525A195949C0BE97DEF6455"

# Qubo's service provider id. Also a public constant, identical for all
# accounts.
DEFAULT_SP = "d10e4bfb0153496e8e8bb955f7ebe413"

MAGIC = bytes.fromhex("244a2112")
SC = bytes.fromhex("00000001")  # Annex-B start code

PT_VIDEO = 96
PT_AUDIO = 97

BIND_REQUEST = 0x0001
BIND_SUCCESS = 0x0101
CHECK_INTERVAL = 2.0
STALL_DROP_SECONDS = 0.5

# How long to wait for the first media packet, and how long a session may go
# silent once it was working. Both exist so a session the doorbell never
# joins cannot outlive the process that started it.
MEDIA_TIMEOUT = float(os.environ.get("QMEDIATIMEOUT", "30"))
STALL_TIMEOUT = float(os.environ.get("QSTALLTIMEOUT", "120"))

# A fresh key and IV per process, which is also a fresh session: the doorbell
# uses exactly the values we hand it in the p2pCall request.
KEY = os.urandom(16)
IV = os.urandom(16)


def _require(name: str) -> str:
    """Return a required environment variable or explain what is missing."""
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"qubo: {name} is not set")
    return value


def _install_crypto(libdir: str) -> None:
    """Import AES-GCM, installing `cryptography` into libdir once if needed."""
    for candidate in ("", libdir):
        if candidate:
            sys.path.insert(0, candidate)
        try:
            import cryptography  # noqa: F401

            return
        except ImportError:
            continue

    import subprocess

    os.makedirs(libdir, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--target",
            libdir,
            "cryptography",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=300,
    )
    sys.path.insert(0, libdir)


_libdir = os.environ.get("QLIBS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_libs"
)
_install_crypto(_libdir)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402


def _credentials() -> tuple[str, str, str]:
    """Return (access token, refresh token, user uuid) for the cloud API.

    Reads them straight out of Home Assistant's config entry when pointed at
    it, so this script never has to store a credential of its own and Home
    Assistant stays the only thing that refreshes them. Explicit environment
    variables win, for setups that are not running next to Home Assistant.
    """
    access = os.environ.get("QACCESS")
    refresh = os.environ.get("QREFRESH", "")
    user_uuid = os.environ.get("QUUID", "")
    if access and user_uuid:
        return access, refresh, user_uuid

    store = os.environ.get("QSTORE")
    if not store:
        for candidate in (
            "/ha-storage/core.config_entries",
            "/config/.storage/core.config_entries",
        ):
            if os.path.exists(candidate):
                store = candidate
                break
    if not store:
        raise SystemExit("qubo: set QSTORE or QACCESS/QUUID")

    with open(store) as handle:
        entries = json.load(handle)["data"]["entries"]
    for entry in entries:
        if entry.get("domain") != "qubo":
            continue
        data = entry["data"]
        if not data.get("access_token"):
            continue
        return (
            data["access_token"],
            data.get("refresh_token", ""),
            data.get("user_uuid", ""),
        )
    raise SystemExit(f"qubo: no Qubo entry with a token in {store}")


def _default_advert_ip() -> str:
    """Return the address of the default route, as seen from this host."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually sent to
        return probe.getsockname()[0]
    finally:
        probe.close()


def _control_frame(msg_type: int, txid: bytes, attrs: bytes) -> bytes:
    """Build one control frame with its trailing HMAC."""
    header = struct.pack("!HH", msg_type, len(attrs) + 24) + MAGIC + txid
    mac = hmac.new(KEY, header + attrs, hashlib.sha1).digest()
    return header + attrs + struct.pack("!HH", 0x0008, 20) + mac


def _check_a() -> bytes:
    """Liveness probe."""
    return _control_frame(
        BIND_REQUEST, os.urandom(12), struct.pack("!HHI", 0x7002, 4, 0)
    )


def _check_b() -> bytes:
    """The "go" signal: claim a username, then set the stream attribute."""
    attrs = struct.pack("!HH", 0x0025, 4) + struct.pack("!I", 0x70)
    attrs += struct.pack("!HHI", 0x0200, 4, 0)
    return _control_frame(BIND_REQUEST, os.urandom(12), attrs)


def _open_session(port: int, advert: str, quality: str, headers: dict) -> None:
    """Ask the cloud to signal a session for this doorbell."""
    body = {
        "aesIV": IV.hex().upper(),
        "aesKey": KEY.hex().upper(),
        "mediaStreams": "both",
        "mobileUUID": os.environ["QMOB"],
        "offerData": json.dumps(
            [
                {
                    "host": f"{advert}:{port}",
                    "protocol": "UDP",
                    "srflx_0": f"{advert}:{port}",
                }
            ]
        ),
        "protocols": "udp",
        "pttEnabled": "false",
        "sessionId": str(uuid.uuid4()),
        "sourceType": "live",
        "srcDeviceId": os.environ.get(
            "QSOURCE", os.environ.get("QDEVICEID", "")
        ),
        "streamSourceQuality": quality,
    }
    path = (
        "/stream-manager/api/v2/sp/%s/unit/%s/device/%s/p2pCall"
        % (os.environ.get("QSP", DEFAULT_SP), os.environ["QUNIT"], os.environ["QDEVICE"])
    )
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as err:
        raise SystemExit(f"qubo: p2pCall rejected ({err.code})") from err


def _emit_annexb(out, payload: bytes) -> None:
    """Write one NAL, turning FU-A fragments back into a start code."""
    if not payload:
        return
    nal_type = payload[0] & 0x1F
    if nal_type == 28 and len(payload) >= 2:  # FU-A
        header = payload[1]
        if (header >> 7) & 1:  # first fragment: restore the real NAL header
            out.write(SC + bytes([(payload[0] & 0xE0) | (header & 0x1F)]))
        out.write(payload[2:])
    else:
        out.write(SC + payload)


def main() -> None:
    """Open a P2P session and stream video until the run duration expires."""
    out_path = sys.argv[1] if len(sys.argv) > 1 else "-"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("QPORT", "49700"))
    advert = os.environ.get("QADVERT") or _default_advert_ip()
    quality = os.environ.get("QQUALITY", "low")
    duration = float(os.environ.get("QDUR", "60"))

    for name in ("QDEVICE", "QUNIT", "QMOB"):
        _require(name)

    access_token, _refresh, user_uuid = _credentials()
    headers = {
        "Subscriber-Key": access_token,
        "User-UUID": user_uuid,
        "Token-Type": "USER",
        "Source-Device-Id": os.environ.get("QSOURCE", os.environ.get("QDEVICEID", "")),
        "App-Id": APP_ID,
        "Source": "Android",
        "Content-Type": "application/json",
    }

    _open_session(port, advert, quality, headers)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # SO_REUSEADDR lets a fresh run take the port even if a previous process is
    # still lingering, so a restart does not need the port to be free.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as err:
        sys.stderr.write(f"qubo: bind :{port} failed ({err}), retrying in 3s\n")
        time.sleep(3)
        sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)

    cipher = AESGCM(KEY)
    out = sys.stdout.buffer if out_path == "-" else open(out_path, "wb", buffering=0)

    doorbell = None
    last_a = last_b = 0.0
    pending: dict[int, bytes] = {}
    next_seq = None
    last_flush = time.time()
    packets = 0
    started = time.time()
    last_media = started
    parent_pid = os.getppid()

    sys.stderr.write(f"qubo: advertising {advert}:{port} -> {out_path}\n")
    sys.stderr.flush()

    while time.time() - started < duration:
        now = time.time()
        if os.getppid() != parent_pid:
            # The process that started us is gone, so nobody is reading our
            # output any more. Leaving would hold the UDP port and answer
            # the doorbell's checks for the rest of QDUR.
            sys.stderr.write("qubo: parent gone, exiting\n")
            break
        # A session that never delivers media is dead, not slow. Without
        # this the client would sit in recvfrom for the whole of QDUR,
        # holding the UDP port and answering the doorbell's checks long
        # after go2rtc gave up on it -- which makes the next session fail
        # too.
        if packets == 0 and now - started > MEDIA_TIMEOUT:
            sys.stderr.write(f"qubo: no media within {MEDIA_TIMEOUT:.0f}s, giving up\n")
            break
        if packets and now - last_media > STALL_TIMEOUT:
            sys.stderr.write(f"qubo: media stopped for {STALL_TIMEOUT:.0f}s, giving up\n")
            break
        if doorbell:
            if now - last_a > CHECK_INTERVAL:
                last_a = now
                sock.sendto(_check_a(), doorbell)
            if now - last_b > CHECK_INTERVAL:
                last_b = now
                sock.sendto(_check_b(), doorbell)
        try:
            packet, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break

        if len(packet) >= 20 and packet[4:8] == MAGIC:
            # A control frame: remember the doorbell and answer its request.
            doorbell = addr
            if struct.unpack("!H", packet[:2])[0] == BIND_REQUEST:
                sock.sendto(_control_frame(BIND_SUCCESS, packet[8:20], b""), addr)
            continue

        if len(packet) < 12 + 17 or (packet[0] & 0xC0) != 0x80:
            continue
        rtp = packet[:12]
        payload_type = rtp[1] & 0x7F
        if payload_type not in (PT_VIDEO, PT_AUDIO):
            continue

        # The GCM nonce mixes our IV with the RTP header, so the same IV is
        # never reused across packets.
        nonce = bytes(a ^ b for a, b in zip(IV[:8], rtp[:8])) + IV[8:16]
        try:
            body = cipher.decrypt(nonce, packet[12:], rtp)
        except Exception:  # noqa: BLE001 - a corrupt packet is not fatal
            continue
        packets += 1
        last_media = time.time()
        if payload_type != PT_VIDEO:
            continue

        seq = struct.unpack("!H", rtp[2:4])[0]
        pending[seq] = body
        if next_seq is None:
            next_seq = seq
        try:
            while next_seq in pending:
                _emit_annexb(out, pending.pop(next_seq))
                next_seq = (next_seq + 1) & 0xFFFF
                last_flush = time.time()
            # Skip a sequence number that never arrived rather than stall
            # forever behind it.
            if next_seq not in pending and pending and (time.time() - last_flush) > STALL_DROP_SECONDS:
                next_seq = (next_seq + 1) & 0xFFFF
                last_flush = time.time()
            out.flush()
        except (BrokenPipeError, OSError):
            break

    if out_path != "-":
        out.close()
    # ffmpeg may already be gone; do not let shutdown tracebacks reach go2rtc.
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.stderr.write(f"qubo: decrypted {packets} packets, wrote {out_path}\n")


if __name__ == "__main__":
    main()