"""
host.py -- run this on the machine you want to CONTROL.

It captures the screen, streams JPEG frames to a connected viewer, and
replays the mouse/keyboard input the viewer sends back.

    python host.py --password mysecret --fps 15 --quality 60 --scale 1.0

Install deps:  pip install mss opencv-python numpy pynput
"""
import argparse
import socket
import struct
import threading
import time

import cv2
import mss
import numpy as np
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Button, Controller as MouseController

import protocol as p

mouse = MouseController()
keyboard = KeyboardController()

# Map the string names the viewer sends to pynput special keys.
SPECIAL_KEYS = {
    'enter': Key.enter, 'esc': Key.esc, 'backspace': Key.backspace,
    'tab': Key.tab, 'space': Key.space, 'delete': Key.delete,
    'up': Key.up, 'down': Key.down, 'left': Key.left, 'right': Key.right,
    'home': Key.home, 'end': Key.end, 'pageup': Key.page_up,
    'pagedown': Key.page_down, 'insert': Key.insert,
    'shift': Key.shift, 'ctrl': Key.ctrl, 'alt': Key.alt, 'cmd': Key.cmd,
    'caps': Key.caps_lock,
    'f1': Key.f1, 'f2': Key.f2, 'f3': Key.f3, 'f4': Key.f4,
    'f5': Key.f5, 'f6': Key.f6, 'f7': Key.f7, 'f8': Key.f8,
    'f9': Key.f9, 'f10': Key.f10, 'f11': Key.f11, 'f12': Key.f12,
}

BUTTONS = {1: Button.left, 2: Button.middle, 3: Button.right}


def handle_input(conn, screen_w, screen_h):
    """Runs in its own thread: read input messages and act on them."""
    try:
        while True:
            msg_type, payload = p.recv_msg(conn)
            if msg_type is None:
                break

            if msg_type == p.MSG_MOUSE_MOVE:
                fx, fy = struct.unpack('!ff', payload)
                mouse.position = (int(fx * screen_w), int(fy * screen_h))

            elif msg_type == p.MSG_MOUSE_BUTTON:
                button_id, pressed = struct.unpack('!BB', payload)
                btn = BUTTONS.get(button_id)
                if btn:
                    (mouse.press if pressed else mouse.release)(btn)

            elif msg_type == p.MSG_MOUSE_SCROLL:
                dx, dy = struct.unpack('!ii', payload)
                mouse.scroll(dx, dy)

            elif msg_type == p.MSG_KEY:
                pressed = payload[0]
                key_str = payload[1:].decode('utf-8', errors='ignore')
                key_obj = SPECIAL_KEYS.get(key_str)
                if key_obj is None and len(key_str) == 1:
                    key_obj = key_str          # a normal character
                if key_obj is not None:
                    try:
                        (keyboard.press if pressed else keyboard.release)(key_obj)
                    except Exception:
                        pass                   # ignore keys pynput can't map
    except Exception as e:
        print("[host] input handler stopped:", e)


def serve_client(conn, addr, args):
    print("[host] client connected:", addr)

    # --- authenticate ---
    msg_type, payload = p.recv_msg(conn)
    if msg_type != p.MSG_AUTH or payload.decode('utf-8', 'ignore') != args.password:
        p.send_msg(conn, p.MSG_AUTH_FAIL)
        conn.close()
        print("[host] auth failed:", addr)
        return
    p.send_msg(conn, p.MSG_AUTH_OK)

    with mss.mss() as sct:
        monitor = sct.monitors[args.monitor]
        screen_w, screen_h = monitor['width'], monitor['height']
        p.send_msg(conn, p.MSG_SCREEN_INFO, struct.pack('!II', screen_w, screen_h))

        threading.Thread(target=handle_input,
                         args=(conn, screen_w, screen_h), daemon=True).start()

        frame_interval = 1.0 / args.fps
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), args.quality]
        try:
            while True:
                start = time.time()
                img = np.array(sct.grab(monitor))            # BGRA
                frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                if args.scale != 1.0:
                    frame = cv2.resize(frame, None, fx=args.scale, fy=args.scale,
                                       interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode('.jpg', frame, encode_param)
                if ok:
                    p.send_msg(conn, p.MSG_FRAME, buf.tobytes())
                elapsed = time.time() - start
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)
        except (ConnectionError, BrokenPipeError, OSError):
            pass
        finally:
            conn.close()
            print("[host] client disconnected:", addr)


def main():
    ap = argparse.ArgumentParser(description="PyDesk host (screen sharer).")
    ap.add_argument('--host', default='0.0.0.0', help='bind address')
    ap.add_argument('--port', type=int, default=5900)
    ap.add_argument('--password', default='changeme')
    ap.add_argument('--fps', type=int, default=15)
    ap.add_argument('--quality', type=int, default=60, help='JPEG quality 1-100')
    ap.add_argument('--scale', type=float, default=1.0, help='resize factor, e.g. 0.7')
    ap.add_argument('--monitor', type=int, default=1, help='1 = primary monitor')
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(1)
    print(f"[host] listening on {args.host}:{args.port}  (Ctrl+C to quit)")
    try:
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            serve_client(conn, addr, args)   # one viewer at a time
    except KeyboardInterrupt:
        print("\n[host] shutting down")
    finally:
        srv.close()


if __name__ == '__main__':
    main()
