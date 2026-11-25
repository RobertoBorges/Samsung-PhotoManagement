from threading import Event
from time import monotonic
import socket
import unittest
from unittest.mock import Mock, patch

from websocket import WebSocketConnectionClosedException

from thumbnails import ThumbnailArt, ThumbnailTimeout


class HeartbeatConnection:
    def __init__(self, internal_heartbeats=False):
        self.stopped = Event()
        self.receiving = Event()
        self.internal_heartbeats = internal_heartbeats

    def send(self, _data):
        pass

    def settimeout(self, _seconds):
        pass

    def abort(self):
        self.stopped.set()

    def recv(self):
        self.receiving.set()
        if self.internal_heartbeats:
            if not self.stopped.wait(1):
                raise AssertionError("The watchdog did not interrupt the receive.")
            raise WebSocketConnectionClosedException("Aborted by deadline")
        if self.stopped.is_set():
            raise WebSocketConnectionClosedException("Aborted by deadline")
        return '{"event":"ms.channel.clientConnect"}'


class ThumbnailDeadlineTests(unittest.TestCase):
    def test_deadline_is_not_reset_by_unrelated_messages_or_internal_pings(self):
        for internal in (False, True):
            with self.subTest(internal_heartbeats=internal):
                art = ThumbnailArt("127.0.0.1", key_press_delay=0)
                art.connection = HeartbeatConnection(internal)
                start = monotonic()
                with self.assertRaises(ThumbnailTimeout):
                    with art.request_deadline(0.05):
                        art.get_thumbnail_list(["MY_1"])
                self.assertLess(monotonic() - start, 0.8)
                self.assertTrue(art.connection.receiving.is_set())

    def test_deadline_interrupts_a_blocked_thumbnail_transfer(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        art = ThumbnailArt("127.0.0.1")
        art.connection = Mock()
        with self.assertRaises(ThumbnailTimeout):
            with art.request_deadline(0.05):
                art._track_socket(reader)
                reader.recv(4)
        self.assertEqual(reader.fileno(), -1)
        self.assertIsNone(art._transfer_socket)

    def test_successful_request_cancels_the_watchdog(self):
        art = ThumbnailArt("127.0.0.1")
        art.connection = Mock()
        with art.request_deadline(1):
            pass
        art.connection.abort.assert_not_called()
        self.assertFalse(art._expired.is_set())

    @patch("thumbnails.socket.socket")
    def test_d2d_socket_is_tracked_before_connect(self, factory):
        art = ThumbnailArt("127.0.0.1")
        art.connection = Mock()
        sock = factory.return_value
        sock.connect.side_effect = lambda _address: self.assertIs(art._transfer_socket, sock)
        with art.request_deadline(1):
            self.assertIs(art._open_d2d_socket({"ip": "127.0.0.1", "port": 1234}), sock)
        sock.close.assert_called_once()

    @patch("thumbnails.get_ssl_context")
    @patch("thumbnails.socket.socket")
    def test_tls_socket_is_tracked_before_handshake(self, factory, ssl_context):
        art = ThumbnailArt("127.0.0.1")
        art.connection = Mock()
        secured = ssl_context.return_value.wrap_socket.return_value
        secured.do_handshake.side_effect = lambda: self.assertIs(art._transfer_socket, secured)
        with art.request_deadline(1):
            result = art._open_d2d_socket({"ip": "127.0.0.1", "port": 1234, "secured": True})
            self.assertIs(result, secured)
        ssl_context.return_value.wrap_socket.assert_called_once_with(
            factory.return_value, do_handshake_on_connect=False
        )
        secured.close.assert_called_once()
