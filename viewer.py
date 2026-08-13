"""
viewer.py -- run this on the machine you are controlling FROM.

It connects to a host, shows the remote screen in a window, and forwards
your mouse and keyboard to it.

    python viewer.py --host 192.168.1.42 --password mysecret

Install deps:  pip install pygame opencv-python numpy
"""
import argparse
import io
import socket
import struct
import threading

import pygame

import protocol as p

# Map pygame keycodes -> the string names host.py understands.
PYGAME_SPECIAL = {
    pygame.K_RETURN: 'enter', pygame.K_KP_ENTER: 'enter',
    pygame.K_ESCAPE: 'esc', pygame.K_BACKSPACE: 'backspace',
    pygame.K_TAB: 'tab', pygame.K_DELETE: 'delete', pygame.K_INSERT: 'insert',
    pygame.K_UP: 'up', pygame.K_DOWN: 'down', pygame.K_LEFT: 'left', pygame.K_RIGHT: 'right',
    pygame.K_HOME: 'home', pygame.K_END: 'end',
    pygame.K_PAGEUP: 'pageup', pygame.K_PAGEDOWN: 'pagedown',
    pygame.K_LSHIFT: 'shift', pygame.K_RSHIFT: 'shift',
    pygame.K_LCTRL: 'ctrl', pygame.K_RCTRL: 'ctrl',
    pygame.K_LALT: 'alt', pygame.K_RALT: 'alt',
    pygame.K_LMETA: 'cmd', pygame.K_RMETA: 'cmd',
    pygame.K_LSUPER: 'cmd', pygame.K_RSUPER: 'cmd',
    pygame.K_CAPSLOCK: 'caps',
    pygame.K_F1: 'f1', pygame.K_F2: 'f2', pygame.K_F3: 'f3', pygame.K_F4: 'f4',
    pygame.K_F5: 'f5', pygame.K_F6: 'f6', pygame.K_F7: 'f7', pygame.K_F8: 'f8',
    pygame.K_F9: 'f9', pygame.K_F10: 'f10', pygame.K_F11: 'f11', pygame.K_F12: 'f12',
}

# pygame mouse buttons -> our button ids (left/middle/right)
MOUSE_BUTTONS = {1: 1, 2: 2, 3: 3}


class FrameStore:
    """Thread-safe handoff of the newest frame from receiver to main loop."""
    def __init__(self):
        self.lock = threading.Lock()
        self.surface = None
        self.running = True


def receiver(sock, store):
    """Background thread: decode incoming JPEG frames into pygame surfaces.

    pygame (via bundled SDL_image) decodes the JPEG directly, so the viewer
    needs no OpenCV/numpy -- which also avoids the macOS SDL2 duplicate-library
    clash between cv2 and pygame.
    """
    while store.running:
        msg_type, payload = p.recv_msg(sock)
        if msg_type is None:
            store.running = False
            break
        if msg_type == p.MSG_FRAME:
            try:
                surf = pygame.image.load(io.BytesIO(payload), "frame.jpg")
            except Exception:
                continue
            with store.lock:
                store.surface = surf


def main():
    ap = argparse.ArgumentParser(description="PyDesk viewer (remote control).")
    ap.add_argument('--host', required=True, help='host IP address')
    ap.add_argument('--port', type=int, default=5900)
    ap.add_argument('--password', default='changeme')
    ap.add_argument('--max-width', type=int, default=1280)
    ap.add_argument('--max-height', type=int, default=800)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((args.host, args.port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    # --- authenticate ---
    p.send_msg(sock, p.MSG_AUTH, args.password.encode('utf-8'))
    msg_type, _ = p.recv_msg(sock)
    if msg_type != p.MSG_AUTH_OK:
        print("Authentication failed.")
        return

    msg_type, payload = p.recv_msg(sock)
    host_w, host_h = struct.unpack('!II', payload)

    # Fit the initial window inside the requested max size, keeping aspect ratio.
    scale = min(args.max_width / host_w, args.max_height / host_h, 1.0)
    win_w, win_h = int(host_w * scale), int(host_h * scale)

    pygame.init()
    screen = pygame.display.set_mode((win_w, win_h), pygame.RESIZABLE)
    pygame.display.set_caption(f"PyDesk -- {args.host}")

    store = FrameStore()
    threading.Thread(target=receiver, args=(sock, store), daemon=True).start()

    pressed_chars = {}   # remember keydown chars so keyup can release the same one
    clock = pygame.time.Clock()

    try:
        while store.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    store.running = False

                elif event.type == pygame.VIDEORESIZE:
                    screen = pygame.display.set_mode(event.size, pygame.RESIZABLE)

                elif event.type == pygame.MOUSEMOTION:
                    w, h = screen.get_size()
                    p.send_msg(sock, p.MSG_MOUSE_MOVE,
                               struct.pack('!ff', event.pos[0] / w, event.pos[1] / h))

                elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                    pressed = 1 if event.type == pygame.MOUSEBUTTONDOWN else 0
                    if event.button in MOUSE_BUTTONS:
                        p.send_msg(sock, p.MSG_MOUSE_BUTTON,
                                   struct.pack('!BB', MOUSE_BUTTONS[event.button], pressed))

                elif event.type == pygame.MOUSEWHEEL:
                    p.send_msg(sock, p.MSG_MOUSE_SCROLL,
                               struct.pack('!ii', event.x, event.y))

                elif event.type == pygame.KEYDOWN:
                    key_str = PYGAME_SPECIAL.get(event.key)
                    if key_str is None and event.unicode and event.unicode.isprintable():
                        key_str = event.unicode
                        pressed_chars[event.key] = key_str
                    if key_str:
                        p.send_msg(sock, p.MSG_KEY, bytes([1]) + key_str.encode('utf-8'))

                elif event.type == pygame.KEYUP:
                    key_str = PYGAME_SPECIAL.get(event.key)
                    if key_str is None:
                        key_str = pressed_chars.pop(event.key, None)
                    if key_str:
                        p.send_msg(sock, p.MSG_KEY, bytes([0]) + key_str.encode('utf-8'))

            # --- draw the newest frame ---
            with store.lock:
                surf = store.surface
            if surf is not None:
                w, h = screen.get_size()
                screen.blit(pygame.transform.smoothscale(surf, (w, h)), (0, 0))
                pygame.display.flip()
            clock.tick(30)
    finally:
        store.running = False
        sock.close()
        pygame.quit()


if __name__ == '__main__':
    main()
