"""ZOH conversion checks against known plants and independent resampling.

Tests distinguish integer transport delay from the intrinsic sample lag of a
strictly proper ZOH plant, and verify the constant forcing for an integrator.
"""

import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import cont2discrete, lfilter

from paramid.continuous import _convert, continuous_model
from paramid.core import ModelSpec
from paramid.representation import mathematical_model


def from_known(numerator, denominator, dt=.05, extra_delay=0, offset=0.):
    b, a, _ = cont2discrete((numerator, denominator), dt, method='zoh')
    b = b[0]
    active = np.flatnonzero(b != 0)
    first, last = int(active[0]), int(active[-1])
    spec = ModelSpec.from_dict({'kind': 'arx', 'input': 'u', 'output': 'y', 'sample_time': dt,
                               'na': len(a)-1, 'nb': last-first+1, 'nk': first+extra_delay})
    theta = np.r_[-a[1:], b[first:last+1], offset]
    return spec, theta, a, np.r_[np.zeros(extra_delay), b]


class ContinuousTests(unittest.TestCase):
    def assert_available(self, result):
        self.assertEqual(result['status'], 'available', result.get('reason'))
        self.assertLess(result['roundtrip_relative_error'], 1e-8)

    def test_first_order_recovers_known_plant_without_false_dead_time(self):
        spec, theta, _, _ = from_known([6.], [1., 3.])
        result = continuous_model(spec, theta)
        self.assert_available(result)
        self.assertEqual(spec.nk, 1)
        self.assertEqual(result['delay_samples'], 0)
        np.testing.assert_allclose(result['transfer_function']['numerator'], [6.], atol=1e-12)
        np.testing.assert_allclose(result['transfer_function']['denominator'], [1., 3.], atol=1e-12)

    def test_recover_two_and_three_order_systems_and_direct_feedthrough(self):
        for num, den in [([4.], [1., 1.2, 4.]), ([2., 3.], [1., 5., 6.]),
                         ([.4, 3.], [1., 2.]), ([3.], [1., 6., 11., 6.])]:
            with self.subTest(num=num, den=den):
                spec, theta, _, _ = from_known(num, den, dt=.04)
                result = continuous_model(spec, theta)
                self.assert_available(result)
                n = len(result['transfer_function']['numerator'])
                padded = np.pad(num, (n-len(num), 0))
                np.testing.assert_allclose(result['transfer_function']['numerator'], padded, rtol=1e-7, atol=2e-8)
                np.testing.assert_allclose(result['transfer_function']['denominator'], den, rtol=1e-7, atol=2e-8)
                self.assertEqual(result['delay_samples'], 0)

    def test_preserve_extra_transport_delay_without_adding_the_zoh_sample_lag(self):
        spec, theta, _, _ = from_known([2.], [1., 5.], extra_delay=3)
        result = continuous_model(spec, theta)
        self.assert_available(result)
        self.assertEqual(spec.nk, 4)
        self.assertEqual(result['delay_samples'], 3)
        self.assertAlmostEqual(result['delay_seconds'], .15)
        text = mathematical_model(spec, theta)['continuous_model']['text']
        self.assertIn('exp(-0.15 s)', text)

    def test_resampled_affine_model_matches_arx_with_offset_and_integrators(self):
        rng = np.random.default_rng(21)
        for num, den, delay in [([2.], [1., 0.], 0), ([4.], [1., 1.2, 4.], 2), ([.4, 3.], [1., 2.], 0)]:
            with self.subTest(den=den, delay=delay):
                spec, theta, den_d, num_d = from_known(num, den, extra_delay=delay, offset=.07)
                result = continuous_model(spec, theta)
                self.assert_available(result)
                ss = result['state_space']
                Ac, Bc, Cc, Dc, gc, h = [np.asarray(ss[k]) for k in ('A', 'B', 'C', 'D', 'g', 'h')]
                Ad, forcing_d, _, _, _ = cont2discrete((Ac, np.column_stack((Bc, gc)), Cc, np.zeros((1, 2))), spec.sample_time)
                x, actual = np.zeros((ss['dimension'], 1)), []
                u = rng.normal(size=160)
                lag = result['delay_samples']
                for k in range(len(u)):
                    held_input = u[k-lag] if k >= lag else 0.
                    actual.append(float((Cc @ x + Dc * held_input + h)[0, 0]))
                    x = Ad @ x + forcing_d[:, :1] * held_input + forcing_d[:, 1:2]
                expected = lfilter(num_d, den_d, u) + lfilter([.07], den_d, np.ones(len(u)))
                np.testing.assert_allclose(actual, expected, rtol=3e-9, atol=3e-9)

    def test_negative_and_near_zero_poles_are_rejected_without_complex_exports(self):
        spec = ModelSpec.from_dict({'kind': 'arx', 'input': 'u', 'output': 'y'})
        negative = mathematical_model(spec, [-.5, .3, .01])
        self.assertEqual(negative['continuous_model']['status'], 'unavailable')
        self.assertIn('复数', negative['continuous_model']['reason'])
        self.assertIsNotNone(negative['transfer_function'])
        tiny = continuous_model(spec, [1e-15, .3, 0.])
        self.assertEqual(tiny['status'], 'unavailable')
        self.assertIn('接近零', tiny['reason'])

    def test_unstable_positive_pole_is_not_silently_stabilized(self):
        spec, theta, _, _ = from_known([1.], [1., -.3])
        result = continuous_model(spec, theta)
        self.assert_available(result)
        self.assertAlmostEqual(result['poles'][0]['real'], .3, places=10)

    def test_static_gain_with_delay_and_unsupported_fir(self):
        spec = ModelSpec.from_dict({'kind': 'arx', 'input': 'u', 'output': 'y', 'sample_time': .1, 'nk': 4})
        result = mathematical_model(spec, [0., 2., .3])['continuous_model']
        self.assert_available(result)
        self.assertEqual(result['state_space']['dimension'], 0)
        self.assertEqual(result['transfer_function']['numerator'], [2.])
        self.assertAlmostEqual(result['delay_seconds'], .4)
        self.assertEqual(result['state_space']['h'], [[.3]])
        spec = ModelSpec.from_dict({'kind': 'arx', 'input': 'u', 'output': 'y', 'nb': 3, 'nk': 0})
        result = continuous_model(spec, [.5, .2, .3, .1, 0.])
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('整体纯延迟', result['reason'])

    def test_conversion_failure_preserves_discrete_model_and_cache_is_independent(self):
        _convert.cache_clear()
        spec = ModelSpec.from_dict({'kind': 'arx', 'input': 'u', 'output': 'y'})
        with patch('paramid.continuous.logm', side_effect=np.linalg.LinAlgError('controlled failure')):
            model = mathematical_model(spec, [.71, .31, .02])
        self.assertEqual(model['continuous_model']['status'], 'unavailable')
        self.assertIn('0.71 y[k-1]', model['equation_text'])
        first = continuous_model(spec, [.82, .35, .04])
        first['state_space']['A'][0][0] = 999
        self.assertLess(continuous_model(spec, [.82, .35, .04])['state_space']['A'][0][0], 0)

    def test_regression_does_not_invent_continuous_dynamics(self):
        spec = ModelSpec.from_dict({'kind': 'regression', 'features': ['a', 'v'], 'output': 'force'})
        result = mathematical_model(spec, [2., .1, .02])['continuous_model']
        self.assertEqual(result['status'], 'not_applicable')
        self.assertIn('未定义动态状态', result['reason'])


if __name__ == '__main__':
    unittest.main()
