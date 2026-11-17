"""Focused tests for spectator roster selection and count reading."""
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import spectator


SCREEN_W, SCREEN_H = 2560, 1440
RECT = (12, 258, 628, 1110)
ROW_TOPS = (728, 857, 1001, 1145)


def roster(eye_rows=()):
    image = np.full((RECT[3], RECT[2], 3), 150, dtype=np.uint8)
    for top in ROW_TOPS:
        x1, y1 = 160 - RECT[0], top - RECT[1]
        x2, y2 = 600 - RECT[0], top + 116 - RECT[1]
        cv2.rectangle(image, (x1, y1), (x2 - 1, y2 - 1), (48, 48, 48), -1)
    template = spectator._eye_template()
    for row_index in eye_rows:
        top = ROW_TOPS[row_index]
        x, y = 52 - RECT[0], top + 29 - RECT[1]
        image[y:y + template.shape[0], x:x + template.shape[1]] = template[:, :, None]
    return image


def right_roster():
    full = np.full((SCREEN_H, SCREEN_W, 3), 150, dtype=np.uint8)
    template = spectator._eye_template()
    x, y = 2425, 1030
    full[y:y + template.shape[0], x:x + template.shape[1]] = template[:, :, None]
    rect = (1920, 546, 626, 822)
    return full[rect[1]:rect[1] + rect[3], rect[0]:rect[0] + rect[2]], rect


class FakeReader:
    def __init__(self, text='3', confidence=.99):
        self.text = text
        self.confidence = confidence
        self.image = None
        self.options = None

    def readtext(self, image, **kwargs):
        self.image = image.copy()
        self.options = kwargs
        return [([[0, 0], [10, 0], [10, 20], [0, 20]], self.text, self.confidence)]


class SpectatorTests(unittest.TestCase):
    def test_non_roster_image_returns_none(self):
        image = np.full((RECT[3], RECT[2], 3), 96, dtype=np.uint8)
        self.assertIsNone(spectator.detect(image, SCREEN_W, SCREEN_H, RECT))

    def test_confirmed_roster_without_clear_eye_does_not_fall_back(self):
        result = spectator.detect(roster(), SCREEN_W, SCREEN_H, RECT)
        self.assertEqual(result['mode'], 'spectator')
        self.assertIsNone(result['avatar'])
        self.assertIsNone(result['count'])
        self.assertEqual(result['counter_evidence']['status'], 'eye_missing')

    def test_unique_eye_selects_row_without_reader_and_preserves_count_unknown(self):
        result = spectator.detect(roster((2,)), SCREEN_W, SCREEN_H, RECT, reader=None)
        self.assertEqual(result['mode'], 'spectator')
        self.assertEqual(result['count'], None)
        self.assertAlmostEqual(result['avatar']['center'][0], 91 / SCREEN_W, places=3)
        self.assertAlmostEqual(result['avatar']['center'][1], 1057 / SCREEN_H, places=3)
        self.assertEqual(result['counter_evidence']['counter']['reason'], 'reader_unavailable')

    def test_unique_eye_reads_only_digits_from_its_row_counter(self):
        reader = FakeReader('7')
        result = spectator.detect(roster((2,)), SCREEN_W, SCREEN_H, RECT, reader=reader)
        self.assertEqual(result['avatar']['center'][1], round(1057 / SCREEN_H, 4))
        self.assertEqual(result['count'], 7)
        self.assertEqual(reader.options['allowlist'], '0123456789')
        self.assertEqual(reader.options['paragraph'], False)
        self.assertIsNotNone(reader.image)
        self.assertEqual(result['counter_evidence']['counter']['status'], 'read')

    def test_low_confidence_counter_stays_unknown(self):
        reader = FakeReader('0', .25)
        result = spectator.detect(roster((2,)), SCREEN_W, SCREEN_H, RECT, reader=reader)
        self.assertIsNone(result['count'])
        self.assertEqual(result['counter_evidence']['counter']['status'], 'unreadable')

    def test_multiple_eye_candidates_are_not_guessed(self):
        result = spectator.detect(roster((1, 2)), SCREEN_W, SCREEN_H, RECT,
                                  reader=FakeReader())
        self.assertEqual(result['mode'], 'spectator')
        self.assertIsNone(result['avatar'])
        self.assertIsNone(result['count'])
        self.assertEqual(result['counter_evidence']['status'], 'eye_ambiguous')

    def test_right_roster_keeps_avatar_and_counter_orientation(self):
        image, rect = right_roster()
        reader = FakeReader('8')
        result = spectator.detect(image, SCREEN_W, SCREEN_H, rect, reader=reader)
        self.assertEqual(result['mode'], 'spectator')
        self.assertEqual(result['count'], 8)
        self.assertAlmostEqual(result['avatar']['center'][0], 2464 / SCREEN_W, places=3)
        self.assertAlmostEqual(result['avatar']['center'][1], 1057 / SCREEN_H, places=3)
        self.assertEqual(result['counter_evidence']['counter']['rect'][0], 1984)


if __name__ == '__main__':
    unittest.main()
