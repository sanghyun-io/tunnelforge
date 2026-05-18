import io

from src.ui.workers.cross_engine_migration_worker import CrossEngineMigrationWorker


class FakeProcess:
    def __init__(self, stdout_lines, return_code=0, stderr=""):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("\n".join(stdout_lines) + "\n")
        self.stderr = io.StringIO(stderr)
        self.return_code = return_code
        self.terminated = False

    def wait(self):
        return self.return_code

    def poll(self):
        return None if not self.terminated else self.return_code

    def terminate(self):
        self.terminated = True


def test_worker_run_emits_result():
    process = FakeProcess([
        '{"event":"phase","phase":"preflight","message":"started"}',
        '{"event":"result","command":"preflight","success":true,"issues":[]}',
    ])

    worker = CrossEngineMigrationWorker(
        "preflight",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    phases = []
    results = []
    worker.phase_changed.connect(lambda phase, message: phases.append((phase, message)))
    worker.finished.connect(lambda success, payload: results.append((success, payload)))

    worker.run()

    assert phases == [("preflight", "started")]
    assert results[0][0] is True
    assert results[0][1]["command"] == "preflight"


def test_worker_run_emits_checkpoint_state():
    state = {"tables": [{"table": "users", "completed": False, "rows_copied": 2}]}
    process = FakeProcess([
        '{"event":"row_progress","table":"users","rows":2,"total":3,"state":{"tables":[{"table":"users","completed":false,"rows_copied":2}]}}',
        '{"event":"result","command":"migrate","success":false,"state":{"tables":[{"table":"users","completed":false,"rows_copied":2}]}}',
    ])

    worker = CrossEngineMigrationWorker(
        "migrate",
        {"source_engine": "mysql", "target_engine": "postgresql"},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    checkpoints = []
    worker.checkpoint.connect(checkpoints.append)
    worker.run()

    assert checkpoints == [state]


def test_worker_run_emits_failure_on_error():
    process = FakeProcess([
        '{"event":"error","message":"boom"}',
    ])

    worker = CrossEngineMigrationWorker(
        "plan",
        {},
        helper_path="fake-helper",
        popen_factory=lambda *args, **kwargs: process,
    )

    failures = []
    worker.failed.connect(failures.append)
    worker.run()

    assert failures == ["boom"]
