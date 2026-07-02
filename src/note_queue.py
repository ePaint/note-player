import threading


class GemTrackQueue:
    """Tracks each note gem as a physical object descending the highway; presses once per gem.

    Matching is by POSITION CONTINUITY, not by an arrival window: a gem seen at y this frame was
    at (y - v*dt) last frame, so we match a detection to the in-lane track whose predicted current
    position is closest (within match_px). A dense same-lane stream (L L L) therefore becomes
    several distinct tracks -> several distinct presses, while one sustain gem stays a single
    track -> a single hold. This is what fixes "press L, nothing, press L": the middle gem is a
    new track and gets its own keydown.

    Two clocks (like NoteEventQueue): detection (slow) feeds observe()/service_holds(); a fast
    ~1ms clock calls fire_due() so presses land at the exact predicted arrival. When a track fires
    into a lane that is still held from the previous gem, it RE-STRUMS (release then press) so the
    game sees a fresh keydown edge. All state is under _lock.
    """

    def __init__(self, press_fn, release_fn, fret_y, px_per_s, latency_s, release_grace_s,
                 match_px=45.0, prune_s=0.6, restrum_gap_s=0.03, min_press_interval_s=0.08,
                 flash_suppress_s=0.12, tap_hold_s=0.06,
                 beam_confirm_px=80, beam_sustain_px=45, max_hold_s=4.0,
                 flash_settle_s=0.0, flash_zone_px=25.0, flash_zone_s=0.25, trail_margin_px=30.0,
                 trail_certain_px=88, trail_early_s=0.08, lift_window_s=0.13, lift_fwd_s=0.07,
                 promote_v_min=400.0, promote_v_max=1300.0, provisional_ttl_s=0.08,
                 promote_min_span_s=0.03, promote_min_dy_px=25.0, promote_lead_px=45.0,
                 lift_beam_slack_px=45.0, on_reject=None, px_scale=1.0):
        self._press = press_fn
        self._release = release_fn
        self._fret_y = fret_y
        self._px_per_s = px_per_s
        self._latency_s = latency_s
        self._release_grace_s = release_grace_s
        self._match_px = match_px
        self._prune_s = prune_s
        self._restrum_gap_s = restrum_gap_s
        self._min_press_interval_s = min_press_interval_s
        self._flash_suppress_s = flash_suppress_s
        self._tap_hold_s = tap_hold_s
        self._beam_confirm_px = beam_confirm_px
        self._beam_sustain_px = beam_sustain_px
        self._max_hold_s = max_hold_s
        self._flash_settle_s = flash_settle_s
        self._flash_zone_px = flash_zone_px
        self._flash_zone_s = flash_zone_s
        self._trail_margin_px = trail_margin_px
        self._trail_certain_px = trail_certain_px
        self._trail_early_s = trail_early_s
        self._lift_window_s = lift_window_s
        self._lift_fwd_s = lift_fwd_s
        self._lift_t = {}        # lane -> release time of the last lift (armed hold or scheduled)
        self._release_at = {}    # lane -> scheduled lift release time (from arrow geometry)
        self._lift_events = []   # (lane, t, source) lifts awaiting drain_lifts() (diagnostic log)
        self._promote_v_min = promote_v_min
        self._promote_v_max = promote_v_max
        self._provisional_ttl_s = provisional_ttl_s
        self._promote_min_span_s = promote_min_span_s
        self._promote_min_dy_px = promote_min_dy_px
        self._promote_lead_px = promote_lead_px
        self._lift_beam_slack_px = lift_beam_slack_px
        self._arrow_drop_zone_px = max(1, int(40 * px_scale + 0.5))
        self._on_reject = on_reject  # optional (reason, lane, y, now) hook: answers "why no press?"
        self._provisional = {}   # lane -> [(y, t), ...] suppressed sightings awaiting velocity proof
        self._beam_last = {}     # lane -> (height_px, t) freshest NONZERO beam reading this hold
        self._beam_hist = {}     # lane -> [(height_px, t), ...] recent readings (drain-rate gate)
        self._trail_armed = set()  # lanes whose CURRENT hold showed a certain trail EARLY (real sustain)
        self._hold_beam_px = {}  # lane -> max beam height seen during the CURRENT hold
        self._tracks = []        # {lane, y, seen, arrival, fired, pre_released}
        self._held = {}          # lane -> press time
        self._beam_anchor = {}     # lane -> last time a sustain beam was seen while held
        self._beam_confirmed = set()  # lanes whose current note is a confirmed sustain
        self._last_press = {}    # lane -> time of last actual keydown (refractory)
        self._lock = threading.Lock()

    def _arrival(self, y, now):
        return now + max(0.0, self._fret_y - y) / self._px_per_s

    def _end_hold(self, lane, now=None):
        self._release(lane)
        del self._held[lane]
        self._beam_anchor.pop(lane, None)
        self._beam_confirmed.discard(lane)
        self._hold_beam_px.pop(lane, None)
        self._beam_last.pop(lane, None)
        self._beam_hist.pop(lane, None)
        self._trail_armed.discard(lane)

    def _begin_hold(self, lane, now):
        self._held[lane] = now
        self._beam_anchor.pop(lane, None)      # fresh note -> re-earn its beam
        self._beam_confirmed.discard(lane)
        self._hold_beam_px[lane] = 0
        self._beam_last.pop(lane, None)
        self._beam_hist.pop(lane, None)
        self._trail_armed.discard(lane)
        self._lift_t.pop(lane, None)
        self._release_at.pop(lane, None)
        self._last_press[lane] = now

    def observe(self, detections, now):
        with self._lock:
            for det in detections:
                lane, y = det[0], det[1]
                arrow_y = det[2] if len(det) > 2 else None
                if arrow_y is not None and y > self._fret_y - self._arrow_drop_zone_px:
                    # near the fret the head+glow cluster fakes the same hollow geometry; the
                    # lift time was already learned from higher sightings, so drop late tags.
                    arrow_y = None
                best = None
                best_d = self._match_px + 1.0
                for t in self._tracks:
                    if t["lane"] != lane:
                        continue
                    predicted_y = t["y"] + self._px_per_s * (now - t["seen"])
                    d = abs(predicted_y - y)
                    if d < best_d:
                        best_d = d
                        best = t
                if best is not None and best_d <= self._match_px:
                    best["y"] = y
                    best["seen"] = now
                    best["arrival"] = self._arrival(y, now)   # refine with the closest sighting
                    if arrow_y is not None:
                        best["lift_at"] = self._arrival(arrow_y, now)
                        if best["fired"] and lane in self._release_at:
                            # the tube keeps refining its own lift moment while held
                            self._release_at[lane] = best["lift_at"] - self._latency_s
                else:
                    # Reject a NEW track in a lane we just hit: the hit-flash blooms a bright burst
                    # (+ particles) up into the band, and those aren't real notes -> they'd fire
                    # later as overstrum. A real note is detected high, many frames before the fret;
                    # a genuine repeat arrives >= a musical gap (>flash window) later, so it survives.
                    # A sustain's bright body also fragments into mid-band "gems"; firing one
                    # would re-strum (break) the very hold. Anything first seen INSIDE the span
                    # the trail has covered this hold is junk — a real next note is first seen
                    # above the trail top. Checked FIRST and kept position-hard: tube segments
                    # DO descend at scroll speed, so the velocity gate below must not see them.
                    trail = self._hold_beam_px.get(lane, 0)
                    if (lane in self._held and lane in self._trail_armed
                            and y > self._fret_y - trail - self._trail_margin_px):
                        if self._on_reject:
                            self._on_reject("in_trail", lane, y, now)
                        continue
                    # The hit flash reads as "gems" too: bloom/sparkle near the fret for as
                    # long as the flash lives, plus junk anywhere in-lane right after a press.
                    # Position alone cannot separate that from a real next note on fast chord
                    # walls (~113ms same-lane spacing puts it mid-band at press time) — but
                    # junk is STATIONARY while notes descend at scroll speed (~570-840px/s
                    # with perspective), so suppressed sightings accumulate as provisionals and
                    # a sighting with in-range velocity against one of them promotes to a track.
                    # Velocity alone is spoofable at high fps (run-15 regression): frame-pair
                    # jitter (3px/5ms = 600px/s) and junk-cluster ensembles (29px/44ms) both
                    # read as scroll speed, and the promoted phantom re-strums — i.e. RELEASES —
                    # the real hold it was born under. Hence two extra gates: the velocity
                    # baseline must be long (>= min_span AND >= min_dy: crawling junk covers
                    # ~13px per 80ms window and can never qualify) and the promoting sighting
                    # must sit >= promote_lead ABOVE the fret (fret-zone junk can fake any
                    # trajectory; real notes are promotable well before they get that deep).
                    # The history is a LIST: a single slot got clobbered by interleaved junk
                    # and starved real notes (velocity computed across different objects).
                    last = self._last_press.get(lane)
                    suppressed = last is not None and now - last < self._flash_suppress_s
                    deep = y > self._fret_y - self._flash_zone_px
                    flashing = lane in self._held or (last is not None and now - last < self._flash_zone_s)
                    promoted = None
                    if suppressed or (deep and flashing):
                        # keep same-timestamp entries: a junk sibling detected in the same frame
                        # must not erase the real note's history (min_span skips them anyway)
                        hist = [(py, pt) for py, pt in self._provisional.get(lane, [])
                                if 0 <= now - pt <= self._provisional_ttl_s]
                        if y <= self._fret_y - self._promote_lead_px:
                            for py, pt in hist:
                                span = now - pt
                                if span < self._promote_min_span_s or y - py < self._promote_min_dy_px:
                                    continue
                                if self._promote_v_min <= (y - py) / span <= self._promote_v_max:
                                    promoted = (py, pt)
                                    break
                        if not promoted:
                            hist.append((y, now))
                            self._provisional[lane] = hist[-12:]
                            if self._on_reject:
                                self._on_reject("flash_suppress" if suppressed else "flash_zone",
                                                lane, y, now)
                            continue
                        self._provisional.pop(lane, None)
                    elif y > self._fret_y - self._promote_lead_px:
                        # A new track may only be BORN above the promote-lead line. The flash
                        # windows are races junk can win: run-16's phantom re-strums were all
                        # bloom sightings first seen at y162-179 — past the 120ms suppress
                        # window (bloom lives ~150ms) yet above the flash-zone depth — that
                        # birthed here ungated and re-strummed a held lane onto nothing
                        # (11 of the run's 12 mistakes). At any healthy fps a real note is
                        # sighted dozens of frames above this line, so a first sighting this
                        # deep is junk; the known cost is a note already deep at engine start
                        # (autoplay toggled mid-song), which drops at most one press at 1x.
                        hist = [(py, pt) for py, pt in self._provisional.get(lane, [])
                                if 0 <= now - pt <= self._provisional_ttl_s]
                        hist.append((y, now))
                        self._provisional[lane] = hist[-12:]
                        if self._on_reject:
                            self._on_reject("deep_birth", lane, y, now)
                        continue
                    # (y0, t0) = the track's descent baseline: its first sighting, or for a
                    # promoted birth the provisional entry that proved its velocity. fire_due
                    # requires net descent from here before pressing (notes only ever descend).
                    y0, t0 = promoted if promoted else (y, now)
                    track = {"lane": lane, "y": y, "seen": now, "y0": y0, "t0": t0,
                             "arrival": self._arrival(y, now), "fired": False}
                    if arrow_y is not None:
                        track["lift_at"] = self._arrival(arrow_y, now)
                    self._tracks.append(track)

    def fire_due(self, now):
        """Fire every track whose predicted arrival has come (FAST clock).

        Re-strum with a REAL gap: the game samples key STATE at its own frame rate (~60fps), so a
        release immediately followed by a press is never sampled as a key-up -> the repeated note
        is invisible ("press L, nothing, press L"). So when the next gem targets a still-held lane,
        we PRE-RELEASE the key `restrum_gap_s` early; the key then sits up for a game-frame or two
        before the press, giving a keydown edge the game actually registers."""
        fired = False
        with self._lock:
            # Scheduled arrow LIFTS run on the fast clock like presses: the key must come UP
            # when the arrow reaches the fret (led by the same input latency as presses).
            for lane, t_rel in list(self._release_at.items()):
                if now >= t_rel:
                    self._release_at.pop(lane)
                    if lane in self._held:
                        self._end_hold(lane, now)
                    self._lift_t[lane] = now
                    self._lift_events.append((lane, now, "sched"))
            for t in self._tracks:
                if t["fired"]:
                    continue
                fire_at = t["arrival"] - self._latency_s
                if now < fire_at - self._restrum_gap_s:
                    continue                          # not time to engage this track yet
                # REFRACTORY: a lane can't hold two real notes within min_press_interval_s, so a
                # track trying to fire that soon after this lane's last keydown is a duplicate of
                # the note just played (one gem that spawned two tracks) -> consume it silently.
                # Checked BEFORE pre-release so it never lifts the real note being held.
                last = self._last_press.get(t["lane"])
                if last is not None and now - last < self._min_press_interval_s:
                    t["fired"] = True                 # drop the duplicate; no press, no pre-release
                    if self._on_reject:
                        self._on_reject("refractory", t["lane"], t["y"], now)
                    continue
                # ARROW LIFT (user-confirmed mechanic): the arrow at a thick hold's end demands
                # a RELEASE, never a press. A track due in a held lane whose tube CONFIRMED
                # (post-settle beam evidence — dense-run spoofs haven't confirmed by the time
                # their next note engages) is the arrow: defer past pre-release, then lift.
                # The arrow rides the TUBE'S END, so at any moment the remaining trail equals
                # the arrow's height above the fret. A track due over a YOUNG sustain instead
                # reads beam >> distance (run-16 +56.49: 128px trail vs 33px to go — a real
                # repeat note, and the lift ate it -> miss reset). Only defer when a FRESH
                # beam reading matches the track's remaining distance AND the beam has been
                # DRAINING at ~scroll speed: a genuine tube-end shrinks with the highway
                # (run-17 live arrow: 816px/s) while an under-read sustain trail wobbles
                # flat (run-18: six resets from ~70px flat readings that matched
                # |beam - dist| by coincidence and lifted away real notes). Anything else
                # falls through and re-strums like any next note.
                if t["lane"] in self._held and t["lane"] in self._beam_confirmed:
                    beam = self._beam_last.get(t["lane"])
                    predicted_y = t["y"] + self._px_per_s * (now - t["seen"])
                    dist = max(0.0, self._fret_y - predicted_y)
                    draining = False
                    if beam is not None:
                        for h_old, t_old in self._beam_hist.get(t["lane"], []):
                            span = beam[1] - t_old
                            if 0.04 <= span <= 0.22:
                                rate = (h_old - beam[0]) / span
                                if 0.6 * self._px_per_s <= rate <= 1.5 * self._px_per_s:
                                    draining = True
                                    break
                    if (draining and now - beam[1] <= self._release_grace_s
                            and abs(beam[0] - dist) <= self._lift_beam_slack_px):
                        if now >= fire_at:
                            self._end_hold(t["lane"], now)
                            self._lift_t[t["lane"]] = now
                            self._lift_events.append((t["lane"], now, "deferred"))
                            t["fired"] = True
                            if self._on_reject:
                                self._on_reject("deferred_lift", t["lane"], t["y"], now)
                        continue
                # The apex also re-detects as its own descending "gem", so a track whose
                # arrival falls at the lift moment (pending schedule or executed release)
                # was already served by the release -> consume.
                # Window is asymmetric: the arrow object's arrival lands within [-40,+20]ms of
                # the drain release, while a real next note after the tube arrives >=100ms
                # later — a symmetric window ate those next notes (run-14 unknown resets).
                sched = self._release_at.get(t["lane"])
                if sched is not None and -self._lift_window_s <= t["arrival"] - (sched + self._latency_s) <= self._lift_fwd_s:
                    t["fired"] = True
                    if self._on_reject:
                        self._on_reject("lift_consume", t["lane"], t["y"], now)
                    continue
                lift_t = self._lift_t.get(t["lane"])
                if lift_t is not None and -self._lift_window_s <= t["arrival"] - lift_t <= self._lift_fwd_s:
                    t["fired"] = True
                    self._lift_t.pop(t["lane"], None)
                    if self._on_reject:
                        self._on_reject("lift_consume", t["lane"], t["y"], now)
                    continue
                # Notes only ever DESCEND, and a real note is re-sighted every frame on its
                # way to the fret. A track without descent proof neither presses nor
                # pre-releases: a re-sighted non-descender is junk NOW (run-16 +78.29: a
                # blip drifting y118->116 re-strummed a held lane), and a single-sighting
                # track WAITS for proof and dies when its hit window passes (run-17's only
                # reset: a one-frame f:111 flash 186ms post-press re-strummed the held
                # chord onto nothing; no frame ever re-sighted it).
                resighted = t["seen"] > t.get("t0", t["seen"])
                if not (resighted and t["y"] > t.get("y0", t["y"])):
                    if resighted or now > t["arrival"] + self._latency_s:
                        t["fired"] = True
                        if self._on_reject:
                            self._on_reject("no_descent", t["lane"], t["y"], now)
                    continue
                # a next gem into a still-held lane must re-strum: lift the key first, early
                # enough that a key-up is actually sampled.
                if t["lane"] in self._held and not t.get("pre_released"):
                    self._end_hold(t["lane"], now)
                    t["pre_released"] = True
                    t["pre_t"] = now
                if t.get("pre_released"):
                    # guarantee the key sits UP for the full gap before re-pressing (even if the
                    # gem was detected late) so the game samples the lift and sees a new keydown.
                    if now >= t["pre_t"] + self._restrum_gap_s:
                        self._press(t["lane"])
                        t["fired"] = True
                        self._begin_hold(t["lane"], now)
                        if t.get("lift_at"):
                            self._release_at[t["lane"]] = t["lift_at"] - self._latency_s
                        fired = True
                elif now >= fire_at:
                    self._press(t["lane"])            # normal press (lane was not held)
                    t["fired"] = True
                    self._begin_hold(t["lane"], now)
                    if t.get("lift_at"):
                        self._release_at[t["lane"]] = t["lift_at"] - self._latency_s
                    fired = True
        return fired

    def service_holds(self, beam_heights, now):
        """Sustain/release held lanes and prune spent tracks (frame clock).

        Hold length is driven by the SUSTAIN BEAM height, not the fret strip (the hit-flash keeps
        the strip lit ~200ms after every hit -> over-held taps + mistimed sustain ends). BEAM
        HYSTERESIS: a note becomes a confirmed sustain when its beam reaches beam_confirm_px; once
        confirmed we keep holding while the beam stays above the lower beam_sustain_px, because a
        long sustain's tail SHRINKS as it passes and would otherwise read as "gone" ~100ms early.
        A note that never beams is a tap -> released after a short tap_hold. A hard cap force-
        releases any lane held >= max_hold_s: no real sustain approaches that length, and stage
        glow can otherwise pin the beam signal on and absorb every note in the lane (this happened:
        16.8s stuck holds). Beam readings within flash_settle_s of the press are ignored: the
        hit-flash bloom reads as an 80px+ column and would confirm every hard tap as a "sustain"
        (tap_hold must exceed the settle so a real sustain survives to its first eligible trail
        reading). `beam_heights` maps lane -> tallest bright-column height (px)."""
        with self._lock:
            for lane in list(self._held):
                held_since = now - self._held[lane]
                if held_since >= self._max_hold_s:
                    self._end_hold(lane, now)
                    continue
                if lane in self._release_at:
                    continue                          # a scheduled lift owns this hold's release
                raw = beam_heights.get(lane, 0)
                if raw > 0:
                    self._beam_last[lane] = (raw, now)  # freshest reading: the defer gate needs
                    # the CURRENT remaining trail, not the max (readings flicker to 0 mid-hold)
                    hist = [e for e in self._beam_hist.get(lane, []) if now - e[1] <= 0.3]
                    hist.append((raw, now))
                    self._beam_hist[lane] = hist[-20:]
                if raw > self._hold_beam_px.get(lane, 0):
                    self._hold_beam_px[lane] = raw    # trail-span memory for observe()'s rejection
                # A real sustain's trail reads certain-height AT the press; a fast stream's next
                # gem stacking over this hold's fret glow only fakes one ~100ms+ in. Only EARLY
                # certain readings arm the trail rules, else the spoof consumes the very gem
                # that caused it.
                if raw >= self._trail_certain_px and held_since <= self._trail_early_s:
                    self._trail_armed.add(lane)
                h = raw if held_since >= self._flash_settle_s else 0
                if h >= self._beam_confirm_px:
                    self._beam_confirmed.add(lane)
                    self._beam_anchor[lane] = (h, now)
                elif lane in self._beam_confirmed and h >= self._beam_sustain_px:
                    self._beam_anchor[lane] = (h, now)  # shrinking tail keeps anchoring
                if lane in self._beam_confirmed:
                    # The trail's height IS its remaining duration (h px draining at scroll
                    # speed) — the same invariant as the deferred-lift gate. Grace-after-
                    # stale abandoned the last ~50-60ms of every tail and let edge-lane
                    # beam flicker (weak ~70px d-lane readings, run 17, user-confirmed
                    # early releases) cut holds short; holding to the predicted tail end
                    # rides out dropouts up to the tail's own remaining time and centers
                    # arrow drain-lifts on the arrow's true arrival. Sub-sustain readings
                    # never anchor, so persistent dim glow cannot pin a hold (the cap
                    # still backstops a stuck anchor loop).
                    anchor = self._beam_anchor.get(lane)
                    if anchor is not None and now < anchor[1] + anchor[0] / self._px_per_s:
                        continue
                # A CONFIRMED sustain releases on beam timing regardless of the tap-hold floor:
                # arrow-hold tubes run ~150-250ms and their lift demands the key UP at the
                # drain; the floor exists only to bridge the bloom for unconfirmed holds.
                if held_since < self._tap_hold_s and lane not in self._beam_confirmed:
                    continue                          # tap still within its short min-hold
                # The drain of a confirmed tube IS the lift moment (chevrons repeat along long
                # tubes, so no single sighting can schedule it): record it so the arrow object
                # arriving alongside is consumed by the lift window instead of overstrumming.
                if lane in self._beam_confirmed:
                    self._lift_t[lane] = now
                    self._lift_events.append((lane, now, "drain"))
                self._end_hold(lane, now)
            self._tracks = [t for t in self._tracks if not (t["fired"] and now - t["seen"] > self._prune_s)]

    def update(self, beam_lanes, now):
        self.fire_due(now)
        self.service_holds(beam_lanes, now)

    def held_lanes(self):
        with self._lock:
            return frozenset(self._held)

    def drain_lifts(self):
        with self._lock:
            out, self._lift_events = self._lift_events, []
            return out

    def track_count(self):
        with self._lock:
            return len(self._tracks)

    def release_all(self):
        with self._lock:
            for lane in list(self._held):
                self._release(lane)
            self._held.clear()
            self._beam_anchor.clear()
            self._beam_confirmed.clear()
            self._hold_beam_px.clear()
            self._beam_last.clear()
            self._beam_hist.clear()
            self._trail_armed.clear()
            self._lift_t.clear()
            self._release_at.clear()
            self._provisional.clear()
            self._tracks.clear()
            self._last_press.clear()


class NoteEventQueue:
    """Tracks notes descending the highway as a queue of predicted press events.

    Each frame contributes detections (lane, y) — every visible note, at any height. From a
    note's position and the constant scroll speed we predict when it reaches the fret and
    enqueue a press for that time. Repeat sightings of the same note across frames are merged
    (data association by lane + arrival window) and the arrival is refined to the latest,
    closest sighting — "future frames correct pending commands". So one frame can enqueue many
    notes at once (the frame rate doesn't cap the note rate), chords are independent entries,
    and a note missed at one height is caught at another.

    TWO CLOCKS, decoupled. Detection is slow (~50fps) and only ADDS/REFINES events (`observe`)
    and services holds (`service_holds`). Firing the presses (`fire_due`) is timing-critical and
    runs on a separate FAST clock (~1ms) so a press lands at its exact predicted moment instead
    of being quantised to — and bunched at — the next detection frame. Both clocks touch the same
    `_pending`/`_held`, so every mutation is under `_lock`.
    """

    def __init__(self, press_fn, release_fn, fret_y, px_per_s, latency_s, release_grace_s, match_tol_s,
                 prune_s=0.6, min_regap_s=0.08):
        self._press = press_fn
        self._release = release_fn
        self._fret_y = fret_y
        self._px_per_s = px_per_s
        self._latency_s = latency_s
        self._release_grace_s = release_grace_s
        self._match_tol_s = match_tol_s
        self._prune_s = prune_s
        self._min_regap_s = min_regap_s
        self._pending = []   # {lane, arrival, last_seen, fired}
        self._held = {}      # lane -> last_fret_time
        self._lock = threading.Lock()

    def observe(self, detections, now):
        """Add/refine predicted press events from this frame's note sightings (frame clock).

        Repeated notes in one lane (e.g. L L L) each get their own event, even while that lane
        is still held — otherwise the 2nd/3rd note is silently absorbed into the ongoing hold
        ("press L, nothing, press L"). A note detected higher up the highway predicts a later
        arrival; a re-sighting of the note we're already holding predicts arrival ~= now, so a
        held lane only accepts a NEW event whose arrival is at least `min_regap_s` out — that
        rejects the current note's own residue while still catching the genuine next note."""
        with self._lock:
            for lane, y in detections:
                arrival = now + max(0.0, self._fret_y - y) / self._px_per_s
                if lane in self._held and (arrival - now) < self._min_regap_s:
                    continue   # too close to the note being held -> same note, not a new strum
                match = None
                for e in self._pending:
                    if e["lane"] == lane and not e["fired"] and abs(e["arrival"] - arrival) <= self._match_tol_s:
                        match = e
                        break
                if match is not None:
                    match["arrival"] = arrival   # latest sighting is closest to the fret -> most accurate
                    match["last_seen"] = now
                else:
                    self._pending.append({"lane": lane, "arrival": arrival, "last_seen": now, "fired": False})

    def fire_due(self, now):
        """Fire every pending press whose predicted arrival has come (FAST clock).

        Timing-critical: drive this from a dedicated sub-millisecond loop, NOT the detection
        frame loop. On a fast clock, dense runs of notes each fire at their own exact predicted
        time; on the frame clock they get bunched at frame boundaries (the miss source in dense
        passages). Returns True if it pressed anything (handy for logging)."""
        fired = False
        with self._lock:
            for e in self._pending:
                if not e["fired"] and now >= e["arrival"] - self._latency_s:
                    if e["lane"] in self._held:
                        # lane is still down from the previous note -> RE-STRUM: release first so
                        # this press is a fresh keydown edge the game will count as a new note.
                        self._release(e["lane"])
                    self._press(e["lane"])
                    e["fired"] = True
                    self._held[e["lane"]] = now
                    fired = True
        return fired

    def service_holds(self, fret_present, now):
        """Sustain/release held lanes and prune spent events (frame clock).

        Not timing-critical: a hold lasts far longer than a frame interval and release_grace
        absorbs frame jitter, so this stays on the detection clock which is where fret presence
        is actually observed."""
        with self._lock:
            for lane in list(self._held):
                if lane in fret_present:
                    self._held[lane] = now
                elif now - self._held[lane] >= self._release_grace_s:
                    self._release(lane)
                    del self._held[lane]
            self._pending = [e for e in self._pending if not (e["fired"] and now - e["last_seen"] > self._prune_s)]

    def update(self, fret_present, now):
        """Single-clock path: fire then service, both at `now`. Used by unit tests; the live
        bot instead runs fire_due on a fast clock and service_holds on the frame clock."""
        self.fire_due(now)
        self.service_holds(fret_present, now)

    def held_lanes(self):
        with self._lock:
            return frozenset(self._held)

    def pending_count(self):
        with self._lock:
            return sum(1 for e in self._pending if not e["fired"])

    def release_all(self):
        with self._lock:
            for lane in list(self._held):
                self._release(lane)
            self._held.clear()
            self._pending.clear()
