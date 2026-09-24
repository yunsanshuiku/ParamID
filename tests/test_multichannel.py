"""Known coupled systems, honest holdout boundaries and unsafe-header cases."""
import csv
import io
import json
import unittest

import numpy as np
from scipy.signal import lfilter

from paramid.core import IdentificationError
from paramid.data import detect_channels
from paramid.service import offline


def record(n=700, noise=0., correlated=False, time=True, offset=True):
    rng = np.random.default_rng(100)
    u = np.repeat(rng.choice([-1., 1.], (n//5, 2)), 5, axis=0)
    if correlated:
        u[:, 1] = 2*u[:, 0]
    y = np.column_stack([lfilter([0., .25], [1., -.75], u[:, 0])+lfilter([0., -.1], [1., -.75], u[:, 1]),
                         lfilter([0., .12], [1., -.55], u[:, 0])+lfilter([0., .3], [1., -.55], u[:, 1])])
    if offset:
        y += [.3, -.1]
    y += rng.normal(0., noise, y.shape)
    buf = io.StringIO(); writer = csv.writer(buf)
    writer.writerow((["time"] if time else [])+["input2", "output2", "input1", "output1"])
    writer.writerows(([k*.01] if time else [])+[u[k, 1], y[k, 1], u[k, 0], y[k, 0]] for k in range(n))
    return buf.getvalue(), u, y


class ChannelDetectionTests(unittest.TestCase):
    def test_numeric_order_case_and_separators(self):
        result = detect_channels(["output10", "INPUT10", "Input_2", "output_2", "note", "time"])
        self.assertEqual(result["inputs"], ["Input_2", "INPUT10"])
        self.assertEqual(result["outputs"], ["output_2", "output10"])
        self.assertEqual(result["ignored"], ["note"])
        self.assertEqual(result["system_type"], "MIMO")

    def test_duplicate_aliases_rejected(self):
        result = detect_channels(["input1", "INPUT01", "output1"])
        self.assertFalse(result["valid"])
        self.assertIn("重复", result["errors"][0])

    def test_invalid_or_ambiguous_time(self):
        for times in ([0., .01, .03], [0., .01, .01], [0., float("nan"), .02]):
            result = detect_channels(["input1", "output1", "time"], [{"time": t} for t in times])
            self.assertFalse(result["valid"])
        self.assertFalse(detect_channels(["input1", "output1", "time", "t"])["valid"])

    def test_no_time_is_valid_but_not_invented(self):
        result = detect_channels(["input3", "output5"])
        self.assertTrue(result["valid"])
        self.assertIsNone(result["sample_time"])


class MultichannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.csv, cls.u, cls.y = record()
        cls.result = offline({"csv": cls.csv})

    def test_all_four_coupled_paths_and_offsets(self):
        result = self.result
        self.assertEqual(result["model"]["inputs"], ["input1", "input2"])
        self.assertEqual(result["model"]["outputs"], ["output1", "output2"])
        for row, a, b, offset in zip(result["model"]["output_models"], [.75, .55], [[.25, -.1], [.12, .3]], [.075, -.045]):
            self.assertEqual((row["na"], row["nb"], row["nk"]), (1, 1, 1))
            np.testing.assert_allclose(row["a"], [a], atol=1e-8)
            np.testing.assert_allclose(np.asarray(row["b"]).ravel(), b, atol=1e-8)
            self.assertAlmostEqual(row["offset"], offset)
        self.assertTrue(all(item["metrics"]["rmse"] < 1e-9 for item in result["output_results"]))
        self.assertEqual(len(result["mathematical_model"]["transfer_matrix"]), 2)

    def test_state_space_reproduces_joint_impulse_responses(self):
        ss = self.result["mathematical_model"]["state_space"]
        A, B, C, D = (np.asarray(ss[key]) for key in ("A", "B", "C", "D"))
        for j in range(2):
            x = np.zeros(A.shape[0]); values = []
            for k in range(30):
                u = np.zeros(2); u[j] = float(k == 0)
                values.append(C@x+D@u); x = A@x+B@u
            for i, a in enumerate([.75, .55]):
                gain = [[.25, -.1], [.12, .3]][i][j]
                expected = lfilter([0., gain], [1., -a], np.r_[1., np.zeros(29)])
                np.testing.assert_allclose(np.asarray(values)[:, i], expected, atol=1e-9)

    def test_continuous_paths_preserve_dc_gain(self):
        for i, row in enumerate(self.result["mathematical_model"]["transfer_matrix"]):
            for j, path in enumerate(row):
                continuous = path["continuous_model"]
                self.assertEqual(continuous["status"], "available")
                tf = continuous["transfer_function"]
                dc_gain = tf["numerator"][-1]/tf["denominator"][-1]
                self.assertAlmostEqual(dc_gain, [[1., -.4], [.12/.45, .3/.45]][i][j], places=7)

    def test_exports_finite_and_include_all_outputs(self):
        json.dumps(self.result, allow_nan=False)
        self.assertIn("output1_free_run", self.result["csv_export"])
        self.assertIn("output2_free_run", self.result["csv_export"])
        self.assertIn("input2 → output1", self.result["report"])

    def test_final_holdout_cannot_change_parameters(self):
        lines = self.csv.splitlines()
        for k in range(561, len(lines)):
            row = lines[k].split(","); row[2] = str(float(row[2])+100.); row[4] = str(float(row[4])-100.); lines[k] = ",".join(row)
        result = offline({"csv": "\n".join(lines)})
        self.assertEqual(result["parameters"], self.result["parameters"])

    def test_independent_validation_columns_can_be_reordered(self):
        reader = csv.DictReader(io.StringIO(self.csv)); buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["output1", "input1", "time", "output2", "input2"])
        writer.writeheader(); writer.writerows(reader)
        result = offline({"csv": self.csv, "validation_csv": buf.getvalue()})
        self.assertEqual(result["validation_source"], "独立文件")
        self.assertTrue(all(item["metrics"]["rmse"] < 1e-8 for item in result["output_results"]))

    def test_no_time_requires_explicit_sample_time(self):
        text, _, _ = record(time=False)
        with self.assertRaises(IdentificationError):
            offline({"csv": text})
        result = offline({"csv": text, "model": {"kind": "multichannel", "sample_time": .05}})
        self.assertEqual(result["model"]["sample_time"], .05)

    def test_correlated_inputs_rejected(self):
        text, _, _ = record(correlated=True)
        with self.assertRaisesRegex(IdentificationError, "成比例"):
            offline({"csv": text})

    def test_bad_selected_number_or_missing_channel(self):
        with self.assertRaises(IdentificationError):
            offline({"csv": self.csv.replace("-1.0", "nan", 1)})
        with self.assertRaisesRegex(IdentificationError, "全部输入"):
            offline({"csv": self.csv, "validation_csv": "time,input1,output1\n0,0,0\n.01,1,1"})

    def test_noisy_outputs_recover_gains(self):
        text, _, _ = record(noise=.005)
        result = offline({"csv": text})
        for i, row in enumerate(result["model"]["output_models"]):
            gains = np.sum(row["b"], axis=1)/(1.-np.sum(row["a"]))
            np.testing.assert_allclose(gains, [[1., -.4], [.12/.45, .3/.45]][i], atol=.025)
        self.assertTrue(all(row["refined_candidates"] > 0 for row in result["output_selections"]))

    def test_miso_and_intercept_disabled(self):
        text, _, _ = record(offset=False)
        records = list(csv.DictReader(io.StringIO(text))); buf = io.StringIO()
        writer = csv.writer(buf); writer.writerow(["time", "input2", "input1", "output1"])
        writer.writerows([row[name] for name in ["time", "input2", "input1", "output1"]] for row in records)
        result = offline({"csv": buf.getvalue(), "model": {"kind": "multichannel", "intercept": False}})
        self.assertEqual(result["channels"]["system_type"], "MISO")
        self.assertEqual(result["model"]["output_models"][0]["offset"], 0.)

    def test_siso_and_simo_are_inferred_without_manual_mapping(self):
        u = self.u[:, 0]
        ys = [lfilter([0., .2], [1., -.8], u), lfilter([0., .3], [1., -.6], u)]
        for count, kind in [(1, "SISO"), (2, "SIMO")]:
            buf = io.StringIO(); writer = csv.writer(buf)
            writer.writerow(["time", "input7"]+[f"output{j+2}" for j in range(count)])
            writer.writerows([k*.01, u[k]]+[y[k] for y in ys[:count]] for k in range(len(u)))
            result = offline({"csv": buf.getvalue()})
            self.assertEqual(result["channels"]["system_type"], kind)
            self.assertTrue(all(x["metrics"]["rmse"] < 1e-9 for x in result["output_results"]))

    def test_different_units_are_undone_in_exported_coefficients(self):
        buf = io.StringIO(); writer = csv.writer(buf)
        writer.writerow(["time", "input1", "input2", "output1", "output2"])
        writer.writerows(zip(np.arange(len(self.u))*.01, self.u[:, 0]*1000, self.u[:, 1], self.y[:, 0], self.y[:, 1]*.001))
        result = offline({"csv": buf.getvalue()})
        rows = result["model"]["output_models"]
        np.testing.assert_allclose(np.asarray(rows[0]["b"]).ravel(), [.00025, -.1], atol=1e-10)
        np.testing.assert_allclose(np.asarray(rows[1]["b"]).ravel(), [.00000012, .0003], atol=1e-10)
        self.assertAlmostEqual(rows[1]["offset"], -.000045)

    def test_three_inputs_direct_feedthrough_and_different_delays(self):
        rng = np.random.default_rng(19); u = rng.normal(size=(900, 3))
        denominator = [1., -1.1, .28]
        numerators = [[.08], [0., 0., -.12], [0., .15]]
        y = sum(lfilter(b, denominator, u[:, j]) for j, b in enumerate(numerators))
        buf = io.StringIO(); writer = csv.writer(buf)
        writer.writerow(["input1", "input2", "input3", "output1"])
        writer.writerows(np.column_stack((u, y)))
        result = offline({"csv": buf.getvalue(), "model": {"kind": "multichannel", "sample_time": .1}})
        self.assertLess(result["output_results"][0]["metrics"]["rmse"], 1e-8)
        paths = result["mathematical_model"]["transfer_matrix"][0]
        for j, path in enumerate(paths):
            tf = path["transfer_function"]
            actual = lfilter(tf["numerator"], tf["denominator"], np.r_[1., np.zeros(39)])
            expected = lfilter(numerators[j], denominator, np.r_[1., np.zeros(39)])
            np.testing.assert_allclose(actual, expected, atol=1e-8)

    def test_constant_input_is_not_silently_identified(self):
        lines = self.csv.splitlines()
        for k in range(1, len(lines)):
            row = lines[k].split(","); row[1] = "1"; lines[k] = ",".join(row)
        with self.assertRaisesRegex(IdentificationError, "没有有效变化"):
            offline({"csv": "\n".join(lines)})


if __name__ == "__main__":
    unittest.main()
