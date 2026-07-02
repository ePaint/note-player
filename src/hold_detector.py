def lane_fill_ratios(frame, num_lanes, section_size, brightness_threshold,
                     min_lit_fraction_per_row, crop_top=0.0, crop_bottom=1.0, horizontal_inset=0.0):
    """Per-lane vertical fill ratio of bright pixels within a cropped lane sub-region.

    The capture band is a rectangle over a perspective trapezoid, so the outer lanes
    pick up the bright highway side-rails and the static corner buttons. Cropping the
    top/bottom and insetting the lane horizontally restricts sampling to the central
    region where a held-note beam lives; a high brightness threshold drops the grey
    rails while keeping the near-white beam. A sustain yields a high ratio, a tap or an
    idle lane a low one.
    """
    height = frame.shape[0]
    row_start = int(height * crop_top)
    row_end = int(height * crop_bottom)
    band_height = row_end - row_start
    margin = int(section_size * horizontal_inset)
    ratios = []
    for i in range(num_lanes):
        x0 = i * section_size + margin
        x1 = i * section_size + section_size - margin
        column = frame[row_start:row_end, x0:x1]
        lit_fraction_per_row = (column >= brightness_threshold).mean(axis=1)
        lit_rows = int((lit_fraction_per_row >= min_lit_fraction_per_row).sum())
        ratios.append(lit_rows / band_height)
    return ratios


def detect_note_bars(frame, num_lanes, section_size, band_top, band_bottom,
                     brightness_threshold, min_row_fill, horizontal_inset=0.0, min_run=2):
    """Find each note head's (lane, y_center) across the highway band.

    A note head is a horizontal bright bar -> a run of rows whose per-row bright fraction
    clears `min_row_fill`. Hold beams are vertical (thin per row) so they don't register as
    bars. Returns one (lane, y) per detected bar so the queue can predict each note's arrival.
    """
    margin = int(section_size * horizontal_inset)
    out = []
    for i in range(num_lanes):
        col = frame[band_top:band_bottom, i * section_size + margin:(i + 1) * section_size - margin]
        if col.size == 0:
            continue
        bright = (col >= brightness_threshold).mean(axis=1) >= min_row_fill
        run_start = None
        for y in range(len(bright)):
            if bright[y] and run_start is None:
                run_start = y
            elif not bright[y] and run_start is not None:
                if y - run_start >= min_run:
                    out.append((i, band_top + (run_start + y) // 2))
                run_start = None
        if run_start is not None and len(bright) - run_start >= min_run:
            out.append((i, band_top + (run_start + len(bright)) // 2))
    return out


def detect_gems(frame, num_lanes, section_size, band_top, band_bottom,
                brightness_threshold, min_row_fill, horizontal_inset=0.0,
                min_height=4, merge_gap=14, min_mean_brightness=195, max_height=52,
                merge_center_px=0, hollow_fill=0.12, return_arrow=False, px_scale=1.0):
    """Find each SOLID note-gem head's (lane, y_center) — a cleaner detector than
    detect_note_bars for dense same-lane streams.

    A real gem is a near-white solid block spanning most of the lane. Two failure modes of the
    raw row-fill approach are handled: (1) a single gem whose middle dims gets split into two
    runs -> we MERGE runs separated by < merge_gap px back into one gem; (2) translucent hold-
    body outlines and hit-burst particles clear the fill threshold but are DIM or thin -> we
    require the lit pixels to be near-white (mean >= min_mean_brightness) and the merged run to
    be >= min_height px. A run TALLER than max_height is a sustain BEAM (a continuous bright
    column), not a tap gem, so it is rejected — otherwise it would spawn a phantom press mid-lane
    and re-strum (break) the very sustain being held. Result: one detection per real gem.

    merge_center_px (0 = off): gems also render as bright top+bottom edges with a fully DARK
    middle (gap brightness can't discriminate; measured median 42), ~43px apart center-to-center,
    while real same-lane notes are never closer than ~75px centers at this scroll speed — so runs
    whose centers are closer than this are ONE gem regardless of the gap's darkness.

    hollow_fill: the arrow-hold variant renders a solid chevron APEX ~35-40px above the head,
    connected by the hollow tube's bright rails. Reporting such a cluster's span center dragged
    the press 25-250ms late (every arrow hold missed). The rows BETWEEN a fragment pair are dark
    (fill ~0) while the rows inside an arrow structure carry the rails (fill ~0.2), so a merged
    cluster whose inter-run rows average >= hollow_fill reports its BOTTOM sub-run = the head.

    return_arrow: emit (lane, y, arrow_y) where arrow_y is the hollow cluster's TOP sub-run
    center (the arrow apex; None for ordinary gems). The arrow is a LIFT marker — the key must
    come UP when it reaches the fret — and its pre-press geometry is what the queue schedules
    the release from.

    px_scale rescales the hard-coded arrow-geometry minima (apex/gap/rail spans, measured
    at 1440p) for other capture resolutions; passed-in px params arrive pre-scaled.
    """
    import numpy as np
    margin = int(section_size * horizontal_inset)
    out = []
    for i in range(num_lanes):
        col = frame[band_top:band_bottom, i * section_size + margin:(i + 1) * section_size - margin]
        if col.size == 0:
            continue
        lit = col >= brightness_threshold
        row_fill = lit.mean(axis=1)
        lit_counts = lit.sum(axis=1)
        lit_sums = (col * lit).sum(axis=1)
        row_mean = np.where(lit_counts > 0, lit_sums / np.maximum(lit_counts, 1), 0)
        solid = (row_fill >= min_row_fill) & (row_mean >= min_mean_brightness)
        runs = []
        start = None
        for y in range(len(solid)):
            if solid[y] and start is None:
                start = y
            elif not solid[y] and start is not None:
                runs.append([start, y])
                start = None
        if start is not None:
            runs.append([start, len(solid)])
        clusters = []
        for r in runs:
            if clusters:
                ext_a, ext_b = clusters[-1][0][0], clusters[-1][-1][1]
                close_edges = r[0] - ext_b < merge_gap
                close_centers = (merge_center_px
                                 and (r[0] + r[1]) / 2 - (ext_a + ext_b) / 2 < merge_center_px)
                if close_edges or close_centers:
                    clusters[-1].append(r)   # fragments/structure of one note -> one cluster
                    continue
            clusters.append([r])
        for c in clusters:
            a, b = c[0][0], c[-1][1]
            arrow = None
            if len(c) > 1:
                gap_fill = [row_fill[y] for r1, r2 in zip(c, c[1:]) for y in range(r1[1], r2[0])]
                if gap_fill and sum(gap_fill) / len(gap_fill) >= hollow_fill:
                    a, b = c[-1]             # hollow structure -> the bottom sub-run is the head
                    # Tag as an arrow hold only when the top sub-run is a real chevron APEX:
                    # >=3px tall and near-white (measured 230-256 peak vs 195-210 for the
                    # 1px glow-halo slivers a thin sustain's head casts above itself), and
                    # the gap's MIDDLE rows span rail-to-rail (~100px; a thin line spans
                    # ~15-35px; rows near the sub-runs carry the lane-wide halo, skip them).
                    # False tags scheduled lifts that broke real thin holds.
                    _s = lambda v: max(1, int(v * px_scale + 0.5))
                    apex_h = c[0][1] - c[0][0]
                    apex_peak = row_mean[c[0][0]:c[0][1]].max()
                    g0, g1 = c[0][1] + _s(6), c[-1][0] - _s(6)
                    if apex_h >= _s(3) and apex_peak >= 220 and g1 - g0 >= _s(8):
                        cols = np.flatnonzero(lit[g0:g1].mean(axis=0) >= 0.5)
                        if len(cols) and cols[-1] - cols[0] >= _s(55):
                            arrow = band_top + (c[0][0] + c[0][1]) // 2
            if min_height <= b - a <= max_height:
                gem = (i, band_top + (a + b) // 2)
                out.append(gem + (arrow,) if return_arrow else gem)
    return out


def _tall_group_height(best, min_width, max_width, group_min_px):
    """Max column-run height among contiguous groups of tall columns whose width fits the window.

    Measured live: a real trail is a solid 17-34px-wide group of tall columns; hit-flash particle
    streaks are <10px wide; the hit-flash bloom spans 56-118px (most of the lane). Only a group
    whose width falls inside [min_width, max_width] can be a trail. Heights below group_min_px
    never form a group and report 0 (the hold controller ignores anything below its sustain
    threshold anyway)."""
    height = 0
    w, gmax = 0, 0
    for h in list(best) + [0]:                        # sentinel closes the last group
        if h >= group_min_px:
            w += 1
            gmax = max(gmax, int(h))
            continue
        if w >= min_width and (max_width is None or w <= max_width):
            height = max(height, gmax)
        w, gmax = 0, 0
    return height


def detect_beam_heights(frame, lane_keys, section_size, band_top, band_bottom,
                        brightness_threshold, horizontal_inset=0.0,
                        min_width=1, max_width=None, group_min_px=45, anchor_px=8):
    """Per lane, the height (px) of the tallest bright vertical column group = SUSTAIN BEAM length.

    A held sustain renders its tail as a bright vertical column spanning much of the highway; a
    struck TAP only makes a short hit-flash bloom at the fret. So a tall column => a sustain. The
    height matters for the END of a long sustain: as its tail passes, the visible beam SHRINKS, so
    the controller confirms a sustain at a high threshold but keeps holding the shrinking tail down
    to a low one (hysteresis) instead of letting go ~100ms early. The width window (min_width /
    max_width, see _tall_group_height) rejects flash particle streaks and the flash bloom, which
    otherwise read as beams. A held trail is ANCHORED at the fret, while an incoming sustain's
    tube (pre-hit) floats above it — columns not lit within anchor_px of the band bottom don't
    count (anchor_px=None disables).
    """
    import numpy as np
    margin = int(section_size * horizontal_inset)
    heights = {}
    for i, key in enumerate(lane_keys):
        col = frame[band_top:band_bottom, i * section_size + margin:(i + 1) * section_size - margin]
        if col.ndim == 3:
            col = col[..., 0]     # live GRAY capture carries a (H, W, 1) channel dim
        if col.size == 0:
            heights[key] = 0
            continue
        lit = (col >= brightness_threshold)
        run = np.zeros(lit.shape[1], dtype=np.int32)
        best = np.zeros(lit.shape[1], dtype=np.int32)
        for r in range(lit.shape[0]):                 # longest consecutive lit run per column
            run = (run + 1) * lit[r]
            best = np.maximum(best, run)
        if anchor_px is not None:
            best = best * lit[-anchor_px:].any(axis=0)
        heights[key] = _tall_group_height(best, min_width, max_width, group_min_px)
    return heights


def detect_beam_lanes(frame, lane_keys, section_size, band_top, band_bottom,
                      brightness_threshold, horizontal_inset=0.0, min_beam_height=80):
    """Set of lanes whose beam is >= min_beam_height tall (see detect_beam_heights)."""
    heights = detect_beam_heights(frame, lane_keys, section_size, band_top, band_bottom,
                                  brightness_threshold, horizontal_inset)
    return {k for k, h in heights.items() if h >= min_beam_height}


def lane_strip_fill(frame, num_lanes, section_size, strip_top, strip_bottom,
                    brightness_threshold, horizontal_inset=0.0):
    """Universal per-lane note signal: the fraction of a thin strip just above the fret
    that is bright. Type-agnostic (any note appearance, not one template shape) and it
    stays lit while a sustain's trail passes, so the same signal drives taps AND holds.
    Returns a fraction per lane; apply hysteresis on top to reject threshold-edge flicker.
    """
    margin = int(section_size * horizontal_inset)
    fills = []
    for i in range(num_lanes):
        strip = frame[strip_top:strip_bottom, i * section_size + margin:(i + 1) * section_size - margin]
        fills.append(float((strip >= brightness_threshold).mean()) if strip.size else 0.0)
    return fills


def lane_trail_scores(frame, num_lanes, section_size, brightness_threshold,
                      crop_top=0.0, crop_bottom=1.0, horizontal_inset=0.0):
    """Per-lane score for a thin vertical sustain trail.

    A held sustain renders as a near-full-height bright column only a few pixels wide,
    so per-row width-fill (lane_fill_ratios) misses it. Instead, for each column take the
    fraction of rows that are lit and return the max over columns: a solid trail column
    scores ~1.0, an empty lane scores near 0 (edge artifacts stay well below a real trail).
    """
    height = frame.shape[0]
    row_start = int(height * crop_top)
    row_end = int(height * crop_bottom)
    margin = int(section_size * horizontal_inset)
    scores = []
    for i in range(num_lanes):
        x0 = i * section_size + margin
        x1 = i * section_size + section_size - margin
        column = frame[row_start:row_end, x0:x1]
        lit_fraction_per_column = (column >= brightness_threshold).mean(axis=0)
        scores.append(float(lit_fraction_per_column.max()) if lit_fraction_per_column.size else 0.0)
    return scores


def apply_hysteresis(ratios, lane_keys, engaged, engage_threshold, sustain_threshold):
    """Stabilise hold detection against threshold-edge flicker.

    A lane engages once its fill reaches `engage_threshold`, then stays engaged while
    fill stays above the lower `sustain_threshold`. Without this, a beam fluctuating
    around a single threshold makes the controller release/re-press mid-sustain, so the
    game sees taps instead of one continuous hold.
    """
    held = set()
    for i, key in enumerate(lane_keys):
        threshold = sustain_threshold if key in engaged else engage_threshold
        if ratios[i] >= threshold:
            held.add(key)
    return held


def active_hold_lanes(frame, lane_keys, section_size, brightness_threshold,
                      min_lit_fraction_per_row, fill_ratio_threshold,
                      crop_top=0.0, crop_bottom=1.0, horizontal_inset=0.0):
    ratios = lane_fill_ratios(frame, len(lane_keys), section_size, brightness_threshold,
                              min_lit_fraction_per_row, crop_top, crop_bottom, horizontal_inset)
    active = {lane_keys[i] for i, ratio in enumerate(ratios) if ratio >= fill_ratio_threshold}
    return active, ratios
