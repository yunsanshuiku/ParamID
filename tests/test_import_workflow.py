"""Headerless imports and original-signal dynamic identification workflows."""
import csv
import io
import json
import unittest

import numpy as np
from scipy.signal import lfilter

from paramid.core import IdentificationError
from paramid.data import detect_channels, parse_table, resolve_channels
from paramid.service import offline


def pulse_record(header=True, names=None):
    """Independent periodic pulses with known coupled dynamics and DC offsets."""
    k = np.arange(1200)
    u = np.column_stack(((k%67 < 19).astype(float), ((k+11)%89 < 27).astype(float)))
    y = np.column_stack((lfilter([0., .24], [1., -.8], u[:, 0])+lfilter([0., -.09], [1., -.8], u[:, 1])+3.,
                         lfilter([0., .16], [1., -.6], u[:, 0])+lfilter([0., .28], [1., -.6], u[:, 1])-2.))
    data = np.column_stack((k*.02, u, y))
    buf = io.StringIO(); writer = csv.writer(buf)
    if header:
        writer.writerow(names or ['time', 'input1', 'input2', 'output1', 'output2'])
    writer.writerows(data)
    return buf.getvalue(), data


class HeaderTests(unittest.TestCase):
    def test_first_numeric_sample_is_retained(self):
        table = parse_table('0,0,0\n0.1,1,2\n0.2,2,4\n')
        self.assertFalse(table['has_header'])
        self.assertEqual(table['samples'], 3)
        self.assertEqual(table['columns'], ['column1', 'column2', 'column3'])
        self.assertEqual(table['rows'][0], {'column1':'0', 'column2':'0', 'column3':'0'})

    def test_explicit_header_mode_handles_numeric_headers(self):
        table = parse_table('0,1,2\n.1,3,4\n', 'present')
        self.assertTrue(table['has_header']); self.assertEqual(table['columns'], ['0','1','2'])
        self.assertEqual(table['samples'], 1)
        table = parse_table('nan,0,0\n.1,3,4\n', 'absent')
        self.assertEqual(table['samples'], 2)
        with self.assertRaises(IdentificationError):
            parse_table('a,b\n1,2', 'guess')

    def test_manual_roles_checked_and_time_inferred(self):
        text, _ = pulse_record(header=False); table = parse_table(text)
        config = {'inputs':['column2','column3'], 'outputs':['column4','column5'], 'time':'column1'}
        channels = resolve_channels(table['columns'], table['rows'], config)
        self.assertAlmostEqual(channels['sample_time'], .02)
        for bad in ({'outputs':['column2']}, {'time':'column2'}, {'inputs':['column2','column2']}, {'outputs':[]}, {'inputs':['missing']}):
            with self.assertRaises(IdentificationError):
                resolve_channels(table['columns'], table['rows'], {**config, **bad})

    def test_simple_arx_aliases(self):
        self.assertEqual(detect_channels(['t','u','y'])['inputs'], ['u'])
        self.assertEqual(detect_channels(['time','input','output'])['outputs'], ['output'])
        self.assertFalse(detect_channels(['input1','u','y'])['valid'])


class OriginalSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text, cls.data = pulse_record()
        cls.result = offline({'csv':cls.text})

    def test_recovers_transfer_and_affine_offsets(self):
        for row, a, b, c in zip(self.result['model']['output_models'], [.8,.6], [[.24,-.09],[.16,.28]], [.6,-.8]):
            self.assertEqual((row['na'],row['nb'],row['nk']), (1,1,1))
            np.testing.assert_allclose(row['a'], [a], atol=1e-8)
            np.testing.assert_allclose(np.asarray(row['b']).ravel(), b, atol=1e-8)
            self.assertAlmostEqual(row['offset'], c, places=8)
        self.assertTrue(all(item['metrics']['rmse'] < 1e-8 for item in self.result['output_results']))

    def test_validation_uses_original_levels_and_time_grid(self):
        result = self.result
        self.assertEqual(result['train_samples'], 720)
        self.assertEqual(result['validation_samples'], 240)
        self.assertAlmostEqual(result['model']['sample_time'], .02)
        series = result['output_results'][0]['series']
        np.testing.assert_allclose([row['measured'] for row in series], self.data[960:,3])
        self.assertNotIn('preprocessing', result)
        self.assertNotIn('Δu', result['mathematical_model']['text'])
        self.assertIn('原始输入输出信号', result['report'])
        json.dumps(result, allow_nan=False)

    def test_headerless_manual_selection_matches_named_data(self):
        text, _ = pulse_record(header=False)
        result = offline({'csv':text, 'model':{'kind':'multichannel',
            'inputs':['column2','column3'], 'outputs':['column4','column5'], 'time':'column1'}})
        np.testing.assert_allclose([p['value'] for p in result['parameters']], [p['value'] for p in self.result['parameters']], atol=1e-10)

    def test_arbitrary_headers_with_explicit_mapping(self):
        names = ['clock','voltage','force','angle','speed']; text, _ = pulse_record(names=names)
        result = offline({'csv':text, 'model':{'kind':'multichannel','inputs':names[1:3], 'outputs':names[3:], 'time':'clock'}})
        self.assertEqual(result['model']['outputs'], ['angle','speed'])
        self.assertTrue(all(item['metrics']['rmse'] < 1e-8 for item in result['output_results']))

    def test_final_holdout_cannot_change_dynamics_or_offset(self):
        lines = self.text.splitlines()
        for k in range(961,len(lines)):
            values = lines[k].split(','); values[3] = str(float(values[3])+400); lines[k] = ','.join(values)
        changed = offline({'csv':'\n'.join(lines)})
        self.assertEqual(changed['parameters'], self.result['parameters'])

    def test_independent_validation_excluded(self):
        result = offline({'csv':self.text, 'validation_csv':self.text})
        self.assertEqual(result['validation_samples'], 1191)
        self.assertTrue(all(item['metrics']['rmse'] < 1e-8 for item in result['output_results']))

    def test_disable_intercept_does_not_invent_offset(self):
        # This reference system has zero offset, matching the configured model.
        rows = self.data.copy(); rows[:,3] -= 3.; rows[:,4] += 2.
        buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['time','input1','input2','output1','output2']); writer.writerows(rows)
        result = offline({'csv':buf.getvalue(), 'model':{'kind':'multichannel','intercept':False}})
        self.assertTrue(all(row['offset']==0 for row in result['model']['output_models']))
        self.assertTrue(all(row['metrics']['rmse']<1e-8 for row in result['output_results']))

    def test_removed_option_rejected_for_old_clients(self):
        # A stale browser/API request must not silently get different semantics.
        for kind in ('multichannel', 'arx', 'regression'):
            with self.assertRaisesRegex(IdentificationError, '差分预处理已移除'):
                offline({'csv':self.text, 'difference':True, 'model':{'kind':kind}})


if __name__ == '__main__':
    unittest.main()
