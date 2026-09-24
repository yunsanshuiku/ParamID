"""Independent recurrence/filter checks for exported mathematical representations."""

import json
from pathlib import Path
import unittest

import numpy as np
from scipy.signal import lfilter

from paramid.core import IdentificationError, ModelSpec
from paramid.representation import mathematical_model
from paramid.service import OnlineSession, offline


ROOT = Path(__file__).resolve().parents[1]


class RepresentationTests(unittest.TestCase):
    def spec(self, na=1, nb=1, nk=1, intercept=True):
        return ModelSpec.from_dict({'kind': 'arx', 'input': 'voltage', 'output': 'speed',
                                    'sample_time': .02, 'na': na, 'nb': nb, 'nk': nk, 'intercept': intercept})

    def test_denominator_sign_delay_and_offset_are_separate(self):
        model = mathematical_model(self.spec(), [.82, .35, .04])
        tf = model['transfer_function']
        self.assertEqual(tf['numerator'], [0., .35])
        self.assertEqual(tf['denominator'], [1., -.82])
        self.assertEqual(tf['delay_seconds'], .02)
        self.assertEqual(model['equation_text'], 'y[k] = 0.82 y[k-1] + 0.35 u[k-1] + 0.04')
        self.assertEqual(tf['text'], 'G(z) = (0.35 z^(-1)) / (1 - 0.82 z^(-1))')
        self.assertEqual(model['state_space']['g'], [[.04], [0.]])
        self.assertEqual(model['state_space']['h'], [[.04]])

    def test_transfer_and_state_realization_match_independent_filter(self):
        rng = np.random.default_rng(46)
        # Covers direct feedthrough, numerator longer than denominator, and long delay.
        for na, nb, nk in [(1, 1, 0), (3, 2, 0), (1, 4, 1), (2, 3, 4), (8, 8, 50)]:
            with self.subTest(na=na, nb=nb, nk=nk):
                coefficients = np.r_[rng.uniform(-.04, .04, na), rng.normal(size=nb)]
                model = mathematical_model(self.spec(na, nb, nk, False), coefficients)
                ss, tf = model['state_space'], model['transfer_function']
                A, B, C, D = [np.asarray(ss[name]) for name in ('A', 'B', 'C', 'D')]
                x, predicted = np.zeros((ss['dimension'], 1)), []
                inputs = rng.normal(size=160)
                for u in inputs:
                    predicted.append((C @ x + D * u)[0, 0])
                    x = A @ x + B * u
                expected = lfilter(np.r_[np.zeros(nk), coefficients[na:]], np.r_[1., -coefficients[:na]], inputs)
                np.testing.assert_allclose(predicted, expected, atol=2e-14)
                self.assertEqual(tf['numerator'], [0.] * nk + coefficients[na:].tolist())
                self.assertEqual(ss['dimension'], na + max(0, nk + nb - 1))

    def test_affine_state_equations_match_recurrence_with_nonzero_initial_history(self):
        rng = np.random.default_rng(7)
        for na, nb, nk in [(1, 1, 0), (2, 3, 0), (3, 2, 3)]:
            with self.subTest(na=na, nb=nb, nk=nk):
                a, b, c = rng.uniform(-.1, .1, na), rng.normal(size=nb), -.37
                model = mathematical_model(self.spec(na, nb, nk), np.r_[a, b, c])
                ss = model['state_space']
                A, B, C, D, g, h = [np.asarray(ss[n]) for n in ('A', 'B', 'C', 'D', 'g', 'h')]
                y_history = list(rng.normal(size=na))
                u_history = list(rng.normal(size=max(0, nk + nb - 1)))
                x = np.asarray(y_history + u_history).reshape(-1, 1)
                for u in rng.normal(size=100):
                    expected = a @ y_history + c + sum(coefficient * (u if nk+j == 0 else u_history[nk+j-1]) for j, coefficient in enumerate(b))
                    actual = (C @ x + D * u + h)[0, 0]
                    self.assertAlmostEqual(actual, expected, places=12)
                    x = A @ x + B * u + g
                    y_history = [expected] + y_history[:-1]
                    if u_history:
                        u_history = [u] + u_history[:-1]
                    np.testing.assert_allclose(x[:, 0], y_history + u_history, atol=1e-12)

    def test_regression_maps_arbitrary_features_and_retains_small_coefficients(self):
        spec = ModelSpec.from_dict({'kind': 'regression', 'output': 'force', 'features': ['acceleration', 'sin(position)']})
        model = mathematical_model(spec, [2.5, -1e-12, -.3])
        self.assertEqual(model['equation_text'], 'y[k] = 2.5 phi_1[k] - 1e-12 phi_2[k] - 0.3')
        self.assertIn(r'10^{-12}', model['equation_latex'])
        self.assertEqual(model['signals'][2]['column'], 'sin(position)')
        self.assertIsNone(model['transfer_function'])
        self.assertIsNone(model['state_space'])

    def test_waiting_and_invalid_parameters(self):
        self.assertIsNone(mathematical_model(self.spec(), None))
        for theta in ([1], [1, 2, float('nan')]):
            with self.assertRaises(IdentificationError):
                mathematical_model(self.spec(), theta)
        zero = mathematical_model(self.spec(intercept=False), [0., -0.])
        self.assertEqual(zero['equation_text'], 'y[k] = 0')
        self.assertEqual(zero['transfer_function']['text'], 'G(z) = (0) / (1)')

    def test_offline_json_and_report_share_exact_fitted_coefficients(self):
        payload = json.loads((ROOT / 'examples/arx.json').read_text())
        payload.update(csv=(ROOT / 'examples/arx.csv').read_text(), order_mode='fixed')
        result = offline(payload)
        model = result['mathematical_model']
        self.assertEqual(model['transfer_function']['denominator'][1], -result['parameters'][0]['value'])
        self.assertIn(model['equation_text'], result['report'])
        self.assertIn(model['transfer_function']['text'], result['report'])
        self.assertIn('A =', result['report'])
        json.dumps(result, allow_nan=False)

    def test_online_formula_appears_after_updates_and_tracks_latest_theta(self):
        session = OnlineSession({'model': {'kind': 'regression', 'features': ['u'], 'output': 'y'}})
        self.assertIsNone(session.snapshot()['mathematical_model'])
        first = session.push([{'u': 1., 'y': 2.}], 0)
        self.assertEqual(first['mathematical_model']['equation_terms'][0]['coefficient'], first['parameters'][0]['value'])
        second = session.push([{'u': 2., 'y': 5.}], 1)
        self.assertNotEqual(first['mathematical_model']['equation_text'], second['mathematical_model']['equation_text'])
        self.assertEqual(second['mathematical_model']['equation_terms'][0]['coefficient'], second['parameters'][0]['value'])
        pending = OnlineSession({'model': {'kind': 'arx', 'input': 'u', 'output': 'y'}})
        self.assertIsNone(pending.push([{'u': 1., 'y': 1.}], 0)['mathematical_model'])


if __name__ == '__main__':
    unittest.main()
