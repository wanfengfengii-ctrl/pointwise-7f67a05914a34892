"""Joint verification for the overwrite handoff.

Runs the original unittest regression first, then the interleaved ledger that
exercises withdrawal, cross-recording preservation and close-after-settlement.
Any mismatch exits non-zero.
"""
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _run(argv):
    completed = subprocess.run(argv, cwd=ROOT)
    return completed.returncode


def main():
    if _run([sys.executable, '-B', 'check_project.py']) != 0:
        return 1
    if _run([sys.executable, '-B', '-m', 'survey.recording.station', 'demo']) != 0:
        return 1
    from examples import overwrite_interleave
    try:
        overwrite_interleave.verify()
    except AssertionError as error:
        print(f'INTERLEAVE FAIL {error}', file=sys.stderr)
        return 1
    print('INTERLEAVE SUMMARY result=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
