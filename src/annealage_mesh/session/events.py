"""EventLog: monotonic seq, a bounded in-memory ring, and an optional
append-only ``events.jsonl``.

Every server-to-browser event carries a seq number that never repeats and
never goes backwards for a given log (plan section 3.4). A reconnecting
browser sends the last seq it saw; this module decides how much of what
happened since then can be replayed straight back over the socket versus
having to be paged from disk, and never silently drops the difference.

Nothing here is wired into the CLI in M4: a file per session needs a
session, and sessions start in M5. ``EventLog`` still takes an optional
``path`` and is fully exercised directly, against a ``tmp_path``, so the
file-backed behaviour is proven before anything depends on it.

This is not the exchange-file threat model ``paths.py`` defends: those
files sit in a served project directory an outside party can also write
into, so every open there re-validates identity against a race. An
``events.jsonl`` lives inside this tool's own ``.mesh/`` control
directory (once M8 creates one), which nothing but this process ever
writes to, so there is no name to race and no symlink substitution to
guard against; the file is opened once, by name, and held for the log's
lifetime.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

# Bounded history kept in memory for a reconnect to replay without a disk
# read. 500 events comfortably outlasts a normal reconnect gap (dropped
# WiFi, laptop lid, a phone locking) without growing without bound across
# a long session; a gap wider than that is reported rather than guessed
# at, via Replay.truncated below.
RING_SIZE = 500


@dataclasses.dataclass(frozen=True)
class Replay:
    """The result of asking an ``EventLog`` to replay from a client's ``last_seq``.

    ``events`` is every ring-held ``(seq, wire_dict)`` pair newer than
    ``last_seq``, in seq order, ready to send straight back over the
    socket. ``truncated`` is True when ``last_seq`` predates what the ring
    still holds: some events between ``last_seq`` and the oldest one
    returned exist only in the append-only file (or, if no path was
    given, nowhere at all), and the caller must say so rather than
    replaying a partial history that looks complete. ``events`` is still
    populated in that case with whatever the ring does have, since
    telling the browser about a gap in its oldest history is no reason to
    also withhold the newer events that are available. ``truncated`` is
    also True when ``last_seq`` is below the log's own current position
    but the ring is empty (a restart recovers the seq counter but not the
    ring's contents), and when ``last_seq`` is higher than any seq this
    log has ever issued, which can only mean it belongs to a different
    run of the log than the one now being asked. In both cases real
    events exist between ``last_seq`` and the log's current position that
    this reply cannot hand back, which is a gap regardless of the ring's
    own state.
    """

    events: List[Tuple[int, dict]]
    truncated: bool


class EventLog:
    """Append-only event history for one session's lifetime.

    ``path``, when given, is opened once for appending (``O_APPEND`` makes
    each write atomic with respect to any other append to the same file
    descriptor, which matters only for the "one process, one writer"
    invariant this class itself maintains: nothing here defends against a
    second process writing the same path, that is ``.mesh/lock``'s job,
    owned elsewhere). Re-opening the same path in a fresh ``EventLog``
    picks up numbering where the file left off, so a restarted process
    (or, in tests, a second ``EventLog`` standing in for one) never
    reissues a seq a client may already have.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path is not None else None
        self._ring: collections.deque = collections.deque(maxlen=RING_SIZE)
        self._seq = 0
        self._fd: Optional[int] = None
        if self._path is not None:
            self._seq = self._recover_seq()
            self._fd = os.open(
                str(self._path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def _recover_seq(self) -> int:
        """The highest seq already written to ``self._path``, or 0.

        A new ``EventLog`` for a path that already has content must not
        restart numbering at 0: a client that saw seq 50 before a restart
        and reconnects with ``last_seq=50`` would otherwise be replayed
        seq 1 through 50 again, duplicating events it has already
        rendered. A malformed trailing line, from a process killed mid
        write, is skipped rather than raising: the recovered seq only
        needs to be at least as high as anything a client could have
        already been sent, and a torn last line was never sent to one
        either, so skipping it costs nothing a client could have seen.
        """
        if not self._path.exists():
            return 0
        highest = 0
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                seq = record.get("seq")
                if isinstance(seq, int) and seq > highest:
                    highest = seq
        return highest

    @property
    def current_seq(self) -> int:
        """The seq of the most recently appended event, or 0 if none yet.

        This is what a ``hello`` frame reports as its own ``seq``: the
        point in the stream a freshly connected client is starting from,
        with nothing before it to replay.
        """
        return self._seq

    def append(self, event) -> int:
        """Assign the next seq to ``event`` and record it. Returns that seq.

        ``event`` is anything with a ``to_wire()`` method returning a
        JSON-able dict (``session.base.AgentEvent`` and its subclasses);
        this module has no other dependency on that class, so a caller
        with some other object shaped the same way works identically.
        """
        self._seq += 1
        seq = self._seq
        wire = event.to_wire()
        self._ring.append((seq, wire))
        if self._fd is not None:
            line = json.dumps({"seq": seq, "event": wire}) + "\n"
            os.write(self._fd, line.encode("utf-8"))
        return seq

    def replay(self, last_seq: Optional[int]) -> Replay:
        """Everything the ring holds newer than ``last_seq``, plus whether
        that leaves a gap. ``last_seq=None`` means the client has no prior
        history at all (a first-ever connection), treated the same as 0.
        """
        if last_seq is None:
            last_seq = 0
        if last_seq > self._seq:
            # last_seq is higher than any seq this log has ever issued.
            # This log cannot be the one that produced it, most likely
            # because the process restarted and this is a fresh log
            # counting up from a lower recovered seq: nothing here can
            # answer for what came after a point it never reached, so the
            # whole ring is handed back rather than an empty reply that
            # would read as "already caught up".
            return Replay(events=list(self._ring), truncated=True)
        if not self._ring:
            # An empty ring says nothing about whether last_seq is caught
            # up: a restarted, path-backed log recovers self._seq from
            # the file but never repopulates the ring (see
            # _recover_seq), so last_seq below self._seq here means real
            # events exist only in the file, not that nothing has
            # happened since last_seq.
            return Replay(events=[], truncated=last_seq < self._seq)
        oldest = self._ring[0][0]
        # A gap exists whenever the ring's oldest kept event is not the
        # very next one the client is expecting: everything between
        # last_seq and oldest fell off the ring before this replay and is
        # not silently skippable, so the caller is told rather than
        # handed a history with a hole already cut out of it.
        truncated = last_seq < oldest - 1
        events = [(seq, wire) for seq, wire in self._ring if seq > last_seq]
        return Replay(events=events, truncated=truncated)

    def close(self) -> None:
        """Close the append-only file descriptor, if one is open.

        Idempotent, so a caller that closes explicitly and a shutdown
        path that closes again defensively do not double-close a
        descriptor number the OS may since have reissued to something
        else entirely.
        """
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
