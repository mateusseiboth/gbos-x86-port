#!/usr/bin/env python3
"""Print the guest's boot phases while it boots (the window stays black for a while).

  progress.py SERIAL_LOG QEMU_PID

Follows the serial log until QEMU exits and prints one line per phase with the elapsed time.
"""
import os
import re
import sys
import time

# First match wins. Android's log lines start with "MM-DD hh:mm:ss.mmm".
PHASES = [
    (re.compile(r'Linux version '), 'kernel started'),
    (re.compile(r'Kernel panic'), 'KERNEL PANIC (see the serial log)'),
    (re.compile(r'reboot: Restarting system'), 'guest is rebooting'),
    (re.compile(r'SurfaceFlinger: Enter boot animation'), 'boot animation on screen'),
    (re.compile(r'SurfaceFlinger: Boot is finished'), 'boot completed'),
    (re.compile(r'^\d\d-\d\d \d\d:\d\d:\d\d\.\d+ '), 'Android userspace running'),
]

# A phase that comes back to the start: everything may be reported again.
RESTARTS = {'kernel started', 'guest is rebooting'}


def find_phase(line):
    return next((msg for rx, msg in PHASES if rx.search(line)), None)


def follow_phases(lines):
    """Each phase once, in the order it is reached; a reboot or new kernel starts over."""
    seen = set()
    for line in lines:
        phase = find_phase(line)
        if phase is None or phase in seen:
            continue
        seen = set() if phase in RESTARTS else seen
        seen.add(phase)
        yield phase


def tail_lines(path, is_alive, poll=0.5):
    """Complete lines of a file that is still being written, until is_alive() turns false."""
    while not os.path.exists(path) and is_alive():
        time.sleep(poll)
    with open(path, 'r', errors='replace') as f:
        pending = ''
        while True:
            chunk = f.read()
            if chunk:
                *done, pending = (pending + chunk).split('\n')
                yield from done
                continue
            if not is_alive():
                return
            time.sleep(poll)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    path, pid = sys.argv[1], int(sys.argv[2])
    start = time.monotonic()
    for phase in follow_phases(tail_lines(path, lambda: _alive(pid))):
        print(f'[gbos {time.monotonic() - start:5.0f}s] {phase}', file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
