"""Hermetic SSH protocol smoke: real encrypted channel, no external host."""
import socket
import threading
import time

import paramiko
import pytest

from src.core.tunnel_engine import TunnelEngine
from src.core.tunnel_monitor import TunnelMonitor, TunnelState


@pytest.mark.parametrize('tunnel_count,disconnect', [(1, False), (1, True), (3, True)])
def test_real_ssh_tunnel_forwards_bytes_and_releases_listener(tmp_path, monkeypatch, tunnel_count, disconnect):
    client_key = paramiko.RSAKey.generate(2048)
    host_key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "client.pem"
    client_key.write_private_key_file(str(key_path))
    destinations = []
    failures = []
    stopping = threading.Event()

    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return "publickey"

        def check_auth_publickey(self, username, key):
            if username == "test" and key == client_key:
                return paramiko.AUTH_SUCCESSFUL
            return paramiko.AUTH_FAILED

        def check_channel_direct_tcpip_request(self, chanid, origin, destination):
            destinations.append(destination)
            return paramiko.OPEN_SUCCEEDED

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    transports = []

    authenticated = []

    def handle(sock):
        transport = None
        try:
            transport = paramiko.Transport(sock)
            transports.append(transport)
            transport.add_server_key(host_key)
            transport.start_server(server=Server())
            while not stopping.is_set() and transport.is_active():
                if transport.is_authenticated() and transport not in authenticated:
                    authenticated.append(transport)
                channel = transport.accept(0.2)
                if channel is None:
                    continue
                channel.settimeout(3)
                with channel:
                    data = channel.recv(1024)
                    if data:
                        channel.sendall(data)
        except Exception as exc:
            # The engine's host-key probe hangs up right after key exchange, before any
            # authentication; that (and only that) is expected. Errors on authenticated
            # connections, i.e. the real tunnel, still count.
            probe = transport is None or not transport.is_authenticated()
            if not stopping.is_set() and not probe:
                failures.append(exc)

    def serve():
        # the engine first probes the host key (TOFU), then opens the real forwarder connection
        while not stopping.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=handle, args=(sock,), daemon=True).start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    class Store:
        entries = {}

        def get_known_host(self, host, port):
            return self.entries.get((host, port))

        def save_known_host(self, host, port, entry):
            self.entries[(host, port)] = entry

    engine = TunnelEngine(known_hosts=Store())
    engine.host_key_confirmer = lambda prompt: True
    monitor = TunnelMonitor(engine)
    monkeypatch.setattr(monitor, '_measure_latency', lambda tid: -1)
    monkeypatch.setattr('src.core.tunnel_monitor.RECONNECT_BACKOFF_SECONDS', (0,))
    endpoints = {}

    def wait_until(predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            assert time.monotonic() < deadline, 'local SSH recovery timed out'
            stopping.wait(0.01)

    def roundtrip(host, port):
        with socket.create_connection((host, port), timeout=5) as connection:
            payload = "SQL 결과 😀".encode() + b"\x00\xff"
            connection.sendall(payload)
            received = bytearray()
            while len(received) < len(payload):
                chunk = connection.recv(1024)
                assert chunk, "SSH channel closed before forwarding all bytes"
                received.extend(chunk)
            assert bytes(received) == payload

    try:
        for index in range(tunnel_count):
            tid = f'ssh-live-{index}'
            success, message = engine.start_tunnel({
                "id": tid, "name": "Local protocol smoke",
                "bastion_host": "127.0.0.1", "bastion_port": listener.getsockname()[1],
                "bastion_user": "test", "bastion_key": str(key_path),
                "remote_host": "db.test", "remote_port": 3306, "local_port": 0,
            })
            assert success, message
            host, port = engine.get_connection_info(tid)
            assert host == "127.0.0.1" and port > 0
            endpoints[tid] = host, port
            roundtrip(host, port)
            monitor.on_tunnel_connected(tid)

        if disconnect:
            # Drop actual authenticated SSH connections at the bastion side.
            for transport in list(authenticated):
                transport.close()
            wait_until(lambda: all(not engine.is_running(tid) for tid in endpoints))
            # Losing SSH does not itself release the local listening sockets.
            assert all(not engine.is_port_available(port) for _, port in endpoints.values())
            monitor._running = True
            monitor._check_all_tunnels()
            wait_until(lambda: all(monitor.get_status(tid).state == TunnelState.CONNECTED
                                   and engine.is_running(tid) for tid in endpoints))
            for tid, endpoint in endpoints.items():
                assert engine.get_connection_info(tid) == endpoint
                roundtrip(*endpoint)

        assert destinations and all(item == ("db.test", 3306) for item in destinations)
        for tid, (_, port) in endpoints.items():
            assert engine.stop_tunnel(tid)
            assert not engine.is_running(tid)
            assert engine.get_connection_info(tid) == (None, None)
            assert engine.is_port_available(port)
        assert not failures
        assert authenticated, "the real tunnel connection never authenticated"
    finally:
        stopping.set()
        monitor.stop_monitoring()
        engine.stop_all()
        for transport in transports:
            transport.close()
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
