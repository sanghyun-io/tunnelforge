"""Hermetic SSH protocol smoke: real encrypted channel, no external host."""
import socket
import threading

import paramiko

from src.core.tunnel_engine import TunnelEngine


def test_real_ssh_tunnel_forwards_bytes_and_releases_listener(tmp_path):
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
    listener.settimeout(5)
    transports = []

    def handle(sock):
        try:
            transport = paramiko.Transport(sock)
            transports.append(transport)
            transport.add_server_key(host_key)
            transport.start_server(server=Server())
            while not stopping.is_set() and transport.is_active():
                channel = transport.accept(0.2)
                if channel is None:
                    continue
                channel.settimeout(3)
                with channel:
                    data = channel.recv(1024)
                    if data:
                        channel.sendall(data)
        except Exception as exc:
            if not stopping.is_set():
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
    try:
        success, message = engine.start_tunnel({
            "id": "ssh-live", "name": "Local protocol smoke",
            "bastion_host": "127.0.0.1", "bastion_port": listener.getsockname()[1],
            "bastion_user": "test", "bastion_key": str(key_path),
            "remote_host": "db.test", "remote_port": 3306, "local_port": 0,
        })
        assert success, message
        host, port = engine.get_connection_info("ssh-live")
        assert host == "127.0.0.1" and port > 0
        with socket.create_connection((host, port), timeout=5) as connection:
            payload = "SQL 결과 😀".encode() + b"\x00\xff"
            connection.sendall(payload)
            received = bytearray()
            while len(received) < len(payload):
                chunk = connection.recv(1024)
                assert chunk, "SSH channel closed before forwarding all bytes"
                received.extend(chunk)
            assert bytes(received) == payload
        assert destinations and all(item == ("db.test", 3306) for item in destinations)
        assert engine.stop_tunnel("ssh-live")
        assert not engine.is_running("ssh-live")
        assert engine.get_connection_info("ssh-live") == (None, None)
        assert not failures
    finally:
        stopping.set()
        engine.stop_all()
        for transport in transports:
            transport.close()
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
