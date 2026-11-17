import json
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from visual_feed import analyze, highlight_rects  # noqa: E402


GOLD = (55, 165, 245)       # BGR
BLUE = (205, 150, 75)       # BGR
WHITE = (245, 245, 245)


def _text(img, text, x, baseline, color, scale=0.66, thickness=2):
    canvas = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", max(14, round(scale * 32)))
    draw.text((x, baseline - round(scale * 32)), text,
              fill=(int(color[2]), int(color[1]), int(color[0])), font=font)
    img[:] = cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)


def _text_width(text, scale=0.66):
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", max(14, round(scale * 32)))
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)[2]


def _gun(img, x, y, width=82, height=22, variant=0):
    cv2.line(img, (x, y + height // 2), (x + width, y + height // 2), WHITE, 4, cv2.LINE_AA)
    cv2.rectangle(img, (x + width // 3, y + 4), (x + width * 2 // 3, y + height // 2), WHITE, -1)
    cv2.line(img, (x + width // 2, y + height // 2), (x + width // 2 - 5, y + height), WHITE, 4)
    if variant:
        cv2.line(img, (x + width // 2, y + 2), (x + width - 3, y + 2), WHITE, 2)


def _frame(role, own="Scarface", attacker="Abandon", victim="Koldplay",
           own_color=WHITE, victim_color=BLUE, border=2, assist_mark=True, icon_variant=0):
    img = np.full((68, 620, 3), (12, 12, 14), dtype=np.uint8)
    cv2.rectangle(img, (2, 3), (617, 64), (0, 0, 230), border, cv2.LINE_AA)
    if role == "kill":
        if icon_variant == 1:  # a compact flash / blindness icon before the local name
            cv2.circle(img, (23, 33), 11, WHITE, 2, cv2.LINE_AA)
            cv2.line(img, (16, 40), (30, 26), WHITE, 3, cv2.LINE_AA)
            name_x = 45
        else:
            name_x = 18
        _text(img, own, name_x, 43, own_color, .68)
        _gun(img, 236, 22, 92, 22, icon_variant == 2)
        # A compact headshot/skull mark remains white, like other kill icons.
        cv2.circle(img, (390, 34), 12, WHITE, 2, cv2.LINE_AA)
        cv2.line(img, (381, 42), (399, 26), WHITE, 2, cv2.LINE_AA)
        _text(img, victim, 430, 43, victim_color, .66)
    elif role == "assist":
        _text(img, attacker, 18, 43, GOLD, .82)
        attack_width = _text_width(attacker, .82)
        plus_x = 18 + attack_width + 9
        if assist_mark:
            _text(img, "+", plus_x, 43, WHITE, .72, 2)
        _text(img, own, plus_x + 31, 43, own_color, .68)
        _gun(img, 330, 22, 92, 22, icon_variant == 2)
        _text(img, victim, 455, 43, victim_color, .66)
    elif role == "death":
        _text(img, attacker, 18, 43, GOLD, .82)
        _gun(img, 205, 22, 92, 22, icon_variant == 2)
        _text(img, own, 412, 43, own_color, .68)
    elif role == "self_death":
        _text(img, own, 18, 43, own_color, .68)
        _text(img, "+", 125, 43, WHITE, .72, 2)
        _text(img, attacker, 157, 43, BLUE, .82)
        # Bomb-like compact white icon between the two own-name runs.
        cv2.rectangle(img, (304, 20), (348, 46), WHITE, -1)
        cv2.line(img, (313, 28), (339, 28), (20, 20, 20), 2)
        _text(img, own, 412, 43, own_color, .68)
    elif role == "icons_only":
        _gun(img, 190, 22, 92, 22)
        cv2.circle(img, (350, 34), 12, WHITE, 2)
    return img


class VisualFeedTests(unittest.TestCase):
    def test_role_layouts_and_name_width_variants(self):
        positives = [
            ("Lio7", "kill", BLUE, 2, 0),
            ("Scarface", "kill", GOLD, 3, 1),
            ("VeryWideName", "kill", BLUE, 4, 2),
            ("Kz2026", "kill", GOLD, 2, 0),
        ]
        for name, expected, color, border, icon_variant in positives:
            with self.subTest(name=name, border=border, icon=icon_variant):
                row = _frame(expected, own=name, victim="GoldVictim" if color == GOLD else "BlueVictim",
                             victim_color=color, border=border, icon_variant=icon_variant)
                rows = analyze(row)
                self.assertEqual([r["role"] for r in rows], ["kill"])
                self.assertEqual(len(rows[0]["rect"]), 4)

    def test_assist_death_and_self_death_are_not_kills(self):
        cases = [
            (_frame("assist", attacker="LongPrimary", victim="BlueVictim", border=2), "assist"),
            (_frame("assist", attacker="GP", victim="GoldVictim", victim_color=GOLD, border=4), "assist"),
            (_frame("death", attacker="nyxx", victim="Scarface", border=3), "death"),
            (_frame("self_death", attacker="xie", victim="Scarface", border=2), "self_death"),
        ]
        for row, expected in cases:
            with self.subTest(expected=expected):
                result = analyze(row)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["role"], expected)
                self.assertNotEqual(result[0]["role"], "kill")

    def test_local_killer_with_teammate_name_after_plus_is_still_kill(self):
        row = _frame("kill", own="Scarface", victim="Hypex1", icon_variant=2)
        # A colored teammate name after the white local killer can be an assist
        # annotation; insert it before the weapon without changing the main name.
        _text(row, "+", 143, 43, WHITE, .72, 2)
        _text(row, "Kxz", 172, 43, GOLD, .68)
        self.assertEqual([r["role"] for r in analyze(row)], ["kill"])

    def test_border_detection_deduplicates_rows_and_handles_no_feed(self):
        first = _frame("kill", own="Scarface", border=2)
        second = _frame("death", attacker="nyxx", border=4)
        canvas = np.full((170, 660, 3), (20, 20, 20), dtype=np.uint8)
        canvas[5:73, 20:640] = first
        canvas[90:158, 20:640] = second
        boxes = highlight_rects(canvas)
        self.assertEqual(len(boxes), 2)
        self.assertEqual([r["role"] for r in analyze(canvas)], ["kill", "death"])
        self.assertEqual(highlight_rects(np.zeros((200, 700, 3), dtype=np.uint8)), [])
        self.assertEqual(analyze(np.zeros((200, 700, 3), dtype=np.uint8)), [])

        # Warm stone can pass a red/green ratio test, but it is not a saturated
        # red HUD outline and must not create a false notification rectangle.
        wall = np.full((180, 700, 3), (71, 96, 143), dtype=np.uint8)
        cv2.rectangle(wall, (220, 40), (650, 110), (71, 96, 143), 3)
        self.assertEqual(highlight_rects(wall), [])

    def test_icons_alone_do_not_count_as_kill(self):
        result = analyze(_frame("icons_only"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "unknown")

    def test_signature_and_evidence_are_stable_json_shapes(self):
        row = _frame("kill", own="Scarface", victim="AlphaWolf")
        result = analyze(row)[0]
        self.assertRegex(result["visual_signature"], r"^dhash128:[0-9a-f]{32}$")
        self.assertEqual(len(result["rect_size"]), 2)
        json.dumps(result["evidence"])

        def distance(a, b):
            return (int(a.split(":")[-1], 16) ^ int(b.split(":")[-1], 16)).bit_count()

        faded = analyze(cv2.convertScaleAbs(row, alpha=.70))[0]
        other_victim = analyze(_frame("kill", own="Scarface", victim="Hypex1"))[0]
        self.assertLessEqual(distance(result["visual_signature"], faded["visual_signature"]), 8)
        self.assertGreater(distance(result["visual_signature"], other_victim["visual_signature"]), 8)


if __name__ == "__main__":
    unittest.main()
