"""Minimal length-prefixed TCP tunnel carrying raw 14-byte RFU comm-slot bytes between the two
relay processes (one per house), over Tailscale. No framing beyond a 2-byte big-endian length
prefix; no retries, no reconnection - a dropped tunnel ends the session on both sides, same as a
real link cable being pulled.
"""

import queue
import socket
import struct
import threading


class Tunnel:
    """One TCP connection to the other house's relay process.

    `send()` is called by the engine (from HostSession/Sim's own thread) whenever the local real
    console emits something; `recv_nowait()` is polled once per tick() to get whatever the other
    house's console most recently sent. Two background threads do the actual socket I/O so the
    engine's tick()/feed_*() calls never block on the network.
    """

    def __init__(self, sock, log=print):
        self.sock = sock
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.outbox = queue.Queue()
        self.inbox = queue.Queue()
        self.log = log
        self.closed = False
        self._stop = threading.Event()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)

    def start(self):
        self._send_thread.start()
        self._recv_thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.closed = True

    def send(self, payload):
        self.outbox.put(bytes(payload))

    def recv_nowait(self):
        try:
            return self.inbox.get_nowait()
        except queue.Empty:
            return None

    def recv_blocking(self, timeout=None):
        """For the one-shot identity handshake at startup only - never call this once the main
        RFU relay loop has started draining recv_nowait(), the two would race for the same queue."""
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                payload = self.outbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.sock.sendall(struct.pack(">H", len(payload)) + payload)
            except OSError as exc:
                self.log(f"[tunnel] send failed: {exc}")
                self._stop.set()
                self.closed = True
                return

    def _recv_loop(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(4096)
            except OSError as exc:
                if self._stop.is_set():
                    return
                self.log(f"[tunnel] recv failed: {exc}")
                self._stop.set()
                self.closed = True
                return
            if not chunk:
                self.log("[tunnel] peer closed the connection")
                self._stop.set()
                self.closed = True
                return
            buf += chunk
            while len(buf) >= 2:
                size = struct.unpack(">H", buf[:2])[0]
                if len(buf) < 2 + size:
                    break
                payload, buf = buf[2:2 + size], buf[2 + size:]
                self.inbox.put(payload)


def connect_out(host, port, log=print, timeout=15):
    """The follower side dials out to the leader side's relay process."""
    log(f"[tunnel] connecting to {host}:{port}...")
    sock = socket.create_connection((host, port), timeout=timeout)
    # create_connection() leaves its connect-phase timeout set on the socket permanently; left in
    # place, the recv loop's blocking sock.recv() raises a timeout after `timeout` seconds of real
    # silence (which happens often - e.g. idle gaps between RFU polls) and kills the tunnel. Clear
    # it so recv() blocks indefinitely once the connection is actually up.
    sock.settimeout(None)
    log(f"[tunnel] connected to {host}:{port}")
    return Tunnel(sock, log=log).start()


def listen_and_accept(port, log=print, accept_timeout=300):
    """The leader side listens for the follower side's relay process to dial in."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(1)
    srv.settimeout(accept_timeout)
    log(f"[tunnel] listening on :{port}, waiting for the other house...")
    conn, addr = srv.accept()
    srv.close()
    conn.settimeout(None)  # belt-and-suspenders: see the matching note in connect_out().
    log(f"[tunnel] peer connected from {addr}")
    return Tunnel(conn, log=log).start()
