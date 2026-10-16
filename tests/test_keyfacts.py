import os, unittest

from tests.helpers import SAMPLE

import keyfacts


class SplitArgsTest(unittest.TestCase):

    def test_commas_inside_calls_and_strings_stay_together(self):
        self.assertEqual(keyfacts.split_args("a, f(b, c), 'd, e', [1, 2]"),
                         ["a", "f(b, c)", "'d, e'", "[1, 2]"])


class ArgumentsTest(unittest.TestCase):

    def test_js_array_unpacks_and_jsx_says_what_it_shows(self):
        path = os.path.join(SAMPLE, "web", "src", "inbox", "TaskRow.tsx")
        self.assertEqual(keyfacts.arguments_at(path, "x-sent-you-x-for-x"),
                         [["task.senderName", "a link showing task.itemTitle", "task.stepName"]])

    def test_single_argument(self):
        path = os.path.join(SAMPLE, "web", "src", "files", "DeleteDialog.tsx")
        self.assertEqual(keyfacts.arguments_at(path, "x-files-deleted"), [["selection.size"]])

    def test_a_key_that_is_only_the_tail_of_another_is_not_matched(self):
        path = os.path.join(SAMPLE, "web", "src", "inbox", "TaskRow.tsx")
        self.assertEqual(keyfacts.arguments_at(path, "sent-you-x-for-x"), [])


class RecordedArgumentsTest(unittest.TestCase):

    def test_component_filled_placeholder_comes_from_the_reference_table(self):
        src, cells, _note = keyfacts.recorded_arguments("go-to-page-x")
        self.assertEqual(src, "reference/component-placeholders.md")
        self.assertEqual(cells, ["{0} = the number of that page, for example `3`"])

    def test_unknown_key_has_no_record(self):
        self.assertIsNone(keyfacts.recorded_arguments("x-files-deleted"))


class SpokenTest(unittest.TestCase):

    def test_announce_is_read_aloud_and_plain_jsx_is_drawn(self):
        dialog = os.path.join(SAMPLE, "web", "src", "files", "DeleteDialog.tsx")
        row = os.path.join(SAMPLE, "web", "src", "inbox", "TaskRow.tsx")
        self.assertTrue(keyfacts.spoken_at("%s:7:" % dialog))
        self.assertFalse(keyfacts.spoken_at("%s:4:" % row))


if __name__ == "__main__":
    unittest.main()
