"""Acquisition verification uses real file appends and pyserial loopback.

Loopback covers the serial adapter without claiming a physical USB-device test.
Known ARX dynamics check that the full source-to-estimator path remains causal.
"""
import csv
import json
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import serial

from paramid.acquisition import Acquisition, AcquisitionManager, CSVStream, FileFollower, channel_config
from paramid.core import IdentificationError


CHANNELS = {"input": "input_v", "output": "output_rad_s", "time": "time_s", "sample_time": .01}


def wait_until(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for acquisition")


def samples(count, begin=0):
    rng, y, old_u = random.Random(24), 0., 0.
    for k in range(begin + count):
        u = rng.uniform(-2, 2)
        y = .82 * y + .35 * old_u + .04 + rng.gauss(0, .005)
        old_u = u
        if k >= begin:
            yield {"time_s": k * .01, "input_v": u, "output_rad_s": y}


class ParserTests(unittest.TestCase):
    def test_split_utf8_bom_multibyte_and_coalesced_lines(self):
        parser = CSVStream()
        raw = '\ufeff时间,输入,输出\r\n0,1,2\r\n0.01,2,3\n'.encode('utf-8')
        rows = []
        for byte in raw:
            rows.extend(parser.feed(bytes([byte])))
        self.assertEqual(rows, [{"时间": 0., "输入": 1., "输出": 2.}, {"时间": .01, "输入": 2., "输出": 3.}])
        parser = CSVStream('gb18030')
        self.assertEqual(list(parser.feed('输入;输出\n1;2\n'.encode('gb18030'))), [{"输入": 1., "输出": 2.}])

    def test_complete_lines_only_and_valid_prefix_survives_bad_line(self):
        parser = CSVStream(header="u,y")
        self.assertEqual(list(parser.feed(b"1,2\n3,")), [{"u": 1., "y": 2.}])
        stream = parser.feed(b"4\n5,6,7\n")
        self.assertEqual(next(stream), {"u": 3., "y": 4.})
        with self.assertRaises(IdentificationError):
            next(stream)

    def test_invalid_data_headers_encoding_and_buffer_limit(self):
        for raw in (b"u,u\n", b"1,2\n", b"u,y\n1,nan\n", b"u,y\n1,inf\n", b"u,y\n1,\xff\n",
                    b"u,y\nu,y\n", b"u,y\n" + b"x" * 8193):
            with self.subTest(raw=raw[:30]), self.assertRaises(IdentificationError):
                list(CSVStream().feed(raw))

    def test_timestamp_scaling_and_nonuniform_preview(self):
        rows = [{"t": k * 10, "u": k, "y": k * 2} for k in range(4)]
        config = {"input": "u", "output": "y", "time": "t", "time_scale": .001}
        self.assertAlmostEqual(channel_config(config, list(rows[0]), rows)["sample_time"], .01)
        rows[-1]["t"] = 60
        with self.assertRaises(IdentificationError):
            channel_config(config, list(rows[0]), rows)
        with self.assertRaises(IdentificationError):
            channel_config({"input": "u", "output": "y", "sample_time": 0}, ["u", "y"], rows)


class FileTests(unittest.TestCase):
    def test_follow_appended_partial_lines_without_repeating_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.csv"
            path.write_bytes(b"t,u,y\n0,1,2\n0.01,2,")
            follower = FileFollower(path, "utf-8-sig")
            try:
                self.assertEqual(len(list(follower.parser.feed(follower.initial))), 1)
                self.assertEqual(list(follower.parser.feed(follower.read())), [])
                with path.open('ab') as f:
                    f.write(b"3\n0.02,3,4\n")
                rows = list(follower.parser.feed(follower.read()))
                self.assertEqual([r['t'] for r in rows], [.01, .02])
                self.assertEqual(list(follower.parser.feed(follower.read())), [])
            finally:
                follower.close()

    def test_truncation_and_rewrite_with_regrowth_are_detected(self):
        for new_content in (b"t,u,y\n", b"t,u,y\n99,99,99\n100,100,100\n"):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "live.csv"
                path.write_bytes(b"t,u,y\n0,1,2\n")
                follower = FileFollower(path, "utf-8-sig")
                try:
                    path.write_bytes(new_content)
                    with self.assertRaises(IdentificationError):
                        follower.read()
                finally:
                    follower.close()

    def test_replaced_file_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.csv"
            path.write_bytes(b"t,u,y\n0,1,2\n")
            follower = FileFollower(path, "utf-8-sig")
            try:
                # Windows may prohibit replacing an open file; identity behavior
                # is then exercised by mocking only the stat result, not the reader.
                info = path.stat()
                fake = type('Info', (), {'st_dev': info.st_dev, 'st_ino': info.st_ino + 1, 'st_size': info.st_size})()
                with patch.object(Path, 'stat', return_value=fake), self.assertRaises(IdentificationError):
                    follower.read()
            finally:
                follower.close()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def manual(self, **kwargs):
        a = Acquisition({"kind": "demo"}, self.temp.name, **kwargs)
        for row in samples(3):
            a._receive(row)
        a.start(CHANNELS)
        return a

    def finish_manual(self, a):
        a.producer_done.set()
        a.worker.start()
        self.assertTrue(a.done.wait(25), "worker must drain and finalize")
        return a.snapshot()

    def test_complete_journal_exceeds_plot_window_and_model_recovers_truth(self):
        a = self.manual()
        for row in samples(2700, begin=3):
            a._receive(row)
        result = self.finish_manual(a)
        self.assertEqual(result['error'], '')
        self.assertEqual((result['accepted'], result['recorded'], result['fitted']), (2700, 2700, 2700))
        model = result['result']
        self.assertEqual([model['model'][k] for k in ('na', 'nb', 'nk')], [1, 1, 1])
        self.assertEqual(len(model['series']), 2000)
        self.assertEqual(model['series'][0]['time'], 7.03)
        for parameter, true in zip(model['parameters'], [.82, .35, .04]):
            self.assertAlmostEqual(parameter['value'], true, delta=.003)
        with zipfile.ZipFile(a.archive) as archive:
            raw = list(csv.DictReader(io_text(archive.read('raw_samples.csv'))))
            predictions = list(csv.DictReader(io_text(archive.read('predictions.csv'))))
            self.assertEqual(len(raw), 2700)
            self.assertEqual(len(predictions), 2300)
            self.assertAlmostEqual(float(predictions[0]['time_s']), 4.03)
            self.assertIn('非真实设备', archive.read('report.md').decode('utf-8'))
            self.assertIn('G(z) =', archive.read('report.md').decode('utf-8'))
            exported = json.loads(archive.read('model.json'))
            self.assertEqual(exported['mathematical_model']['state_space']['dimension'], 2)
            self.assertEqual(exported['acquisition_channels']['input'], 'input_v')
            self.assertEqual(exported['mathematical_model']['continuous_model']['status'], 'available')
            self.assertIn('G_c(s)', archive.read('report.md').decode('utf-8'))

    def test_queue_overflow_stops_without_dropping_previously_accepted_rows(self):
        a = self.manual(queue_limit=2)
        for row in samples(5, begin=3):
            a._receive(row)
        result = self.finish_manual(a)
        self.assertIn('队列已满', result['error'])
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(result['recorded'], 2)
        self.assertEqual(result['fitted'], 2)
        self.assertIsNotNone(a.rejected)

    def test_time_discontinuity_preserves_prefix_and_rejected_sample(self):
        a = self.manual()
        for row in samples(5, begin=3):
            a._receive(row)
        a._receive({'time_s': .2, 'input_v': 1., 'output_rad_s': 2.})
        result = self.finish_manual(a)
        self.assertIn('采样时间', result['error'])
        self.assertEqual(result['recorded'], 5)
        self.assertEqual(json.loads((a.directory / 'metadata.json').read_text(encoding='utf-8'))['rejected_sample']['time_s'], .2)

    def test_estimation_failure_still_journals_accepted_queue(self):
        a = self.manual()
        for row in samples(300, begin=3):
            a._receive(row)
        with patch.object(a.session, 'push', side_effect=IdentificationError('test numerical failure')):
            result = self.finish_manual(a)
        self.assertEqual(result['recorded'], 300)
        self.assertEqual(result['fitted'], 0)
        self.assertIn('test numerical failure', result['error'])

    def test_disk_failure_is_visible_and_does_not_claim_complete_recording(self):
        a = self.manual()
        for row in samples(4, begin=3):
            a._receive(row)
        a.files[0].close()
        result = self.finish_manual(a)
        self.assertIn('写入失败', result['error'])
        self.assertEqual(result['recorded'], 0)
        self.assertEqual(result['accepted'], 4)

    def test_demo_real_threads_lifecycle_and_automatic_selection(self):
        manager = AcquisitionManager(self.temp.name)
        initial = manager.connect({'kind': 'demo'})
        a = manager.get(initial['id'])
        self.addCleanup(manager.close)
        wait_until(lambda: len(a.snapshot()['preview']) >= 3)
        a.start(CHANNELS)
        with self.assertRaises(IdentificationError):
            manager.connect({'kind': 'demo'})
        with self.assertRaises(IdentificationError):
            a.start(CHANNELS)
        wait_until(lambda: a.snapshot()['fitted'] > 450)
        a.stop()
        self.assertTrue(a.done.wait(10))
        snap = a.snapshot()
        self.assertEqual(snap['recorded'], snap['accepted'])
        self.assertEqual(snap['fitted'], snap['accepted'])
        self.assertTrue(snap['result']['parameters'])
        self.assertFalse(a.producer.is_alive())
        self.assertTrue(manager.download(a.id).is_file())
        with self.assertRaises(IdentificationError):
            manager.download('../model.json')

    def test_serial_loopback_split_receive_and_disconnect(self):
        loop = serial.serial_for_url('loop://', timeout=.05)
        with patch('serial.Serial', return_value=loop):
            a = Acquisition({'kind': 'serial', 'port': 'COM1'}, self.temp.name)
        a.connect()
        self.addCleanup(lambda: (a.stop(), a.done.wait(5)))
        loop.write(b'time_s,input_v,out')
        loop.write(b'put_rad_s\n0,1,2\n0.01,2,3\n0.02,3,4\n')
        wait_until(lambda: a.snapshot()['received'] == 3)
        a.start(CHANNELS)
        loop.write(b'0.03,4,5\n0.04,5,6\n')
        wait_until(lambda: a.snapshot()['recorded'] == 2)
        loop.close()
        self.assertTrue(a.done.wait(5))
        self.assertIn('接收已停止', a.snapshot()['error'])
        self.assertEqual(a.snapshot()['recorded'], 2)

    def test_file_source_templates_and_new_rows_only(self):
        path = Path(self.temp.name) / 'live.csv'
        path.write_text('time_s,input_v,output_rad_s\n0,1,2\n0.01,2,3\n0.02,3,4\n', encoding='utf-8')
        manager = AcquisitionManager(Path(self.temp.name) / 'experiments')
        initial = manager.connect({'kind': 'file', 'path': str(path)})
        a = manager.get(initial['id'])
        self.addCleanup(manager.close)
        manager.save_template({'id': a.id, 'name': '测试设备', 'channels': CHANNELS})
        self.assertEqual(manager.templates()[0]['channels']['sample_time'], .01)
        a.start(CHANNELS)
        with path.open('a', encoding='utf-8') as f:
            f.write('0.03,4,5\n0.04,5,6\n')
        wait_until(lambda: a.snapshot()['recorded'] == 2)
        a.stop()
        self.assertTrue(a.done.wait(5))
        self.assertEqual(a.snapshot()['recorded'], 2)
        self.assertEqual(a.snapshot()['received'], 5)


def io_text(raw):
    import io
    return io.StringIO(raw.decode('utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
