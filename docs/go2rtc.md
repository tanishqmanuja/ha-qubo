# Local video with go2rtc

The Qubo cloud mints a single-consumer RTSPS URL per viewer. That works, but
video leaves your house and two viewers cannot watch at once.

Qubo also speaks a peer-to-peer protocol: after a short handshake through the
cloud, the doorbell streams RTP **straight to your machine**. This guide sets
that up in [go2rtc](https://github.com/AlexxIT/go2rtc), so video never touches
the cloud and stays available to everything that speaks RTSP.

> **Scope.** This needs the doorbell and the machine running go2rtc to be on the
> same LAN, and go2rtc must have the `exec` module enabled. It is a companion to
> the Home Assistant integration, not a replacement — the integration still does
> all the device control, and this only handles video.

## How it works

1. The client posts a session request to the cloud. The request carries a key
   and IV that **we** choose, plus our LAN address as the candidate. The cloud
   only passes the message along; no media goes through it.
2. The doorbell finds us over UDP. Every control frame starts with the magic
   `244a2112` and ends with an HMAC-SHA1 of the frame keyed by the key we chose.
   We answer its binding requests and send two probes, the second of which is
   the "go" signal.
3. The doorbell starts sending RTP to us, encrypted with AES-128-GCM. The
   authentication data is the 12-byte RTP header, and the nonce is the first 8
   bytes of our IV XORed with the first 8 bytes of the RTP header followed by our
   IV's last 8 bytes.
4. The client decrypts, reassembles the FU-A fragments into Annex-B H.264 and
   writes it to stdout. ffmpeg reads that and republishes it as RTSP for go2rtc.

Audio arrives as ADTS AAC but is currently discarded — this is video only.

## Requirements

- go2rtc built with the `exec` module (the standard Docker image is).
- `python3`, `ffmpeg` and `pip` available to go2rtc.
- The `cryptography` Python package. The client installs it on first run into a
  `_libs` directory next to itself, so it needs write access there and network
  access on first run. Point `QLIBS` somewhere else if you would rather.
- The doorbell and go2rtc on the same LAN, with the UDP port below reachable
  from the doorbell.

## 1. Find your values

Three UUIDs are needed, all readable from the Qubo app or the cloud:

| Variable | What it is |
| --- | --- |
| `QDEVICE` | The doorbell's device UUID |
| `QUNIT` | The unit (home) UUID the doorbell belongs to |
| `QMOB` | A paired phone's device UUID, sent as `mobileUUID` |
| `QADVERT` | The LAN IP the doorbell should stream to |

`QADVERT` is the one that catches people out: inside a container the process
usually binds `0.0.0.0`, but the address handed to the doorbell must be one it
can actually reach. Use the container host's LAN address, not the container's
internal one.

You can find these by reading them back out of the integration's config entry:

```bash
# Doorbell device UUID and unit UUID
grep -o '"mobile_device_id": "[^"]*"' /config/.storage/core.config_entries
```

`QMOB` is the paired phone — the Qubo app shows it under
*Account -> Logged In Devices*.

## 2. Credentials

The client needs a valid access token. It reads one from Home Assistant's
config entry so there is nothing extra to store and Home Assistant stays the only
thing that refreshes tokens:

```
QSTORE=/ha-storage/core.config_entries
```

Point `QSTORE` at a read-only mount of `.storage/core.config_entries` from the
same Home Assistant that runs this integration.

Alternatively, bypass Home Assistant entirely with `QACCESS` (access token),
`QUUID` (user UUID) and optionally `QREFRESH`. **No other script can refresh
these for you**, so a token that has passed its one-hour life will start failing
with HTTP 401 until you replace it.

## 3. Configure go2rtc

Copy `qubo_p2p_client.py` into your go2rtc config directory — the same folder as
`go2rtc.yaml` — so it sits next to its `_libs` directory.

Then add the stream:

```yaml
preload:
  qubo_doorbell: video=h264

streams:
  qubo_doorbell:
    - exec:sh -c "QDEVICE=<doorbell-uuid> QUNIT=<unit-uuid> QMOB=<phone-uuid> QADVERT=<host-lan-ip> QPORT=49700 QQUALITY=high QDUR=86400 QSTORE=/ha-storage/core.config_entries python3 /config/qubo_p2p_client.py - 49700 2>>/config/qubo_err.log | ffmpeg -hide_banner -loglevel error -f h264 -i pipe:0 -c copy -f rtsp {output}"

webrtc:
  candidates:
    - <host-lan-ip>:8555
```

Notes on that line:

- The script and ffmpeg are joined by a pipe. The script writes H.264 on stdout,
  ffmpeg reads it and republishes it to go2rtc with `-f rtsp {output}`.
- `2>>/config/qubo_err.log` captures the script's progress. It prints how many
  packets it decrypted, which is the quickest way to tell "no handshake" from
  "handshake fine, no media". Append (`>>`) rather than truncate so restarting
  go2rtc does not throw away the evidence of the previous run.
- `-c copy` avoids a pointless re-encode.
- `QDUR=86400` keeps one session alive for a day. **This is not optional.** The
  client exits when `QDUR` expires, and go2rtc does *not* restart a finished
  exec source — a viewer watching at that moment gets a stream that dies with
  nothing to replace it. See [why the obvious fixes backfire](#why-the-obvious-fixes-backfire).
- `preload` keeps one session open so the first viewer does not wait for a
  handshake. Without it the stream still works, it just connects on demand.

The client also takes two timeouts of its own, both defaulted and rarely
needing a change:

| Variable | Default | What it does |
| --- | --- | --- |
| `QMEDIATIMEOUT` | 30 | Give up if no media arrives within this many seconds |
| `QSTALLTIMEOUT` | 120 | Give up if a working session goes this many seconds without media |

Both exist so a session the doorbell never joins cannot outlive the process
that started it. Without them the client waits out the whole of `QDUR` — 24
hours with the setting above — holding the UDP port and answering the
doorbell's checks long after go2rtc abandoned it, which makes the next session
fail as well.

## Why the obvious fixes backfire

The failure looks like "go2rtc does not reconnect", and the two natural repairs
both make it worse. Worth writing down.

**Do not wrap the pipeline in a retry loop.** `exec:` sources are one process per
consumer session. A `while :; do … done` around the line republishes a *new*
ffmpeg to the same `{output}` on every cycle, and each republish interrupts the
viewer already attached — you get `MSE stalled`, `WebRTC connect timeout`, and the
client flapping between transports while go2rtc looks healthy. Make the session
durable with `QDUR` instead of restarting it.

**Keep the `sh -c`.** The media goes to go2rtc over RTSP, not stdout, so the exec
source produces no output and go2rtc logs `ERR [exec] timeout` and kills the
process it started. With `sh -c` in front, only the shell is killed and the
pipeline is orphaned and carries on, which is why the line above keeps working
despite that error. Replace `sh -c` with a bare `python3 …` and the run is torn
down early. The error is cosmetic here; the shell is not.

Restart go2rtc and watch for decrypted packets:

```bash
tail -f /config/qubo_err.log
```

## Troubleshooting

**`no decrypted packets` and no error.** The handshake did not complete. In
order of likelihood: `QADVERT` is not an address the doorbell can reach; UDP
49700 is blocked between the two; or the access token has expired (HTTP 401 in
the error log).

**Packets are decrypted but go2rtc shows nothing.** ffmpeg is not receiving
Annex-B. Check that the doorbell is actually sending video — privacy mode or
"do not disturb" can stop it.

**It works, then drops after a minute.** `QDUR` defaults to 60 seconds, after
which the client exits deliberately — and go2rtc will not start it again for the
viewer that is already watching. Set `QDUR` high (86400). A loop around the
command looks like a fix and is not; see
[why the obvious fixes backfire](#why-the-obvious-fixes-backfire).

**`WebRTC connect timeout` or `MSE stalled` in a browser, but frames decode
fine via `curl /api/frame.jpeg`.** Usually a second publish into the same
`{output}` — a retry loop, or go2rtc restarting a session under an attached
viewer. Check `docker logs go2rtc | grep qubo` for how many producers cycle
while a single viewer is watching.

**`HTTP Error 400` from `p2pCall`.** The cloud refused the session request, which
usually means the doorbell is still occupied by a previous session that has not
wound down. Wait, or power-cycle the doorbell. Do not retry in a tight loop —
that is what keeps it occupied.

**Bind failure on restart.** The client sets `SO_REUSEADDR` and retries once
after 3 seconds, so a lingering previous process is normally not a problem.

**The client runs but nothing arrives, and it never exits.** Older versions
waited the full `QDUR` for media that was never coming, holding the UDP port
and still answering the doorbell's checks — which made the *next* session fail
too. The client now gives up after `QMEDIATIMEOUT` seconds without a single
media packet, and after `QSTALLTIMEOUT` seconds of silence once it was working.

**Orphaned clients, several `qubo_client.py` processes at once.** go2rtc runs
the source through `sh -c` and kills that shell when it stops a producer, but
the shell's children are not killed with it. The client now watches its parent
and exits when it is reparented, so a session can never outlive the process
that started it. If you ever see more than one, something is running a copy
outside go2rtc.

**A session that goes quiet while you are watching.** The client treats
`QSTALLTIMEOUT` seconds of silence as dead and exits, which drops the stream.
Raise it if your doorbell pauses for long stretches, but understand that a
higher value also means a broken session lingers that much longer.

**Two `qubo_p2p_client.py` processes on the same port.** Expected: `preload`
holds a standby session while a viewer's session runs. The doorbell serves one
peer at a time, so the two compete. It works in practice, but if sessions refuse
to start, drop `qubo_doorbell` from the `preload:` block — with a long `QDUR` the
viewer's own session already lasts, so the standby buys about a second of
startup at the cost of a second slot.

## Honest limitations

- **The cloud is still involved at session start.** The handshake is
  signalled through it. Only the media is local.
- **Video only.** Audio is decrypted-capable but not decoded or forwarded.
- **One stream at a time per doorbell.** The doorbell serves a single peer.
- **This is reverse-engineered.** The protocol is undocumented and could change
  with a firmware update. If video stops working after a doorbell update, that
  is the first thing to suspect.