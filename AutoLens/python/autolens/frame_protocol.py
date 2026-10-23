"""Message framing between Nuke and the frame-server workers (autolens/frame_worker.py).

Plain Python with no package imports, so the worker (a separate process running
AutoLens's managed Python) can import it as a top-level module.

A message is::

    4 bytes   big-endian length N of the JSON header
    N bytes   UTF-8 JSON object; "nbytes" gives the payload length (0 if absent)
    nbytes    raw payload (decoded RGB pixels in replies)

Requests carry no payload. Pixels never go through files.
"""
from __future__ import annotations

import json
import struct

MAX_HEADER = 1 << 20          # 1 MB of JSON is far beyond any real header
MAX_PAYLOAD = 1 << 31         # 2 GB: a 16K float frame would still fit


class ProtocolError(RuntimeError):
    pass


def pack(header, payload=b""):
    """Bytes for one message; sets header["nbytes"] to the payload length."""
    header = dict(header)
    header["nbytes"] = len(payload)
    blob = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(blob) > MAX_HEADER:
        raise ProtocolError("header too large (%d bytes)" % len(blob))
    return struct.pack(">I", len(blob)) + blob + bytes(payload)


def read_exact(stream, n):
    """Exactly n bytes from a binary stream; EOFError if it ends first."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("stream closed after %d of %d bytes" % (n - remaining, n))
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv(stream):
    """(header dict, payload bytes) for the next message on the stream."""
    (size,) = struct.unpack(">I", read_exact(stream, 4))
    if size > MAX_HEADER:
        raise ProtocolError("header length %d is not plausible" % size)
    try:
        header = json.loads(read_exact(stream, size).decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError("bad header: %s" % exc)
    if not isinstance(header, dict):
        raise ProtocolError("header is not an object")
    nbytes = int(header.get("nbytes", 0) or 0)
    if nbytes < 0 or nbytes > MAX_PAYLOAD:
        raise ProtocolError("payload length %d is not plausible" % nbytes)
    payload = read_exact(stream, nbytes) if nbytes else b""
    return header, payload


def send(stream, header, payload=b""):
    stream.write(pack(header, payload))
    stream.flush()
