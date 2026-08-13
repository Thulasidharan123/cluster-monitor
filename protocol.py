"""
protocol.py -- tiny length-prefixed message protocol shared by host and viewer.

Every message on the wire is:
    [1 byte  : msg_type]
    [4 bytes : payload length, unsigned big-endian]
    [N bytes : payload]

This keeps TCP's stream neatly chopped into discrete messages.
"""
import struct

# ---- message types ----
MSG_AUTH         = 1   # viewer -> host : payload = password (utf-8)
MSG_AUTH_OK      = 2   # host   -> viewer
MSG_AUTH_FAIL    = 3   # host   -> viewer
MSG_SCREEN_INFO  = 4   # host   -> viewer : '!II' (width, height)
MSG_FRAME        = 5   # host   -> viewer : JPEG bytes
MSG_MOUSE_MOVE   = 6   # viewer -> host   : '!ff' (fx, fy)  normalised 0..1
MSG_MOUSE_BUTTON = 7   # viewer -> host   : '!BB' (button_id, pressed)
MSG_MOUSE_SCROLL = 8   # viewer -> host   : '!ii' (dx, dy)
MSG_KEY          = 9   # viewer -> host   : [1 byte pressed][key string utf-8]

_HEADER = struct.Struct('!BI')  # msg_type (B) + length (I)


def send_msg(sock, msg_type, payload=b''):
    """Send one framed message. Safe to call from a single sender thread."""
    sock.sendall(_HEADER.pack(msg_type, len(payload)) + payload)


def _recv_all(sock, n):
    """Receive exactly n bytes or return None if the peer closed."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    """Receive one framed message. Returns (msg_type, payload) or (None, None)."""
    header = _recv_all(sock, _HEADER.size)
    if header is None:
        return None, None
    msg_type, length = _HEADER.unpack(header)
    payload = _recv_all(sock, length) if length else b''
    if payload is None:
        return None, None
    return msg_type, payload
