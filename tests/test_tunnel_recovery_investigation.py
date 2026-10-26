"""TF-STATUS-214 recovery regressions; no external SSH/DB endpoints."""
import logging
import socketserver
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sshtunnel import SSHTunnelForwarder

from src.core.tunnel_engine import TunnelEngine
from src.core.tunnel_monitor import TunnelMonitor, TunnelState


@pytest.fixture(autouse=True)
def ephemeral_port(sample_tunnel_config):
    sample_tunnel_config['local_port'] = 0


def schedule(monitor, tid):
    monitor._running = True
    with patch('src.core.tunnel_monitor.threading.Thread'):
        monitor._attempt_reconnect(tid)
    return monitor._reconnect_jobs[tid]


def test_dead_real_forwarder_can_be_stopped(sample_tunnel_config):
    with socketserver.TCPServer(('127.0.0.1', 0), socketserver.BaseRequestHandler) as listener:
        server = object.__new__(SSHTunnelForwarder)
        server._transport = SimpleNamespace(is_active=lambda: False)
        server.is_alive = True
        server._server_list = [listener]
        server.logger = logging.getLogger('tunnel-recovery-investigation')
        server.stop = Mock(side_effect=lambda **kwargs: listener.server_close())
        engine = TunnelEngine()
        tid = sample_tunnel_config['id']
        engine.active_tunnels[tid] = server
        engine.tunnel_configs[tid] = sample_tunnel_config
        port = listener.server_address[1]
        assert not engine.is_running(tid)
        assert not engine.is_port_available(port)
        assert engine.stop_tunnel(tid)
        server.stop.assert_called_once_with(force=True)
        assert engine.is_port_available(port)
        assert tid not in engine.active_tunnels
        assert engine.get_cleanup_error(tid) is None


def test_second_retry_keeps_config_after_first_failure(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    engine.active_tunnels[tid] = Mock(is_active=False, local_bind_port=3307)
    engine.tunnel_configs[tid] = sample_tunnel_config
    monitor = TunnelMonitor(engine)
    status = monitor.get_status(tid)
    job = schedule(monitor, tid)

    def start(config):
        if tid in engine.tunnel_configs:
            raise AssertionError('stale registry was not cleaned')
        if start_mock.call_count == 1:
            return False, 'temporary outage'
        engine.active_tunnels[tid] = Mock(is_active=True)
        engine.tunnel_configs[tid] = config
        return True, 'recovered'

    with patch.object(engine, '_start_ssh_tunnel', side_effect=start) as start_mock:
        with patch('src.core.tunnel_monitor.threading.Thread'):
            monitor._reconnect_after_delay(tid, 0, status, job)
            assert status.state == TunnelState.RECONNECTING
            assert tid not in engine.tunnel_configs
            monitor._reconnect_after_delay(tid, 0, status, job)
    assert start_mock.call_count == 2
    assert status.state == TunnelState.CONNECTED
    assert status.error_message is None


def test_retry_limit_counts_actual_attempts(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    engine.tunnel_configs[tid] = sample_tunnel_config
    monitor = TunnelMonitor(engine)
    status = monitor.get_status(tid)
    job = schedule(monitor, tid)
    with patch.object(engine, '_start_ssh_tunnel', return_value=(False, 'outage')) as start:
        with patch('src.core.tunnel_monitor.threading.Thread'):
            for _ in range(5):
                monitor._reconnect_after_delay(tid, 0, status, job)
    assert start.call_count == 5
    assert status.state == TunnelState.ERROR
    assert 'outage' in status.error_message
    assert tid not in monitor._reconnect_jobs


def test_pending_retry_does_not_stop_a_new_manual_connection(sample_direct_config):
    engine = TunnelEngine()
    tid = sample_direct_config['id']
    engine.tunnel_configs[tid] = sample_direct_config
    monitor = TunnelMonitor(engine)
    status = monitor.get_status(tid)
    job = schedule(monitor, tid)
    assert engine.start_tunnel(sample_direct_config)[0]
    with patch.object(engine, '_stop_tunnel_locked') as stop:
        monitor._reconnect_after_delay(tid, 0, status, job)
    stop.assert_not_called()
    assert engine.is_running(tid)
    assert tid not in monitor._reconnect_jobs


def test_manual_stop_cancels_retry_even_after_registry_was_removed(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    engine.tunnel_configs[tid] = sample_tunnel_config
    monitor = TunnelMonitor(engine)
    status = monitor.get_status(tid)
    job = schedule(monitor, tid)
    assert not engine.stop_tunnel(tid)
    with patch.object(engine, '_start_ssh_tunnel') as start:
        monitor._reconnect_after_delay(tid, 0, status, job)
    start.assert_not_called()
    assert engine.get_reconnect_context(tid)[1] is None


def test_disable_or_shutdown_cancels_pending_recovery(sample_tunnel_config):
    for shutdown in (False, True):
        engine = TunnelEngine()
        tid = sample_tunnel_config['id']
        engine.tunnel_configs[tid] = sample_tunnel_config
        monitor = TunnelMonitor(engine)
        status = monitor.get_status(tid)
        job = schedule(monitor, tid)
        if shutdown:
            monitor.stop_monitoring()
        else:
            monitor.set_auto_reconnect(False)
        with patch.object(engine, '_start_ssh_tunnel') as start:
            monitor._reconnect_after_delay(tid, 0, status, job)
        start.assert_not_called()
        assert job.cancelled.is_set()


def test_cleanup_failure_blocks_new_forwarder_and_keeps_owner(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    server = Mock(is_active=False, local_bind_port=3307)
    server.stop.side_effect = RuntimeError('listener still owned')
    engine.active_tunnels[tid] = server
    engine.tunnel_configs[tid] = sample_tunnel_config
    generation, _ = engine.get_reconnect_context(tid)
    with patch.object(engine, '_start_ssh_tunnel') as start:
        assert not engine.reconnect_tunnel(sample_tunnel_config, generation)[0]
    start.assert_not_called()
    assert engine.active_tunnels[tid] is server
    assert engine.get_cleanup_error(tid) == 'listener still owned'


def test_manual_stop_during_recovery_closes_late_result(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    engine.tunnel_configs[tid] = sample_tunnel_config
    entered = threading.Event()
    release = threading.Event()
    requested_stop = threading.Event()
    server = Mock(is_active=True, local_bind_port=3307)
    results = []

    def connect(config):
        entered.set()
        assert release.wait(5)
        engine.active_tunnels[tid] = server
        engine.tunnel_configs[tid] = config
        return True, 'connected'

    new_generation = engine._new_generation

    def signal_stop(tunnel_id, desired_running):
        generation = new_generation(tunnel_id, desired_running)
        if not desired_running:
            requested_stop.set()
        return generation

    with patch.object(engine, '_start_ssh_tunnel', side_effect=connect), patch.object(engine, '_new_generation', side_effect=signal_stop):
        worker = threading.Thread(target=lambda: results.append(engine.reconnect_tunnel(sample_tunnel_config, 0)))
        stopper = threading.Thread(target=lambda: engine.stop_tunnel(tid))
        worker.start()
        try:
            assert entered.wait(5)
            stopper.start()
            assert requested_stop.wait(5)
        finally:
            release.set()
            worker.join(5)
            if stopper.ident:
                stopper.join(5)
    assert not worker.is_alive() and not stopper.is_alive()
    assert results == [None]
    assert tid not in engine.active_tunnels
    server.stop.assert_called_once_with(force=True)


def test_stale_worker_cancellation_cannot_stop_a_newer_connection(sample_direct_config):
    engine = TunnelEngine()
    tid = sample_direct_config['id']
    assert engine.start_tunnel(sample_direct_config)[0]
    old_generation = engine.get_thread_start_generation()
    assert engine.start_tunnel(sample_direct_config)[0]
    assert not engine.stop_tunnel(tid, expected_generation=old_generation)
    assert engine.is_running(tid)


def test_cancel_during_connect_closes_late_result(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    cancelled = threading.Event()
    server = Mock(is_active=True, local_bind_port=3307)

    def connect(config):
        engine.active_tunnels[tid] = server
        engine.tunnel_configs[tid] = config
        cancelled.set()
        return True, 'connected'

    with patch.object(engine, '_start_ssh_tunnel', side_effect=connect):
        assert engine.reconnect_tunnel(sample_tunnel_config, 0, cancelled) is None
    assert not engine.is_running(tid)
    server.stop.assert_called_once_with(force=True)


def test_stop_uses_saved_port_when_dead_property_raises(sample_tunnel_config):
    from unittest.mock import PropertyMock
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    server = Mock(is_active=True, local_bind_port=43210)
    with patch.object(engine, '_build_forwarder', return_value=server), patch.object(engine, '_load_private_key'):
        assert engine.start_tunnel(sample_tunnel_config)[0]
    server.is_active = False
    type(server).local_bind_port = PropertyMock(side_effect=RuntimeError('dead transport'))
    with patch('src.core.tunnel_engine.unregister_endpoint_tls') as unregister:
        assert engine.stop_tunnel(tid)
    unregister.assert_called_once_with('127.0.0.1', 43210, tid)


def test_other_tunnel_can_start_while_one_is_connecting(sample_tunnel_config, sample_direct_config):
    engine = TunnelEngine()
    entered, release = threading.Event(), threading.Event()
    results = []

    def connect(config):
        entered.set()
        assert release.wait(5)
        return False, 'test outage'

    with patch.object(engine, '_start_ssh_tunnel', side_effect=connect):
        worker = threading.Thread(target=lambda: results.append(engine.start_tunnel(sample_tunnel_config)))
        worker.start()
        try:
            assert entered.wait(5)
            assert engine.start_tunnel(sample_direct_config)[0]
        finally:
            release.set()
            worker.join(5)
    assert results == [(False, 'test outage')]
    assert not worker.is_alive()


def test_same_tunnel_starts_are_serialized(sample_tunnel_config):
    engine = TunnelEngine()
    tid = sample_tunnel_config['id']
    entered, release, newer_requested = threading.Event(), threading.Event(), threading.Event()
    servers, results = [], []
    new_generation = engine._new_generation

    def generation(*args):
        value = new_generation(*args)
        if value == 2:
            newer_requested.set()
        return value

    def connect(config):
        server = Mock(is_active=True, local_bind_port=3307)
        servers.append(server)
        if len(servers) == 1:
            entered.set()
            assert release.wait(5)
        engine.active_tunnels[tid] = server
        engine.tunnel_configs[tid] = config
        return True, 'connected'

    with patch.object(engine, '_start_ssh_tunnel', side_effect=connect), \
            patch.object(engine, '_new_generation', side_effect=generation):
        first = threading.Thread(target=lambda: results.append(engine.start_tunnel(sample_tunnel_config)))
        second = threading.Thread(target=lambda: results.append(engine.start_tunnel(sample_tunnel_config)))
        first.start()
        try:
            assert entered.wait(5)
            second.start()
            assert newer_requested.wait(5)
            assert len(servers) == 1
        finally:
            release.set()
            first.join(5)
            if second.ident:
                second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(servers) == 2
    assert sorted(result[0] for result in results) == [False, True]
    servers[0].stop.assert_called_once_with(force=True)
    servers[1].stop.assert_not_called()
    assert engine.active_tunnels[tid] is servers[1]
