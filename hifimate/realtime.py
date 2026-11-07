"""Best-effort bounded RR launcher; never grants privileges or changes RT throttling."""
import gzip
import json
import os
from pathlib import Path
import resource
import sys

PRIORITY = 5
# RR applies to the whole process from exec, so one-time startup work counts:
# measured on PREEMPT_RT 6.12, MPD's main thread runs ~220 ms and a Shairport
# startup thread ~1.5 s without blocking. RLIMIT_RTTIME SIGKILLs a thread at
# the limit, so 200 ms (rtkit's per-audio-thread value) crashlooped both.
# 5 s still kills a runaway RT thread; kernel RT throttling stays in place.
RTTIME_US = 5000000


def kernel_realtime(sys_root=Path('/sys'), boot=Path('/boot'), proc=Path('/proc'), release=None):
    """True/False/None: current kernel evidence only, never uname suffix guessing."""
    try:
        value = (sys_root/'kernel/realtime').read_text().strip()
        if value in ('0', '1'):
            return value == '1'
    except OSError:
        pass
    release = release or os.uname().release
    for path in (proc/'config.gz', boot/f'config-{release}'):
        try:
            if path.suffix == '.gz':
                with gzip.open(path, 'rt') as config:
                    text = config.read()
            else:
                text = path.read_text()
        except (OSError, EOFError, UnicodeError):
            continue
        if 'CONFIG_PREEMPT_RT=y' in text.splitlines():
            return True
        if '# CONFIG_PREEMPT_RT is not set' in text.splitlines():
            return False
    return None


def configure(mode, kernel=kernel_realtime):
    if mode not in ('auto', 'on', 'off'):
        raise ValueError('invalid realtime mode')
    if mode == 'off' or (mode == 'auto' and kernel() is not True):
        return False
    # Keep existing stricter limits. Limit must be installed before RR is attempted.
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_RTTIME)
        bounded = RTTIME_US if soft == resource.RLIM_INFINITY else min(soft, RTTIME_US)
        if hard != resource.RLIM_INFINITY:
            bounded = min(bounded, hard)
        resource.setrlimit(resource.RLIMIT_RTTIME, (bounded, bounded))
        os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(PRIORITY))
    except (OSError, ValueError) as error:
        print(f'HiFiMate realtime unavailable; ordinary scheduling: {error}', file=sys.stderr)
        return False
    print(f'HiFiMate SCHED_RR priority={PRIORITY}, RTTIME soft={bounded}us', file=sys.stderr)
    return True


def main():
    if len(sys.argv) < 4 or sys.argv[2] != '--':
        raise SystemExit('usage: hifimate-rt CONFIG -- COMMAND [ARGS]')
    mode = json.loads(Path(sys.argv[1]).read_text())['mode']
    configure(mode)
    os.execvp(sys.argv[3], sys.argv[3:])


if __name__ == '__main__':
    main()
