import json
import logging
import sys

from django.test import SimpleTestCase

from mentoroai.logging import JsonFormatter


def make_record(msg="hello", level=logging.INFO, args=(), exc_info=None, stack_info=None):
    record = logging.LogRecord(
        name="mentoroai.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    record.stack_info = stack_info
    return record


class JsonFormatterTests(SimpleTestCase):
    def test_basic_record_is_rendered_as_json_with_core_fields(self):
        record = make_record(msg="hello world", level=logging.WARNING)

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["level"], "WARNING")
        self.assertEqual(payload["message"], "hello world")
        self.assertIsInstance(payload["timestamp"], str)
        self.assertNotIn("exc_info", payload)
        self.assertNotIn("stack", payload)

    def test_message_args_are_interpolated_via_getmessage(self):
        record = make_record(msg="user %s did %s", args=("jane", "login"))

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["message"], "user jane did login")

    def test_non_ascii_message_is_preserved_unescaped(self):
        record = make_record(msg="café ist bereit")

        rendered = JsonFormatter().format(record)

        self.assertIn("café ist bereit", rendered)
        payload = json.loads(rendered)
        self.assertEqual(payload["message"], "café ist bereit")

    def test_exception_info_is_included_when_present(self):
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        record = make_record(msg="failed", exc_info=exc_info)

        payload = json.loads(JsonFormatter().format(record))

        self.assertIn("exc_info", payload)
        self.assertIn("ValueError: boom", payload["exc_info"])

    def test_stack_info_is_included_when_present(self):
        record = make_record(msg="deep call", stack_info="Stack (most recent call last):\n  fake frame")

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["stack"], "Stack (most recent call last):\n  fake frame")

    def test_different_logger_names_do_not_affect_payload_shape(self):
        record = make_record(msg="from anywhere")
        record.name = "guides.signals"

        payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(set(payload.keys()), {"timestamp", "level", "message"})

    def test_output_is_valid_single_line_json(self):
        record = make_record(msg="line1\nline2 with \"quotes\"")

        rendered = JsonFormatter().format(record)

        self.assertEqual(rendered.count("\n"), 0)
        payload = json.loads(rendered)
        self.assertEqual(payload["message"], 'line1\nline2 with "quotes"')
