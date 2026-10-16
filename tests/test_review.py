import unittest

import tests.helpers  # noqa: F401

import tq


class ChoiceTest(unittest.TestCase):

    def test_whole_words_only(self):
        self.assertEqual(tq._CHOICES.get("skip"), "k")
        self.assertEqual(tq._CHOICES.get(""), "k")
        self.assertIsNone(tq._CHOICES.get("stop"))
        self.assertIsNone(tq._CHOICES.get("abort"))


class PlaceholderGapTest(unittest.TestCase):

    def test_a_reply_that_skips_a_placeholder_is_flagged(self):
        g = {"english": "{0} sent you {1} for {2}.", "body": "{0} is the sender, {1} the item."}
        self.assertEqual(tq.placeholder_gap(g), ["{2}"])

    def test_no_reply_no_gap(self):
        self.assertEqual(tq.placeholder_gap({"english": "{0} files", "body": ""}), [])


class StripHandlesTest(unittest.TestCase):

    def test_our_own_mentions_are_removed_from_the_question(self):
        self.assertEqual(tq.strip_handles("@demo.reviewer Is this a button?"), "Is this a button?")


class HoldMarkerTest(unittest.TestCase):

    def test_hold_marker_is_recognised(self):
        self.assertTrue(tq.HOLD_RE.match("<!-- tq: hold -->\nWe will confirm."))
        self.assertFalse(tq.HOLD_RE.match("A plain reply."))


if __name__ == "__main__":
    unittest.main()
