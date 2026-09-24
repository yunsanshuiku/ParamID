"""Known-order simulations and invariants for automatic order selection."""

import copy
import io
import csv
import unittest

import numpy as np

from paramid.core import IdentificationError, ModelSpec, fit_linear
from paramid.data import prepare
from paramid.service import OnlineSession, offline
from paramid.selection import select_arx_order


def system_rows(a=(.82,), b=(.35,), delay=1, count=1400, seed=12, noise=.001):
    """Generate equation-noise ARX data from specified poles/coefficients."""
    rng = np.random.default_rng(seed)
    u = rng.normal(size=count)
    y = np.zeros(count)
    for k in range(max(len(a), delay + len(b) - 1), count):
        y[k] = sum(value * y[k - i - 1] for i, value in enumerate(a))
        y[k] += sum(value * u[k - delay - j] for j, value in enumerate(b)) + .02 + rng.normal(0, noise)
    return [{"time_s": i * .01, "u": float(u[i]), "y": float(y[i]), "weight": 1 + (i % 7)} for i in range(count)]


def as_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    return out.getvalue()


def configuration():
    # No na, nb, nk are supplied by the user in the new default workflow.
    return {"model": {"kind": "arx", "input": "u", "output": "y", "time": "time_s", "sample_time": .01, "intercept": True}}


class AutomaticOrderTests(unittest.TestCase):
    def test_unweighted_wls_preserves_automatic_order_and_candidate_scores(self):
        rows = system_rows(count=600)
        baseline = {**configuration(), "csv": as_csv(rows)}
        ordinary = offline(baseline)
        result = offline({**baseline, "algorithm": "wls", "weight_column": ""})
        self.assertEqual(result["algorithm"], "ls")
        self.assertEqual(result["order_selection"], ordinary["order_selection"])
        self.assertEqual(result["parameters"], ordinary["parameters"])
        # The independently reusable selector also accepts omitted weights.
        selected = select_arx_order(rows, ModelSpec.from_dict(configuration()["model"]), {"algorithm": "wls"})
        self.assertEqual(selected["summary"], ordinary["order_selection"])
        with self.assertRaises(IdentificationError):
            offline({**baseline, "algorithm": "wls", "weight_column": "u"})

    def test_recovers_first_second_third_order_and_delay_without_user_orders(self):
        cases = [((.82,), (.35,), 1), ((1.3, -.42), (.4, .15), 3), ((.7, .2, -.096), (.3, -.1), 4)]
        for a, b, delay in cases:
            with self.subTest(order=len(a), delay=delay):
                result = offline({**configuration(), "csv": as_csv(system_rows(a, b, delay))})
                self.assertEqual((result["model"]["na"], result["model"]["nb"], result["model"]["nk"]), (len(a), len(b), delay))
                self.assertEqual(result["order_selection"]["candidate_count"], 704)
                self.assertGreater(result["selection_samples"], 0)
                self.assertIn("最终验证数据未用于定阶", result["report"])

    def test_direct_feedthrough_delay_zero_is_in_search(self):
        result = offline({**configuration(), "csv": as_csv(system_rows(delay=0))})
        self.assertEqual(result["model"]["nk"], 0)
        self.assertEqual(result["model"]["na"], 1)
        self.assertEqual(result["model"]["nb"], 1)

    def test_qr_reduction_matches_full_design_for_all_offline_solvers(self):
        rows = system_rows(count=800)
        for algorithm in ("ls", "wls", "ridge"):
            with self.subTest(algorithm=algorithm):
                settings = {**configuration(), "algorithm": algorithm, "alpha": .3, "weight_column": "weight"}
                selected = select_arx_order(rows, ModelSpec.from_dict(settings["model"]), settings)
                spec, summary = selected["spec"], selected["summary"]
                data = prepare(rows, spec)
                start, stop = summary["train_start"], summary["train_end"]
                weights = np.asarray([row["weight"] for row in rows[start:stop]]) if algorithm == "wls" else None
                reference = fit_linear(data["x"][start - spec.warmup:stop - spec.warmup],
                    data["y"][start - spec.warmup:stop - spec.warmup], algorithm, weights, .3, True)
                np.testing.assert_allclose(selected["fit"]["theta"], reference["theta"], atol=1e-10)
                np.testing.assert_allclose(selected["fit"]["scales"], reference["scales"], rtol=1e-12)

    def test_final_validation_cannot_change_order_parameters_or_selection_scores(self):
        rows = system_rows()
        original = offline({**configuration(), "csv": as_csv(rows)})
        changed = copy.deepcopy(rows)
        for i, row in enumerate(changed[original["order_selection"]["selection_end"]:]):
            row["u"] = i % 3 * 1000
            row["y"] = -999 + i
        perturbed = offline({**configuration(), "csv": as_csv(changed)})
        self.assertEqual(original["order_selection"], perturbed["order_selection"])
        self.assertEqual(original["parameters"], perturbed["parameters"])
        self.assertGreater(perturbed["metrics"]["rmse"], original["metrics"]["rmse"] * 100)

    def test_independent_validation_is_not_used_to_select_order(self):
        rows = system_rows(count=700)
        first = offline({**configuration(), "csv": as_csv(rows), "validation_csv": as_csv(rows)})
        changed = copy.deepcopy(rows)
        for row in changed: row["y"] += 500
        second = offline({**configuration(), "csv": as_csv(rows), "validation_csv": as_csv(changed)})
        self.assertEqual(first["order_selection"], second["order_selection"])
        self.assertEqual(first["parameters"], second["parameters"])
        self.assertEqual(first["order_selection"]["selection_end"], 700)

    def test_short_or_unexcited_data_do_not_produce_a_fake_recommendation(self):
        for rows in (system_rows(count=100), [{"time_s": i*.01, "u": 0, "y": 0} for i in range(400)]):
            with self.assertRaises(IdentificationError):
                offline({**configuration(), "csv": as_csv(rows)})

    def test_chosen_candidate_obeys_parsimony_rule(self):
        result = offline({**configuration(), "csv": as_csv(system_rows())})
        summary = result["order_selection"]
        eligible = [candidate for candidate in summary["candidates"] if candidate["within_tolerance"]]
        self.assertEqual(summary["selected"]["parameters"], min(candidate["parameters"] for candidate in eligible))
        self.assertLessEqual(summary["selected"]["selection_rmse"], summary["tie_threshold"])
        self.assertEqual(sum(candidate["selected"] for candidate in summary["candidates"]), 1)


class OnlineAutomaticOrderTests(unittest.TestCase):
    def test_initial_selection_then_live_forecasts_without_retrospective_error(self):
        rows = system_rows(count=600)
        session = OnlineSession(configuration())
        self.assertEqual(session.snapshot()["parameters"], [])
        session.push(rows[:200], 0)
        self.assertIsNone(session.estimator)
        selected = session.push(rows[200:400], 200)
        self.assertEqual(selected["order_selection"]["status"], "selected")
        self.assertEqual((session.spec.na, session.spec.nb, session.spec.nk), (1, 1, 1))
        self.assertEqual(selected["series"], [])
        self.assertEqual(selected["calibration_samples"], 400)
        tracked = session.push(rows[400:], 400)
        self.assertEqual(len(tracked["series"]), 200)
        self.assertEqual(tracked["series"][0]["time"], 4.0)
        self.assertEqual(session.spec, ModelSpec.from_dict(selected["model"]))

    def test_failed_batch_cannot_commit_selection_transition(self):
        rows = system_rows(count=500)
        session = OnlineSession(configuration())
        session.push(rows[:200], 0); session.push(rows[200:399], 200)
        invalid = {**rows[400], "y": "bad"}
        with self.assertRaises(IdentificationError): session.push([rows[399], invalid], 399)
        self.assertEqual(session.sequence, 399)
        self.assertIsNone(session.estimator)
        self.assertIsNone(session.order_selection)
        self.assertEqual(len(session.calibration), 399)
        session.push([rows[399]], 399)
        self.assertIsNotNone(session.estimator)

    def test_weak_excitation_keeps_collecting_then_recovers(self):
        rows = [{"time_s": i*.01, "u": 0., "y": 0.} for i in range(400)]
        rng = np.random.default_rng(81)
        for i in range(400, 800):
            previous = rows[-1]
            rows.append({"time_s": i*.01, "u": float(rng.normal()), "y": .8*previous["y"] + .3*previous["u"]})
        session = OnlineSession(configuration())
        session.push(rows[:200], 0)
        collecting = session.push(rows[200:400], 200)
        self.assertEqual(collecting["order_selection"]["status"], "collecting")
        self.assertEqual(collecting["next_sequence"], 400)
        self.assertEqual(collecting["warmup_remaining"], 200)
        self.assertTrue(collecting["order_selection"]["message"])
        session.push(rows[400:600], 400)
        self.assertIsNotNone(session.estimator)
        self.assertEqual(session.order_selection["calibration_samples"], 600)
        self.assertEqual(len(session.calibration), 0)

    def test_long_unexcited_stream_has_bounded_calibration_storage(self):
        session = OnlineSession(configuration())
        for start in range(0, 1600, 200):
            session.push([{"time_s": i*.01, "u": 0, "y": 0} for i in range(start, start+200)], start)
        self.assertIsNone(session.estimator)
        self.assertEqual(len(session.calibration), 1200)
        self.assertEqual(session.sequence, 1600)


if __name__ == "__main__":
    unittest.main()
