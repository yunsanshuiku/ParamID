"""Binary wire contracts, bounded expressions, and source-to-model integration.

Serial loopback tests actual pyserial buffering, not physical device timing.
Frame corruptions must stop instead of shifting the model's sampling grid.
"""

import json
from pathlib import Path
import random
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import serial

from paramid.acquisition import Acquisition, AcquisitionManager
from paramid.core import IdentificationError
from paramid.decoder import BinaryStream, TYPES, decoder_config, preview_decode


def custom(code, size, **kwargs):
    return {"mode": "custom", "code": code, "payload_bytes": size, **kwargs}


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Binary acquisition timed out")


class BinaryParserTests(unittest.TestCase):
    def test_all_numeric_types_both_byte_orders_and_fragmented_reads(self):
        for dtype, code in TYPES.items():
            values = (1.25, -2.5) if dtype.startswith('float') else ((12, 230) if dtype.startswith('uint') else (-12, 100))
            for order, endian in [('little', '<'), ('big', '>')]:
                with self.subTest(dtype=dtype, order=order):
                    parser = BinaryStream({'dtype': dtype, 'byte_order': order})
                    raw = struct.pack(endian + code * 2, *values)
                    decoded = []
                    for byte in raw * 3:
                        decoded.extend(parser.feed(bytes([byte])))
                    self.assertEqual(decoded, [{'input1': values[0], 'output1': values[1]}] * 3)
                    self.assertEqual(parser.diagnostics()['received_bytes'], len(raw) * 3)
                    self.assertEqual(parser.diagnostics()['pending_bytes'], 0)

    def test_headers_noise_and_header_bytes_inside_payload(self):
        parser = BinaryStream({'dtype': 'uint16', 'header_hex': 'AA 55', 'trailer_hex': '0D 0A'})
        frame = bytes.fromhex('AA 55 AA 55 0D 0A 0D 0A')
        rows = []
        for part in [b'noise\xaa', frame[:1], frame[1:4], frame[4:] + frame]:
            rows.extend(parser.feed(part))
        self.assertEqual(rows, [{'input1': 0x55AA, 'output1': 0x0A0D}] * 2)
        self.assertEqual(parser.discarded_bytes, 6)

    def test_corrupt_frame_stops_after_valid_prefix(self):
        for raw in [bytes.fromhex('AA 55 01 02 FF'), bytes.fromhex('AB 55 01 02 EE')]:
            parser = BinaryStream({'dtype': 'uint8', 'header_hex': 'AA 55', 'trailer_hex': 'EE'})
            stream = parser.feed(bytes.fromhex('AA 55 03 04 EE') + raw)
            self.assertEqual(next(stream), {'input1': 3, 'output1': 4})
            with self.assertRaisesRegex(IdentificationError, '帧头/帧尾'):
                next(stream)
            self.assertEqual(parser.decoded_frames, 1)

    def test_waiting_frame_and_search_have_bounded_storage(self):
        parser = BinaryStream({'header_hex': 'AA 55'})
        for _ in range(16):
            self.assertEqual(list(parser.feed(b'x' * 4096)), [])
            self.assertLess(len(parser.pending), 2)
        with self.assertRaisesRegex(IdentificationError, '64 KiB'):
            list(parser.feed(b'x' * 4096))
        preview = preview_decode({'decoder': {}, 'hex': '00 00 00'})
        self.assertEqual(preview['rows'], [])
        self.assertEqual(preview['pending_bytes'], 3)

    def test_nonfinite_samples_and_wide_integer_rounding_rejected(self):
        for value in [float('nan'), float('inf')]:
            with self.assertRaises(IdentificationError):
                list(BinaryStream({}).feed(struct.pack('<dd', 1, value)))
        with self.assertRaisesRegex(IdentificationError, '精确整数'):
            list(BinaryStream({'dtype': 'uint64'}).feed(struct.pack('<QQ', 2**53 + 1, 1)))
        parser = BinaryStream(custom('time_s = (u64(0) - 9007199254740992) / 1000\ninput1 = u8(8)', 9))
        self.assertEqual(list(parser.feed(struct.pack('<QB', 2**53 + 10, 2))), [{'time_s': .01, 'input1': 2.}])

    def test_mixed_types_padding_scaling_and_previous_channel_reference(self):
        parser = BinaryStream(custom('time_s = u32(0) * 0.001\ninput1 = f32(4)\noutput1 = i16(10) * 0.01 + input1', 12, byte_order='big'))
        raw = struct.pack('>If2xh', 10, 1.5, -200)
        self.assertEqual(list(parser.feed(raw)), [{'time_s': .01, 'input1': 1.5, 'output1': -.5}])

    def test_bitpacked_uint6_and_byte_order_independent_bit_numbering(self):
        for order in ['little', 'big']:
            parser = BinaryStream(custom('input1 = bits(0, 6)\noutput1 = bits(6, 6)', 2, byte_order=order))
            self.assertEqual(list(parser.feed(bytes.fromhex('91 0A'))), [{'input1': 17., 'output1': 42.}])

    def test_assert_checksum_conditionals_and_bit_operations(self):
        config = custom('assert sum8(0, 2) == u8(2) and xor8(0, 2) == 3\ninput1 = (u8(0) << 2) | 1\noutput1 = max(-2, -u8(1)) if u8(0) > 0 else 0', 3)
        parser = BinaryStream(config)
        self.assertEqual(list(parser.feed(b'\x01\x02\x03')), [{'input1': 5., 'output1': -2.}])
        with self.assertRaisesRegex(IdentificationError, 'assert'):
            list(parser.feed(b'\x01\x02\x04'))

    def test_expression_language_rejects_execution_and_unbounded_constructs(self):
        invalid = ['import os', 'while True: pass', 'input1 = __import__("os")\noutput1 = 1',
                   'input1 = (1).__class__\noutput1 = 1', 'input1 = [1][0]\noutput1 = 1',
                   'input1 = 2 ** 100000\noutput1 = 1', 'input1 = missing\noutput1 = 1',
                   'input1 = 1\ninput1 = 2', 'input1 = "abc"\noutput1 = 1', 'f64 = 1\noutput1 = 2']
        for code in invalid:
            with self.subTest(code=code), self.assertRaises(IdentificationError):
                BinaryStream(custom(code, 8))

    def test_expression_runtime_limits_offsets_and_zero_division(self):
        for expression in ['f64(1)', 'u8(-1)', 'u8(0.5)', 'bits(0, 65)', '1 << 10000',
                           '1 / 0', 'sum8(0, 9)', '(u64(0) << 64) << 64 << 64 << 64']:
            with self.subTest(expression=expression), self.assertRaises(IdentificationError):
                list(BinaryStream(custom(f'input1 = {expression}\noutput1 = 1', 8)).feed(b'\xff' * 8))

    def test_configuration_validation_and_type_aliases(self):
        self.assertEqual(decoder_config({'dtype': 'double'})['dtype'], 'float64')
        self.assertEqual(decoder_config({'dtype': 'float'})['payload_bytes'], 8)
        for config in [{'dtype': 'uint6'}, {'columns': ['a', 'a']}, {'columns': ['a']},
                       {'columns': ['a', '']}, {'byte_order': 'native'}, {'header_hex': 'ABC'},
                       custom('u = 1\ny = 2', 4097), custom('u = 1\ny = 2', 0)]:
            with self.subTest(config=config), self.assertRaises(IdentificationError):
                BinaryStream(config)

    def test_preview_includes_prefix_samples_and_frame_error(self):
        result = preview_decode({'decoder': custom('assert u8(2) == sum8(0, 2)\ninput1 = u8(0)\noutput1 = u8(1)', 3),
                                 'hex': '01 02 03 01 02 00'})
        self.assertEqual(result['rows'], [{'input1': 1., 'output1': 2.}])
        self.assertIn('assert', result['error'])
        self.assertEqual(result['decoded_frames'], 1)
        with self.assertRaises(IdentificationError):
            preview_decode({'hex': 'not hex'})

    def test_invalid_decoder_never_opens_hardware_or_claims_port_busy(self):
        with tempfile.TemporaryDirectory() as folder, patch('serial.Serial') as port:
            with self.assertRaisesRegex(IdentificationError, '偏移|表达式|不支持'):
                Acquisition({'kind': 'serial', 'port': 'COM1', 'format': 'binary',
                             'decoder': custom('input1 = open(0)\noutput1 = 1', 8)}, folder)
            with self.assertRaises(IdentificationError) as error:
                Acquisition({'kind': 'serial', 'port': 'COM1', 'header': 'wrong'}, folder)
            self.assertNotIn('占用', str(error.exception))
            port.assert_not_called()


class BinaryPipelineTests(unittest.TestCase):
    def test_serial_binary_to_automatic_model_recording_and_device_template(self):
        with tempfile.TemporaryDirectory() as folder:
            loop = serial.serial_for_url('loop://', timeout=.02, baudrate=3000000)
            manager = AcquisitionManager(Path(folder))
            self.addCleanup(manager.close)
            config = {'kind': 'serial', 'port': 'COM1', 'format': 'binary',
                      'decoder': {'columns': ['time_s', 'input1', 'output1'], 'header_hex': 'AA 55', 'trailer_hex': '0D 0A'}}
            with patch('serial.Serial', return_value=loop):
                initial = manager.connect(config)
            a = manager.get(initial['id'])
            rng, y, previous = random.Random(24), 0., 0.
            frames = []
            for k in range(903):
                u = rng.uniform(-2, 2)
                y = .82 * y + .35 * previous + .04 + rng.gauss(0, .005)
                previous = u
                frames.append(b'\xaa\x55' + struct.pack('<ddd', k * .01, u, y) + b'\x0d\x0a')
            loop.write(b''.join(frames[:3]))
            wait_for(lambda: a.snapshot()['received'] == 3)
            channels = {'input': 'input1', 'output': 'output1', 'time': 'time_s'}
            manager.save_template({'id': a.id, 'name': 'Binary double test', 'channels': channels})
            self.assertEqual(manager.templates()[0]['source']['decoder']['payload_bytes'], 24)
            a.start(channels)
            raw = b''.join(frames[3:])
            for offset in range(0, len(raw), 701):
                loop.write(raw[offset:offset + 701])
            wait_for(lambda: a.snapshot()['fitted'] == 900, timeout=35)
            a.stop()
            self.assertTrue(a.done.wait(10))
            snap = a.snapshot()
            self.assertEqual(snap['error'], '')
            self.assertEqual(snap['recorded'], 900)
            self.assertEqual(snap['decoder_diagnostics']['decoded_frames'], 903)
            self.assertEqual(snap['decoder_diagnostics']['pending_bytes'], 0)
            for parameter, expected in zip(snap['result']['parameters'], [.82, .35, .04]):
                self.assertAlmostEqual(parameter['value'], expected, delta=.008)
            metadata = json.loads((a.directory / 'metadata.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['source']['format'], 'binary')
            self.assertEqual(metadata['source']['decoder']['columns'], ['time_s', 'input1', 'output1'])
            self.assertTrue(a.archive.is_file())


if __name__ == '__main__':
    unittest.main()
