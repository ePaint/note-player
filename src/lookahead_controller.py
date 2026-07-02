class LookaheadController:
    """Proactive key driver: schedule presses ahead of the hit line instead of reacting at it.

    A note is detected at an UPPER strip (well above the fret). Its press is scheduled for
    `lead_s` later — the measured time for a note to travel from the upper strip to the fret,
    minus input latency — so the keypress *registers* exactly at the fret (PERFECT), and a
    momentary detection dropout at the fret can't cause a miss (the press was committed early).
    After firing, the key is held while the note is present at the FRET strip (a sustain's
    trail keeps it there) and released once the fret clears for `release_grace_s`.
    """

    def __init__(self, press_fn, release_fn, lead_s, release_grace_s):
        self._press = press_fn
        self._release = release_fn
        self._lead_s = lead_s
        self._release_grace_s = release_grace_s
        self._scheduled = {}      # lane -> fire_time
        self._held = {}           # lane -> last_fret_time
        self._prev_upper = set()
        self._prev_fret = set()

    def _fire(self, lane, now):
        self._press(lane)
        self._held[lane] = now
        self._scheduled.pop(lane, None)

    def update(self, upper_present, fret_present, now):
        for lane in set(upper_present) - self._prev_upper:
            # New note at the upper strip -> schedule its press for the fret arrival.
            # Don't reschedule a lane already scheduled or currently held (avoids duplicates
            # from a note lingering across the upper strip, or a sustain trail sitting there).
            if lane not in self._scheduled and lane not in self._held:
                self._scheduled[lane] = now + self._lead_s
        self._prev_upper = set(upper_present)

        for lane in list(self._scheduled):
            if now >= self._scheduled[lane]:
                self._fire(lane, now)

        # Reactive fallback: a note reaches the fret that was never scheduled (the upper
        # strip missed it, ~14%). Catch it now — GOOD beats a miss. Notes seen up top still
        # fire early (PERFECT); only the missed-up-top ones fall back to here.
        for lane in set(fret_present) - self._prev_fret:
            if lane not in self._held and lane not in self._scheduled:
                self._fire(lane, now)
        self._prev_fret = set(fret_present)

        for lane in list(self._held):
            if lane in fret_present:
                self._held[lane] = now
            elif now - self._held[lane] >= self._release_grace_s:
                self._release(lane)
                del self._held[lane]

    def held_lanes(self):
        return frozenset(self._held)

    def release_all(self):
        for lane in list(self._held):
            self._release(lane)
        self._held.clear()
        self._scheduled.clear()
