class SustainController:
    """Press-and-hold-then-confirm key driver, timed in seconds (not frames).

    A sustain's trail only renders while the key is held, so every note is pressed and
    held for a short probe. If the trail lights up (lane in `trails_lit`) the note is a
    sustain and the key stays down until the trail has been gone for `release_grace_s`;
    otherwise the probe expires after `probe_s` and the key releases, behaving as a tap.
    Releasing on a wall-clock timer keeps behaviour identical no matter the loop frame
    rate (frame-based timing stretched under diagnostic load and merged rapid taps).
    """

    def __init__(self, press_fn, release_fn, probe_s, release_grace_s):
        self._press = press_fn
        self._release = release_fn
        self._probe_s = probe_s
        self._release_grace_s = release_grace_s
        self._state = {}
        self._prev_notes = set()

    def update(self, notes_at_hitline, trails_lit, now):
        rising = set(notes_at_hitline) - self._prev_notes
        self._prev_notes = set(notes_at_hitline)
        for lane in notes_at_hitline:
            if lane not in self._state:
                self._press(lane)
                self._state[lane] = {"press_t": now, "confirmed": False, "last_trail_t": now}
            elif lane in rising:
                # A NEW note's head appears (rising edge) in a lane that's already held —
                # the held key would absorb it. Re-strum: release and press again. A single
                # sustain shows no rising edges mid-hold (its head isn't re-detected), so
                # this only fires for genuinely new notes (e.g. dense same-lane streams).
                self._release(lane)
                self._press(lane)
                self._state[lane] = {"press_t": now, "confirmed": False, "last_trail_t": now}

        for lane in list(self._state):
            state = self._state[lane]
            if lane in trails_lit:
                state["confirmed"] = True
                state["last_trail_t"] = now

            if state["confirmed"]:
                if now - state["last_trail_t"] >= self._release_grace_s:
                    self._release(lane)
                    del self._state[lane]
            elif now - state["press_t"] >= self._probe_s:
                # Tap: release once the probe expires so the lane is free for the next
                # press; a still-present note re-triggers a fresh press next frame.
                self._release(lane)
                del self._state[lane]

    def held_lanes(self):
        return frozenset(self._state)

    def release_all(self):
        for lane in list(self._state):
            self._release(lane)
        self._state.clear()
