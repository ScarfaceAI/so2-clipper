"""Name-free visual role classification for red SO2 kill-feed rows.

Input images use OpenCV's BGR convention.  Coordinates returned by
``highlight_rects`` and ``analyze`` are relative to the supplied ROI.
"""

from __future__ import annotations

import cv2
import numpy as np


def _red_mask(roi: np.ndarray) -> np.ndarray:
    b, g, r = cv2.split(roi)
    r16, g16, b16 = r.astype(np.int16), g.astype(np.int16), b.astype(np.int16)
    # Warm stone and blood-red world textures can pass a ratio-only threshold.
    # Keep saturated red frame pixels, while ignoring brown surfaces with weak
    # absolute red-vs-green separation.
    red = (r16 >= 115) & (r16 - g16 >= 62) & (r16 - b16 >= 55)
    return red.astype(np.uint8) * 255


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def highlight_rects(roi):
    """Find red outlined feed rows and return unique (x, y, w, h) boxes."""
    if roi is None or roi.ndim != 3 or roi.shape[2] < 3 or not roi.size:
        return []
    height, width = roi.shape[:2]
    mask = _red_mask(roi)
    # Bridge antialiasing gaps without joining adjacent rows over their full width.
    kx = max(3, min(9, round(width * .003)))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kx, 3)))
    # RETR_LIST preserves the inner outline when unrelated red UI elements touch
    # the outer frame in a busy full-screen crop.
    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    min_h, max_h = max(14, round(height * .025)), min(100, height)
    min_w = max(36, round(width * .22))
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < min_w or not min_h <= h <= max_h or w / max(1, h) < 3.0:
            continue
        patch = mask[y:y + h, x:x + w]
        # A rectangle needs long red runs along both horizontal edges. Check a
        # few nearby scanlines because anti-aliasing often makes the exact edge thin.
        edge = max(2, round(h * .14))
        top = patch[:edge].mean(axis=1).max(initial=0) / 255
        bottom = patch[-edge:].mean(axis=1).max(initial=0) / 255
        if min(top, bottom) < .40:
            continue
        # Reject long red text strips: each vertical side should also have a run.
        side = max(2, round(w * .035))
        left = patch[:, :side].mean(axis=0).max(initial=0) / 255
        right = patch[:, -side:].mean(axis=0).max(initial=0) / 255
        clipped_left = x <= 2
        clipped_right = x + w >= width - 2
        if (left < .20 and not clipped_left) or (right < .20 and not clipped_right):
            continue
        candidates.append((int(x), int(y), int(w), int(h)))
    candidates.sort(key=lambda r: (r[1], r[0], -(r[2] * r[3])))
    unique = []
    for rect in candidates:
        if any(_iou(rect, old) > .55 for old in unique):
            continue
        unique.append(rect)
    return unique


def _color_masks(inner):
    b, g, r = [c.astype(np.int16) for c in cv2.split(inner)]
    hi = np.maximum(np.maximum(r, g), b)
    lo = np.minimum(np.minimum(r, g), b)
    spread = hi - lo
    white = (lo >= 142) & (spread <= 70)
    # Gold and blue are the two common opponent name colors. Keep the thresholds
    # broad enough for antialiasing while excluding the near-white kill icons.
    gold = (r >= 115) & (g >= 78) & (r - b >= 42) & (g - b >= 24) & (r >= g * .96)
    blue = (b >= 105) & (b - r >= 22) & (b >= g * .88)
    gold &= ~white
    blue &= ~white
    return {
        "white": white.astype(np.uint8),
        "gold": gold.astype(np.uint8),
        "blue": blue.astype(np.uint8),
    }


def _components(mask, row_h):
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    items = []
    min_h = max(3, round(row_h * .10))
    max_h = round(row_h * .98)
    for x, y, w, h, area in stats[1:]:
        if h < min_h or h > max_h or w < 2 or area < max(5, row_h * .045):
            continue
        items.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h), "area": int(area)})
    return items


def _groups(mask, row_h, max_gap_scale):
    """Join neighboring glyph components into horizontal visual runs."""
    comps = _components(mask, row_h)
    comps.sort(key=lambda c: (c["x"], c["y"]))
    groups = []
    # Team-color glyphs in the native HUD are spaced more widely than the
    # enlarged detail crops. White names retain tighter grouping so a '+' stays
    # an independent shape cue instead of merging into the name.
    max_gap = max(3, round(row_h * max_gap_scale))
    for comp in comps:
        choices = []
        for i, group in enumerate(groups):
            gx1 = group["x"]
            gx2 = gx1 + group["w"]
            gap = comp["x"] - gx2
            overlap = min(group["y"] + group["h"], comp["y"] + comp["h"]) - max(group["y"], comp["y"])
            if -2 <= gap <= max_gap and overlap >= -round(row_h * .12):
                choices.append((max(0, gap), i))
        if not choices:
            groups.append(dict(comp, n=1, comps=[comp]))
        else:
            _, i = min(choices)
            g = groups[i]
            x1, y1 = min(g["x"], comp["x"]), min(g["y"], comp["y"])
            x2, y2 = max(g["x"] + g["w"], comp["x"] + comp["w"]), max(g["y"] + g["h"], comp["y"] + comp["h"])
            g.update(x=x1, y=y1, w=x2 - x1, h=y2 - y1,
                     area=g["area"] + comp["area"], n=g["n"] + 1)
            g["comps"].append(comp)
    groups.sort(key=lambda g: g["x"])
    return groups


def _text_like(group, row_h):
    # A name is a sequence of glyph-sized shapes. Long, solid horizontal weapon
    # silhouettes and compact round icons fail these shape constraints.
    if group["n"] < 2 or group["h"] < row_h * .20 or group["h"] > row_h * .80:
        return False
    if group["w"] / max(1, group["h"]) > 8.0:
        return False
    tall = sum(1 for c in group["comps"] if .20 * row_h <= c["h"] <= .72 * row_h
               and c["w"] <= max(1.5 * c["h"], row_h * .62))
    return tall >= 2


def _segments(mask, row_h, max_gap_scale):
    groups = _groups(mask, row_h, max_gap_scale)
    result = []
    for group in groups:
        result.append({
            "x": group["x"], "y": group["y"], "w": group["w"], "h": group["h"],
            "components": group["n"], "text_like": _text_like(group, row_h),
            "aspect": round(group["w"] / max(1, group["h"]), 3),
        })
    return result


def _signature(image):
    """128-bit dHash of normalized HUD foreground occupancy, excluding scenery."""
    b, g, r = [c.astype(np.int16) for c in cv2.split(image)]
    hi = np.maximum(np.maximum(r, g), b)
    lo = np.minimum(np.minimum(r, g), b)
    spread = hi - lo
    white = (lo >= 118) & (spread <= 88)
    gold = (r >= 92) & (g >= 58) & (r - b >= 32) & (g - b >= 18) & (r >= g * .91)
    blue = (b >= 92) & (b - r >= 18) & (b >= g * .80)
    foreground = (white | gold | blue).astype(np.uint8)
    # Dilation bridges antialias/fade changes between adjacent decoded frames.
    # Cropping to foreground bounds then removes the changing world/background
    # around a translucent feed row before normalization.
    binary = cv2.dilate(foreground, np.ones((5, 5), np.uint8), iterations=1)
    points = cv2.findNonZero(binary)
    if points is not None:
        x, y, w, h = cv2.boundingRect(points)
        px, py = max(2, round(w * .006)), max(2, round(h * .08))
        x1, y1 = max(0, x - px), max(0, y - py)
        x2, y2 = min(binary.shape[1], x + w + px), min(binary.shape[0], y + h + py)
        binary = binary[y1:y2, x1:x2]
    small = cv2.resize(binary, (17, 8), interpolation=cv2.INTER_AREA)
    bits = small[:, 1:] > .12
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bit)
    return f"dhash128:{value:032x}"


def _classify(features):
    h, w = features["inner_size"]
    white = [s for s in features["segments"]["white"] if s["text_like"]]
    colored = [s for color in ("gold", "blue") for s in features["segments"][color]
               if s["text_like"]]
    white.sort(key=lambda s: s["x"])
    colored.sort(key=lambda s: s["x"])
    plus_candidates = [s for color in ("white", "gold", "blue")
                       for s in features["segments"][color]
                       if not s["text_like"] and .18 * h <= s["h"] <= .52 * h
                       and .15 <= s["w"] / max(1, s["h"]) <= 1.25]
    evidence = {
        "white_text_runs": len(white), "colored_text_runs": len(colored),
        "weapon_divider": False,
        "plus_shaped_marks": len(plus_candidates),
        "white_text_x": [s["x"] for s in white],
        "colored_text_x": [s["x"] for s in colored],
    }
    # Distinguish the icon divider from name glyphs. A gun is a long non-text
    # white run near the middle; compact bomb / medal-like icons may divide self
    # death rows, so retain those separately.
    dividers = [s for s in features["segments"]["white"]
                if .22 * w < s["x"] < .78 * w and s["w"] >= .075 * w
                and s["h"] >= .22 * h]
    weapon_candidates = [s for s in dividers if not s["text_like"]
                         and s["w"] / max(1, s["h"]) >= 1.15]
    # A few dual-weapon icons split into multiple glyph-sized connected pieces.
    # Accept a text-like center run as a weapon only when it is clearly between
    # a left own-name and a right opponent name.
    if not weapon_candidates:
        left_names = [s for s in white if s["x"] + s["w"] < .42 * w]
        right_names = [s for s in colored if s["x"] > .58 * w]
        if left_names and right_names:
            weapon_candidates = [s for s in dividers if s["text_like"]
                                 and s["x"] > max(n["x"] + n["w"] for n in left_names)
                                 and s["x"] + s["w"] < min(n["x"] for n in right_names)]
    weapon = min(weapon_candidates, key=lambda s: abs((s["x"] + s["w"] / 2) - .5 * w)) if weapon_candidates else None
    evidence["weapon_divider"] = weapon is not None
    evidence["divider_x"] = int(weapon["x"]) if weapon else None
    evidence["central_icon_runs"] = len(dividers)

    # A self / bomb death has two white name runs with a colored secondary
    # name between them and a compact white icon near the center.
    left_owners = [s for s in white if s["x"] + s["w"] < .42 * w]
    right_owners = [s for s in white if s["x"] > .58 * w]
    middle_colored = [s for s in colored if left_owners and right_owners
                      and s["x"] > max(left_owners, key=lambda q: q["x"])["x"]
                      and s["x"] + s["w"] < min(right_owners, key=lambda q: q["x"])["x"]]
    if left_owners and right_owners and middle_colored and dividers:
        return "self_death", "white_names_both_sides_with_middle_secondary_name_and_icon", evidence

    # The plus can be white even when the primary attacker is gold. Its shape and
    # position between a colored primary name and a white own-name are useful
    # evidence; a flash icon before a name cannot satisfy this ordering.
    if weapon:
        own_before_weapon = [s for s in white if s["text_like"] and s["x"] + s["w"] < weapon["x"]]
        colored_before_weapon = [s for s in colored if s["x"] + s["w"] < weapon["x"]]
        own_after_weapon = [s for s in white if s["text_like"] and s["x"] > weapon["x"] + weapon["w"]]
        colored_after_weapon = [s for s in colored if s["x"] > weapon["x"] + weapon["w"]]
        if own_before_weapon and colored_after_weapon:
            first_own = min(own_before_weapon, key=lambda s: s["x"])
            first_color = min(colored_before_weapon, key=lambda s: s["x"]) if colored_before_weapon else None
            # A kill may show a teammate after the local killer as an assister
            # (white own-name + colored helper). Assist rows have the inverse
            # order: colored primary killer + white local assister.
            if first_color is None or first_own["x"] < first_color["x"]:
                return "kill", "white_primary_before_weapon_and_opponent_after", evidence
        if colored_before_weapon and own_before_weapon:
            owner = max(own_before_weapon, key=lambda s: s["x"] + s["w"])
            attacker = max(colored_before_weapon, key=lambda s: s["x"] + s["w"])
            gap = owner["x"] - (attacker["x"] + attacker["w"])
            plus_between = any(attacker["x"] + attacker["w"] - 2 <= mark["x"]
                               and mark["x"] + mark["w"] <= owner["x"] + 2
                               for mark in plus_candidates)
            if gap >= -2 and gap <= .30 * w and plus_between and colored_after_weapon:
                return "assist", "colored_primary_plus_white_name_before_weapon", evidence
        if colored_before_weapon and own_after_weapon and not own_before_weapon:
            return "death", "opponent_before_weapon_and_white_name_after", evidence
    return "unknown", "role_layout_not_clear", evidence


def analyze(roi):
    """Return one role record per detected red feed row, without OCR names."""
    if roi is None or roi.ndim != 3 or roi.shape[2] < 3 or not roi.size:
        return []
    rows = []
    for rect in highlight_rects(roi):
        x, y, w, h = rect
        box = roi[y:y + h, x:x + w]
        pad_x = max(2, round(w * .018))
        pad_y = max(3, round(h * .12))
        inner = box[pad_y:h - pad_y, pad_x:w - pad_x]
        masks = _color_masks(inner)
        segments = {name: _segments(mask, inner.shape[0], .14 if name == "white" else .20)
                    for name, mask in masks.items()}
        features = {"inner_size": [int(inner.shape[0]), int(inner.shape[1])],
                    "segments": segments}
        role, reason, evidence = _classify(features)
        rows.append({
            "rect": [int(x), int(y), int(w), int(h)],
            "rect_size": [int(w), int(h)],
            "role": role,
            "visual_signature": _signature(inner),
            "evidence": {"reason": reason, **evidence, "segments": segments},
        })
    return rows
