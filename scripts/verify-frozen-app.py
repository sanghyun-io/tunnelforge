#!/usr/bin/env python3
"""Launch a packaged app and verify its UI and bundled Rust Core handshake."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def verify_frozen_app(command, timeout=60):
    with tempfile.TemporaryDirectory(prefix='tunnelforge-smoke-') as temporary:
        output = Path(temporary) / 'result.json'
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', TUNNELFORGE_CLI_OUTPUT=str(output))
        completed = subprocess.run(
            [*command, '--ui-smoke-check'], env=env, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=timeout, check=True,
        )
        # Windows GUI builds have no stdout and write to the explicit fallback.
        payload = output.read_text(encoding='utf-8') if output.exists() else completed.stdout
        result = json.loads(payload)
        core = result['self_check']
        hello = core['core_hello']
        if not (
            result['success'] is True
            and result['window_title'] == 'TunnelForge'
            and core['success'] is True
            and core['icon_exists'] is True
            and core['core_exists'] is True
            and hello['success'] is True
            and hello['event'] == 'result'
            and hello['request_id'] == 'self-check'
            and hello['service'] == 'tunnelforge-core'
        ):
            raise ValueError(f'Frozen app smoke check failed: {payload}')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    try:
        result = verify_frozen_app([str(args.executable.resolve())], args.timeout)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'Frozen app smoke failed: {exc}', file=sys.stderr)
        if getattr(exc, 'stderr', None):
            print(exc.stderr, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
