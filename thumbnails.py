from contextlib import contextmanager, suppress
import socket
from threading import Event, Lock, Timer
import time

from samsungtvws.art import SamsungTVArt
from samsungtvws.helper import get_ssl_context


THUMBNAIL_TIMEOUT = 12


class ThumbnailTimeout(TimeoutError):
    pass


class ThumbnailArt(SamsungTVArt):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._deadline = None
        self._expired = Event()
        self._transfer_socket = None
        self._socket_lock = Lock()

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if self._expired.is_set() or remaining <= 0:
            raise ThumbnailTimeout("The TV thumbnail request exceeded its deadline.")
        return remaining

    @staticmethod
    def _stop_socket(sock):
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            sock.close()

    def _expire(self):
        self._expired.set()
        if self.connection is not None:
            with suppress(OSError):
                self.connection.abort()
        with self._socket_lock:
            transfer = self._transfer_socket
        if transfer is not None:
            self._stop_socket(transfer)

    def _track_socket(self, sock):
        with self._socket_lock:
            if self._expired.is_set():
                self._stop_socket(sock)
                raise ThumbnailTimeout("The TV thumbnail request exceeded its deadline.")
            self._transfer_socket = sock

    @contextmanager
    def request_deadline(self, seconds=THUMBNAIL_TIMEOUT):
        self._deadline = time.monotonic() + seconds
        self._expired.clear()
        watchdog = Timer(seconds, self._expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            yield
            self._remaining()
        except Exception as exc:
            if self._expired.is_set() and not isinstance(exc, ThumbnailTimeout):
                raise ThumbnailTimeout("The TV thumbnail request exceeded its deadline.") from exc
            raise
        finally:
            watchdog.cancel()
            watchdog.join()
            with self._socket_lock:
                transfer = self._transfer_socket
                self._transfer_socket = None
            if transfer is not None:
                self._stop_socket(transfer)

    def _recv_frame(self):
        if self._deadline is not None:
            self.connection.settimeout(self._remaining())
        return super()._recv_frame()

    def _open_d2d_socket(self, conn_info):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._track_socket(sock)
            sock.settimeout(self._remaining())
            sock.connect((conn_info["ip"], int(conn_info["port"])))
            if conn_info.get("secured", False):
                # Track TLS before handshaking so the deadline can interrupt it.
                with self._socket_lock:
                    self._remaining()
                    sock = get_ssl_context().wrap_socket(sock, do_handshake_on_connect=False)
                    self._transfer_socket = sock
                sock.settimeout(self._remaining())
                sock.do_handshake()
            sock.settimeout(self._remaining())
            return sock
        except Exception:
            self._stop_socket(sock)
            raise
