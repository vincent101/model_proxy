"""阶段 C：纯合成 SSE 行终止、分片与逐事件解析不变量。"""

import json
import unittest
from unittest.mock import patch

from core import translate as pt


class TestSSEFraming(unittest.TestCase):
    ENDINGS = (b"\n", b"\r\n", b"\r")

    @staticmethod
    def _wire(ending):
        lines = [
            b": keep-alive", b"",
            b"id: synthetic", b"",
            b"event: delta", b'data: {"type":"delta",',
            'data: "text":"合成🙂"}'.encode(), b"",
            b'data: {"type":"tail","n":1}', b"",
        ]
        # 混合时，CR 后的 LF 必须是配对而非第二行末；此序列的空行均独立。
        mixed = (b"\r\n", b"\r", b"\n", b"\r\n", b"\n",
                 b"\r\n", b"\n", b"\r", b"\r", b"\r")
        return b"".join(line + (mixed[i] if ending is None else ending)
                        for i, line in enumerate(lines))

    def _assert_live(self, chunks):
        framer = pt.SSEFramer()
        events = []
        for chunk in chunks:
            events.extend(framer.feed(chunk))
        # 活流检查不调用 finish：最后 CR 空行也必须已交付。
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e.is_comment for e in events[:2]))
        self.assertEqual([(e.event_type, e.data) for e in events[2:]],
                         [("delta", {"type": "delta", "text": "合成🙂"}),
                          ("tail", {"type": "tail", "n": 1})])
        self.assertTrue(framer.saw_business_event)
        self.assertEqual(framer.feed(b""), [])

    def test_all_endings_every_single_cut_and_bytewise(self):
        for ending in (*self.ENDINGS, None):
            wire = self._wire(ending)
            for cut in range(len(wire) + 1):
                with self.subTest(ending=ending, cut=cut):
                    self._assert_live([wire[:cut], wire[cut:]])
            with self.subTest(ending=ending, bytewise=True):
                self._assert_live([wire[i:i + 1] for i in range(len(wire))])

    def test_cr_blank_dispatches_before_paired_lf_arrives(self):
        framer = pt.SSEFramer()
        first = framer.feed(b'data: {"type":"first"}\r\n\r')
        self.assertEqual([e.event_type for e in first], ["first"])
        self.assertEqual(framer.feed(b"\n"), [])
        second = framer.feed(b'data: {"type":"second"}\r\r')
        self.assertEqual([e.event_type for e in second], ["second"])
        self.assertEqual(framer.feed(b"\n"), [])

    def test_mixed_line_endings_dispatch_without_finish(self):
        framer = pt.SSEFramer()
        events = framer.feed(
            b'event: delta\rdata: {"type":"delta",\r\n'
            b'data: "n":1}\n\r')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, {"type": "delta", "n": 1})

    def test_one_line_ending_is_not_a_blank_line(self):
        for ending in self.ENDINGS:
            with self.subTest(ending=ending):
                framer = pt.SSEFramer()
                self.assertEqual(framer.feed(b'data: {"n":1}' + ending), [])
                self.assertEqual(len(framer.feed(ending)), 1)

    def test_utf8_is_decoded_only_after_complete_payload(self):
        wire = 'data: {"type":"delta","text":"汉🙂"}\r\n\r\n'.encode()
        for cut in range(len(wire)):
            with self.subTest(cut=cut):
                framer = pt.SSEFramer()
                with patch.object(pt.json, "loads", wraps=json.loads) as loads:
                    events = framer.feed(wire[:cut])
                    if not events:
                        loads.assert_not_called()
                    events.extend(framer.feed(wire[cut:]))
                self.assertEqual([e.data["text"] for e in events], ["汉🙂"])
                self.assertNotIn("\ufffd", events[0].data["text"])

    def test_multidata_lf_join_removes_only_one_optional_space(self):
        framer = pt.SSEFramer()
        with patch.object(pt.json, "loads", wraps=json.loads) as loads:
            events = framer.feed(
                b'event: delta\ndata:   {"type":"delta",\n'
                b'data:\t"text":" x "}\n\n')
        self.assertEqual(events[0].data["text"], " x ")
        payload = loads.call_args.args[0]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        self.assertEqual(payload, '  {"type":"delta",\n\t"text":" x "}')

    def test_missing_event_uses_data_type(self):
        events = pt.SSEFramer().feed(b'data: {"type":"fallback"}\n\n')
        self.assertEqual(events[0].event_type, "fallback")

    def test_chat_without_type_is_left_to_consumer(self):
        event = pt.SSEFramer().feed(b'data: {"choices":[]}\n\n')[0]
        self.assertIsNone(event.event_type)
        self.assertEqual(event.data, {"choices": []})

    def test_comments_and_id_only_are_not_business_events(self):
        framer = pt.SSEFramer()
        events = framer.feed(b": ping\r\rid: x\rretry: 1000\r\r\n\n")
        self.assertTrue(events)
        self.assertTrue(all(e.is_comment for e in events))
        self.assertFalse(framer.saw_business_event)

    def test_eof_comment_and_id_only_are_ignored(self):
        for tail in (b": ping", b"id: x", b"id: x\rretry: 1000"):
            with self.subTest(tail=tail):
                framer = pt.SSEFramer()
                self.assertEqual(framer.feed(tail), [])
                self.assertTrue(all(e.is_comment for e in framer.finish()))
                self.assertFalse(framer.saw_business_event)
                self.assertEqual(framer.finish(), [])

    def test_eof_accepts_complete_tail_and_marks_business_event(self):
        for ending in (b"", *self.ENDINGS):
            with self.subTest(ending=ending):
                framer = pt.SSEFramer()
                self.assertEqual(framer.feed(b'data: {"type":"tail"}' + ending), [])
                self.assertEqual([e.event_type for e in framer.finish()], ["tail"])
                self.assertTrue(framer.saw_business_event)
                self.assertEqual(framer.buffered_bytes, 0)
                self.assertEqual(framer.finish(), [])

    def test_empty_or_whitespace_eof_is_empty(self):
        for tail in (b"", b" ", b"\t", b"\r\n"):
            with self.subTest(tail=tail):
                framer = pt.SSEFramer()
                framer.feed(tail)
                self.assertEqual(framer.finish(), [])

    def test_bad_multidata_json_is_translation_error(self):
        for ending in self.ENDINGS:
            with self.subTest(ending=ending):
                with self.assertRaises(pt.TranslationError) as caught:
                    pt.SSEFramer().feed(ending.join(
                        [b'data: {"n":1}', b'data: {"n":2}', b"", b""]))
                self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_event_data_type_conflict_is_translation_error(self):
        with self.assertRaises(pt.TranslationError) as caught:
            pt.SSEFramer().feed(b'event: a\rdata: {"type":"b"}\r\r')
        self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_event_only_frame_is_malformed(self):
        for wire in (b"event: delta\n\n", b"event: delta"):
            with self.subTest(wire=wire):
                framer = pt.SSEFramer()
                with self.assertRaises(pt.TranslationError) as caught:
                    framer.feed(wire)
                    framer.finish()
                self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_invalid_utf8_never_replaces_bytes(self):
        for wire in (b'data: {"text":"\xff"}\n\n',
                     b'event: \xff\ndata: {"n":1}\n\n'):
            with self.subTest(wire=wire):
                with self.assertRaises(pt.TranslationError) as caught:
                    pt.SSEFramer().feed(wire)
                self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_done_exact_payload_and_extra_space_is_not_trimmed(self):
        event = pt.SSEFramer().feed(b"data: [DONE]\r\r")[0]
        self.assertTrue(event.is_done)
        with self.assertRaises(pt.TranslationError):
            pt.SSEFramer().feed(b"data:  [DONE]\n\n")

    def test_max_events_one_protects_good_frame_from_bad_tail(self):
        for ending in self.ENDINGS:
            with self.subTest(ending=ending):
                good = b'data: {"type":"terminal"}' + ending * 2
                bad = b"data: {bad}" + ending * 2
                framer = pt.SSEFramer()
                first = framer.feed(good + bad, max_events=1)
                self.assertEqual([e.event_type for e in first], ["terminal"])
                self.assertGreater(framer.buffered_bytes, 0)
                with self.assertRaises(pt.TranslationError):
                    framer.feed(b"", max_events=1)

    def test_zero_limit_defers_and_empty_feed_drains_one_at_a_time(self):
        framer = pt.SSEFramer()
        wire = b'data: {"n":1}\r\rdata: {"n":2}\r\r'
        self.assertEqual(framer.feed(wire, max_events=0), [])
        self.assertEqual(framer.buffered_bytes, len(wire))
        self.assertEqual(framer.feed(b"", max_events=1)[0].data, {"n": 1})
        self.assertEqual(framer.feed(b"", max_events=1)[0].data, {"n": 2})
        self.assertEqual(framer.feed(b"", max_events=1), [])

    def test_finish_drains_deferred_frames_before_eof_tail(self):
        framer = pt.SSEFramer()
        first = framer.feed(b'data: {"n":1}\n\ndata: {"n":2}\r\r'
                            b'data: {"n":3}', max_events=1)
        self.assertEqual(first[0].data, {"n": 1})
        self.assertEqual([e.data for e in framer.finish()], [{"n": 2}, {"n": 3}])
        self.assertEqual(framer.finish(), [])

    def test_malformed_eof_tail_stays_strict(self):
        for tail in (b"bare garbage", b'data: {"n":', b"event: x"):
            with self.subTest(tail=tail):
                framer = pt.SSEFramer()
                framer.feed(tail)
                with self.assertRaises(pt.TranslationError) as caught:
                    framer.finish()
                self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_same_chunk_raw_bytes_are_not_reserialized(self):
        for ending in self.ENDINGS:
            with self.subTest(ending=ending):
                raw = ending.join([b"event: x", b'data: { "n" : 1 }', b"", b""])
                self.assertEqual(pt.SSEFramer().feed(raw)[0].raw, raw)


class TestEventOnlyConversionCompat(unittest.TestCase):
    def test_event_only_all_endings_chunk_cuts_and_real_frame_same_chunk(self):
        for ending in (b"\n", b"\r\n", b"\r", None):
            sep = ending or b"\r"
            prefix = (b"event: ping" + sep + b": keep" + (ending or b"\r\n") +
                      b"id: x" + (ending or b"\n") + b"retry: 5" + sep + sep)
            wire = prefix + b'data: {"type":"real"}' + sep + sep
            for cut in range(len(wire) + 1):
                with self.subTest(ending=ending, cut=cut):
                    f = pt.SSEFramer(allow_event_only=True)
                    events = f.feed(wire[:cut]) + f.feed(wire[cut:]) + f.finish()
                    self.assertEqual([(e.event_type, e.data) for e in events if not e.is_comment],
                                     [("real", {"type": "real"})])
                    self.assertTrue(f.saw_business_event)

    def test_event_only_eof_is_not_business(self):
        for ending in (b"", b"\n", b"\r", b"\r\n"):
            f = pt.SSEFramer(allow_event_only=True)
            events = f.feed(b"event: ping" + ending)
            self.assertTrue(all(e.is_comment for e in events))
            with self.assertRaises(pt.TranslationError) as caught:
                f.finish()
            self.assertEqual(caught.exception.reason, "empty_stream")
            self.assertFalse(f.saw_business_event)

    def test_event_only_eof_after_business_is_ignored(self):
        f = pt.SSEFramer(allow_event_only=True)
        events = f.feed(b'data: {"type":"real"}\n\nevent: ping') + f.finish()
        self.assertEqual([(e.event_type, e.data) for e in events if not e.is_comment],
                         [("real", {"type": "real"})])

    def test_compat_does_not_suppress_data_errors_or_conflicts(self):
        for wire in (b"event: x\ndata:\n\n", b"event: x\ndata\n\n",
                     b"data\n\n", b"event: x\ndata: {bad}\n\n",
                     b'event: x\ndata: {"type":"y"}\n\n', b"event: x\ndata: \n\n"):
            with self.subTest(wire=wire):
                with self.assertRaises(pt.TranslationError):
                    pt.SSEFramer(allow_event_only=True).feed(wire)

    def test_static_parse_default_remains_strict(self):
        with self.assertRaises(pt.TranslationError):
            pt.SSEFramer._parse(b"event: ping", b"event: ping\n\n")


    def test_empty_event_name_compat_all_endings_cuts_and_following_business(self):
        for event in (b"event:", b"event: "):
            for ending in (b"\n", b"\r\n", b"\r"):
                wire = (event + ending + b": comment" + ending + b"id: x" + ending + ending +
                        b'data: {"type":"real"}' + ending + ending)
                for cut in range(len(wire) + 1):
                    with self.subTest(event=event, ending=ending, cut=cut):
                        f = pt.SSEFramer(allow_event_only=True)
                        events = f.feed(wire[:cut]) + f.feed(wire[cut:]) + f.finish()
                        self.assertEqual([(e.event_type, e.data) for e in events if not e.is_comment],
                                         [("real", {"type": "real"})])
                        self.assertTrue(f.saw_business_event)

    def test_empty_event_name_default_strict_and_data_errors_still_strict(self):
        for event in (b"event:", b"event: "):
            for ending in (b"\n", b"\r\n", b"\r"):
                with self.subTest(event=event, ending=ending):
                    with self.assertRaises(pt.TranslationError) as caught:
                        pt.SSEFramer().feed(event + ending + ending)
                    self.assertEqual(caught.exception.reason, "malformed_stream")
                    for data in (b"data:", b"data", b"data: {bad}"):
                        with self.assertRaises(pt.TranslationError) as caught:
                            pt.SSEFramer(allow_event_only=True).feed(
                                event + ending + data + ending + ending)
                        self.assertEqual(caught.exception.reason, "malformed_stream")

    def test_empty_event_name_alone_eof_is_empty_stream(self):
        for event in (b"event:", b"event: "):
            for ending in (b"", b"\n", b"\r\n", b"\r"):
                with self.subTest(event=event, ending=ending):
                    f = pt.SSEFramer(allow_event_only=True)
                    events = f.feed(event + ending)
                    self.assertEqual(events, [])
                    with self.assertRaises(pt.TranslationError) as caught:
                        f.finish()
                    self.assertEqual(caught.exception.reason, "empty_stream")
                    self.assertFalse(f.saw_business_event)


if __name__ == "__main__":
    unittest.main()
