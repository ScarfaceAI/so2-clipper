"""Read the active row from the replay spectator roster HUD.

The white eye is a selection marker on the player's roster avatar.  The row's
right-hand numeral is the cumulative kill count.  Player names are deliberately
not OCR'd here: the eye selects the row independently of feed names.
"""
from functools import lru_cache
from pathlib import Path
import re

import cv2
import numpy as np


_REFERENCE_WIDTH = 2560
_REFERENCE_HEIGHT = 1440
_EYE_MIN_SCORE = 0.82


@lru_cache(maxsize=1)
def _eye_template():
    path = Path(__file__).parent / 'assets' / 'spectator-eye.png'
    data = np.fromfile(path, dtype=np.uint8)
    template = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    if template is None or template.size == 0:
        raise ValueError(f'观战眼睛模板不可读：{path}')
    return template


def _gray(image):
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _screen_to_image(rect, image, x, y):
    """Map full-screen coordinates to the supplied crop's pixel coordinates."""
    sx = rect[2] / image.shape[1]
    sy = rect[3] / image.shape[0]
    return round((x - rect[0]) / sx), round((y - rect[1]) / sy)


def _is_right(rect, screen_width):
    return rect[0] + rect[2] / 2 > screen_width / 2


def _bands(gray, screen_width, screen_height, rect):
    """Find the repeated dark roster cards that distinguish the spectator HUD."""
    image_scale_x = rect[2] / gray.shape[1]
    image_scale_y = rect[3] / gray.shape[0]
    ref_x_scale = screen_width / _REFERENCE_WIDTH
    ref_y_scale = screen_height / _REFERENCE_HEIGHT

    if _is_right(rect, screen_width):
        gx1, gx2 = round(.764 * screen_width), round(.936 * screen_width)
    else:
        gx1, gx2 = round(.061 * screen_width), round(.239 * screen_width)
    gy1, gy2 = round(.38 * screen_height), round(.92 * screen_height)
    x1, y1 = _screen_to_image(rect, gray, gx1, gy1)
    x2, y2 = _screen_to_image(rect, gray, gx2, gy2)
    x1, x2 = max(0, x1), min(gray.shape[1], x2)
    y1, y2 = max(0, y1), min(gray.shape[0], y2)
    if x2 - x1 < 80 or y2 - y1 < 80:
        return []

    darkness = (gray[y1:y2, x1:x2] < 100).mean(axis=1)
    smooth_size = max(3, round(7 * ref_y_scale / image_scale_y))
    smooth_size += (smooth_size + 1) % 2
    smooth = np.convolve(darkness, np.ones(smooth_size, np.float32) / smooth_size,
                         mode='same')
    active = smooth > .60

    runs = []
    start = None
    for index, value in enumerate(np.append(active, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append([y1 + start, y1 + index - 1])
            start = None

    merge_gap = max(1, round(12 * ref_y_scale / image_scale_y))
    merged = []
    for top, bottom in runs:
        if merged and top - merged[-1][1] - 1 <= merge_gap:
            merged[-1][1] = bottom
        else:
            merged.append([top, bottom])

    min_height = 80 * ref_y_scale / image_scale_y
    max_height = 170 * ref_y_scale / image_scale_y
    cards = [(top, bottom) for top, bottom in merged
             if min_height <= bottom - top + 1 <= max_height]
    if len(cards) < 3:
        return []

    # Three aligned roster rows are enough to identify the panel; large dark
    # scene overlays and one-off HUD bands do not pass this spacing check.
    spacing_min = 90 * ref_y_scale / image_scale_y
    spacing_max = 165 * ref_y_scale / image_scale_y
    aligned = []
    for card in cards:
        if not aligned or spacing_min <= card[0] - aligned[-1][0] <= spacing_max:
            aligned.append(card)
    if len(aligned) < 3:
        return []
    return [(round(rect[1] + top * image_scale_y),
             round(rect[1] + bottom * image_scale_y)) for top, bottom in aligned]


def _candidate_eyes(binary, screen_width, screen_height, rect, rows, template):
    image_scale_x = rect[2] / binary.shape[1]
    image_scale_y = rect[3] / binary.shape[0]
    ref_x_scale = screen_width / _REFERENCE_WIDTH
    ref_y_scale = screen_height / _REFERENCE_HEIGHT
    template_w = max(8, round(template.shape[1] * ref_x_scale / image_scale_x))
    template_h = max(8, round(template.shape[0] * ref_y_scale / image_scale_y))
    if (template_w, template_h) != (template.shape[1], template.shape[0]):
        eye = cv2.resize(template, (template_w, template_h), interpolation=cv2.INTER_NEAREST)
    else:
        eye = template

    if _is_right(rect, screen_width):
        gx1, gx2 = round(.925 * screen_width), round(.970 * screen_width)
    else:
        gx1, gx2 = round(.010 * screen_width), round(.041 * screen_width)
    candidates = []
    for row_index, (row_top, row_bottom) in enumerate(rows):
        gy1 = round(row_top + 15 * ref_y_scale)
        gy2 = round(row_top + 50 * ref_y_scale)
        x1, y1 = _screen_to_image(rect, binary, gx1, gy1)
        x2, y2 = _screen_to_image(rect, binary, gx2, gy2)
        x1, y1 = max(0, x1), max(0, y1)
        x2 = min(binary.shape[1] - eye.shape[1] + 1, x2)
        y2 = min(binary.shape[0] - eye.shape[0] + 1, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        search = binary[y1:y2 + eye.shape[0] - 1,
                        x1:x2 + eye.shape[1] - 1]
        if search.shape[0] < eye.shape[0] or search.shape[1] < eye.shape[1]:
            continue
        response = cv2.matchTemplate(search, eye, cv2.TM_CCORR_NORMED)
        _, score, _, location = cv2.minMaxLoc(response)
        if score >= _EYE_MIN_SCORE:
            local_x, local_y = x1 + location[0], y1 + location[1]
            candidates.append({
                'row_index': row_index,
                'score': float(score),
                'template_xy': [round(rect[0] + local_x * image_scale_x),
                                round(rect[1] + local_y * image_scale_y)],
                'template_size': [round(eye.shape[1] * image_scale_x),
                                  round(eye.shape[0] * image_scale_y)],
                'row': [row_top, row_bottom],
            })
    return candidates


def _global_eye_candidates(binary, screen_width, screen_height, rect, template):
    """Find a visible eye when changing scene brightness hides roster bands."""
    image_scale_x = rect[2] / binary.shape[1]
    image_scale_y = rect[3] / binary.shape[0]
    ref_x_scale = screen_width / _REFERENCE_WIDTH
    ref_y_scale = screen_height / _REFERENCE_HEIGHT
    template_w = max(8, round(template.shape[1] * ref_x_scale / image_scale_x))
    template_h = max(8, round(template.shape[0] * ref_y_scale / image_scale_y))
    eye = cv2.resize(template, (template_w, template_h), interpolation=cv2.INTER_NEAREST)
    if _is_right(rect, screen_width):
        gx1, gx2 = round(.925 * screen_width), round(.970 * screen_width)
    else:
        gx1, gx2 = round(.010 * screen_width), round(.041 * screen_width)
    gy1, gy2 = round(.38 * screen_height), round(.91 * screen_height)
    x1, y1 = _screen_to_image(rect, binary, gx1, gy1)
    x2, y2 = _screen_to_image(rect, binary, gx2, gy2)
    x1, y1 = max(0, x1), max(0, y1)
    x2 = min(binary.shape[1] - eye.shape[1] + 1, x2)
    y2 = min(binary.shape[0] - eye.shape[0] + 1, y2)
    if x2 <= x1 or y2 <= y1:
        return []
    search = binary[y1:y2 + eye.shape[0] - 1, x1:x2 + eye.shape[1] - 1]
    if search.shape[0] < eye.shape[0] or search.shape[1] < eye.shape[1]:
        return []
    response = cv2.matchTemplate(search, eye, cv2.TM_CCORR_NORMED)
    ys, xs = np.where(response >= _EYE_MIN_SCORE)
    peaks = sorted(((float(response[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
                   reverse=True)
    selected = []
    suppress_x = max(8, eye.shape[1] // 2)
    suppress_y = max(8, eye.shape[0] // 2)
    for score, mx, my in peaks:
        local_x, local_y = x1 + mx, y1 + my
        if any(abs(local_x - old['local_xy'][0]) < suppress_x
               and abs(local_y - old['local_xy'][1]) < suppress_y for old in selected):
            continue
        global_x = rect[0] + local_x * image_scale_x
        global_y = rect[1] + local_y * image_scale_y
        row_top = round(global_y + eye.shape[0] * image_scale_y / 2 - 56 * ref_y_scale)
        row_bottom = round(row_top + 116 * ref_y_scale)
        selected.append({
            'row_index': None,
            'score': score,
            'template_xy': [round(global_x), round(global_y)],
            'template_size': [round(eye.shape[1] * image_scale_x),
                              round(eye.shape[0] * image_scale_y)],
            'row': [row_top, row_bottom],
            'local_xy': [local_x, local_y],
        })
    for candidate in selected:
        candidate.pop('local_xy', None)
    return selected


def _crop_for_rect(image, rect, full_box):
    x1, y1, width, height = full_box
    x2, y2 = x1 + width, y1 + height
    ix1, iy1 = _screen_to_image(rect, image, x1, y1)
    ix2, iy2 = _screen_to_image(rect, image, x2, y2)
    ix1, iy1 = max(0, ix1), max(0, iy1)
    ix2, iy2 = min(image.shape[1], ix2), min(image.shape[0], iy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return None
    return image[iy1:iy2, ix1:ix2]


def _avatar(image, screen_width, screen_height, rect, candidate):
    ex, ey = candidate['template_xy']
    ew, eh = candidate['template_size']
    cx, cy = ex + ew / 2, ey + eh / 2
    box = [round(cx - .023 * screen_width, 2), round(cy - .042 * screen_height, 2),
           round(.047 * screen_width, 2), round(.084 * screen_height, 2)]
    patch = _crop_for_rect(image, rect, box)
    result = {'center': [round(cx / screen_width, 4), round(cy / screen_height, 4)],
              'box': box, 'score': round(candidate['score'], 3)}
    if patch is not None and patch.size:
        small = cv2.resize(patch, (32, 32), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        dct = cv2.dct(gray.astype(np.float32))[:8, :8]
        bits = dct > np.median(dct[1:])
        result['appearance'] = np.packbits(bits).tobytes().hex()
        result['color'] = small.mean(axis=(0, 1)).round().astype(int).tolist()
    return result


def _read_count(reader, image, screen_width, screen_height, rect, row_top):
    ref_x_scale = screen_width / _REFERENCE_WIDTH
    ref_y_scale = screen_height / _REFERENCE_HEIGHT
    if _is_right(rect, screen_width):
        # On this side the star counter precedes the number, so begin after
        # the icon to avoid EasyOCR reading its left point as a leading 1.
        counter_x, counter_width = .775, .013
    else:
        counter_x, counter_width = .226, .011
    box = [round(counter_x * screen_width), round(row_top + 2 * ref_y_scale),
           round(counter_width * screen_width), round(38 * ref_y_scale)]
    evidence = {'rect': box, 'readings': [], 'status': 'unreadable'}
    if reader is None:
        evidence['reason'] = 'reader_unavailable'
        return None, evidence
    crop = _crop_for_rect(image, rect, box)
    if crop is None or crop.size == 0:
        evidence['reason'] = 'counter_crop_outside_image'
        return None, evidence
    if crop.shape[0] < 24 or crop.shape[1] < 12:
        evidence['reason'] = 'counter_crop_too_small'
        return None, evidence
    enlarged = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    try:
        raw = reader.readtext(enlarged, detail=1, paragraph=False,
                              allowlist='0123456789', min_size=4)
    except Exception as exc:  # OCR failure must not turn an unread count into zero.
        evidence['reason'] = f'readtext_error:{type(exc).__name__}'
        return None, evidence

    parsed = []
    for item in raw or []:
        if not isinstance(item, (tuple, list)) or len(item) < 3:
            continue
        text = str(item[1]).strip()
        try:
            confidence = float(item[2])
        except (TypeError, ValueError):
            continue
        evidence['readings'].append({'text': text, 'confidence': round(confidence, 3)})
        if re.fullmatch(r'\d{1,2}', text) and confidence >= .40:
            parsed.append((int(text), confidence))
    if len(parsed) != 1:
        evidence['reason'] = 'no_single_confident_digit_read'
        return None, evidence
    evidence.update({'status': 'read', 'value': parsed[0][0], 'confidence': round(parsed[0][1], 3)})
    return parsed[0][0], evidence


def detect(image, screen_width, screen_height, rect, reader=None):
    """Return spectator identity signals, or ``None`` when no roster UI is present.

    ``rect`` is the full-screen pixel rectangle represented by ``image``.  A
    confirmed roster with a hidden or ambiguous eye marker returns spectator
    mode with ``avatar=None`` so callers do not mistake unrelated HUD icons for
    a first-person avatar.  A failed counter OCR read always remains ``None``.
    """
    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        return None
    if screen_width <= 0 or screen_height <= 0 or rect is None or len(rect) != 4:
        return None
    if rect[2] <= 0 or rect[3] <= 0:
        return None

    gray = _gray(image)
    rows = _bands(gray, screen_width, screen_height, rect)
    # Match the icon's white silhouette on a binary image so the underlying
    # player portrait can vary without changing the marker template.
    _, binary = cv2.threshold(gray, 215, 255, cv2.THRESH_BINARY)
    template = _eye_template()
    candidates = (_candidate_eyes(binary, screen_width, screen_height, rect, rows, template)
                  if rows else _global_eye_candidates(binary, screen_width, screen_height,
                                                      rect, template))
    if not rows and not candidates:
        return None
    evidence = {'roster_rows': [list(row) for row in rows],
                'eye_template': 'spectator-eye.png',
                'eye_candidates': [{'row': c['row'], 'score': round(c['score'], 3),
                                    'template_xy': c['template_xy']} for c in candidates]}
    if len(candidates) != 1:
        evidence['status'] = 'eye_missing' if not candidates else 'eye_ambiguous'
        return {'avatar': None, 'count': None, 'mode': 'spectator',
                'counter_evidence': evidence}

    selected = candidates[0]
    avatar = _avatar(image, screen_width, screen_height, rect, selected)
    row_top = selected['row'][0]
    count, count_evidence = _read_count(reader, image, screen_width, screen_height,
                                        rect, row_top)
    evidence.update({'status': 'selected_row', 'selected_row': list(selected['row']),
                     'counter': count_evidence})
    return {'avatar': avatar, 'count': count, 'mode': 'spectator',
            'counter_evidence': evidence}
