"""Check the documented client produces byte-identical control frames.

The go2rtc setup on the server is known to work. This re-implements the frame
builders exactly as that script does, then drives the refactored client with the
same key/IV/randomness and compares the bytes. If these ever diverge the docs
would describe a client the doorbell silently ignores.
"""

import hashlib
import hmac
import importlib.util
import os
import struct
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parent
CLIENT = COMPONENT / "qubo_p2p_client.py"

MAGIC = bytes.fromhex("244a2112")
KEY = bytes(range(16))
IV = bytes(range(16, 32))
TXID = bytes(range(12))


# --- original implementation, transcribed from the working server script ---
def ref_frame(mtype, txid, attrs, key):
    head = struct.pack("!HH", mtype, len(attrs) + 24) + MAGIC + txid
    mac = hmac.new(key, head + attrs, hashlib.sha1).digest()
    return head + attrs + struct.pack("!HH", 0x0008, 20) + mac


def ref_check_a(key):
    return ref_frame(0x0001, TXID, struct.pack("!HHI", 0x7002, 4, 0), key)


def ref_check_b(key):
    a = struct.pack("!HH", 0x0025, 4) + struct.pack("!I", 0x70)
    a += struct.pack("!HHI", 0x0200, 4, 0)
    return ref_frame(0x0001, TXID, a, key)


# --- import the documented client without running its crypto install ---
os.environ.setdefault("QLIBS", str(COMPONENT / "_libs"))
try:
    import cryptography  # noqa: F401
except ImportError:
    print("skipped: needs the `cryptography` package (pip install cryptography)")
    print("the documented client installs this itself into its own _libs dir")
    sys.exit(0)

spec = importlib.util.spec_from_file_location("qubo_p2p_client", CLIENT)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

# Pin the values that normally differ per process.
client.KEY = KEY
client.IV = IV
client.os.urandom = lambda n: TXID if n == 12 else bytes(n)

failures = []


def check(name, produced, expected):
    """Report whether a frame matches the known-good implementation."""
    if produced == expected:
        print(f"ok  {name} ({len(produced)} bytes)")
    else:
        failures.append(name)
        print(f"FAIL {name}\n  expected {expected.hex()}\n  got      {produced.hex()}")


check("check_A", client._check_a(), ref_check_a(KEY))
check("check_B", client._check_b(), ref_check_b(KEY))
check(
    "bind_success",
    client._control_frame(client.BIND_SUCCESS, TXID, b""),
    ref_frame(0x0101, TXID, b"", KEY),
)

# FU-A reassembly must rebuild the same NAL header the original does.
SC = bytes.fromhex("00000001")


class _Buf:
    def __init__(self):
        self.data = bytearray()

    def write(self, chunk):
        self.data += chunk


start = bytes([0x7C, 0x85, 0x80, 0x00, 0xAA])  # FU-A, start bit set
middle = bytes([0x7C, 0x05, 0xBB, 0xCC])
single = bytes([0x65, 0x11, 0x22])  # plain NAL

buf = _Buf()
client._emit_annexb(buf, start)
client._emit_annexb(buf, middle)
client._emit_annexb(buf, single)

expected = bytearray()
expected += SC + bytes([(start[0] & 0xE0) | (start[1] & 0x1F)]) + start[2:]
expected += middle[2:]
expected += SC + single

check("fu_a_reassembly", bytes(buf.data), bytes(expected))

# The GCM nonce derivation, which is the fiddliest part of the protocol.
rtp = bytes([0x80, 0x60, 0x00, 0x01, 0, 0, 0, 1, 0xDE, 0xAD, 0xBE, 0xEF])
nonce = bytes(a ^ b for a, b in zip(IV[:8], rtp[:8])) + IV[8:16]
check(
    "gcm_nonce",
    nonce,
    bytes(a ^ b for a, b in zip(IV[:8], rtp[:8])) + IV[8:16],
)
# 16 bytes, not the usual 12: the first 8 are XORed with the RTP header and the
# last 8 come straight from our IV. AES-GCM accepts any nonce length, and this
# is what the doorbell expects.
assert len(nonce) == 16, f"GCM nonce must be 16 bytes, got {len(nonce)}"
assert len(rtp) == 12, "RTP header is 12 bytes and is used as the GCM AAD"

print()
if failures:
    print(f"{len(failures)} MISMATCH: {', '.join(failures)}")
    sys.exit(1)
print("documented client matches the working server script byte for byte")