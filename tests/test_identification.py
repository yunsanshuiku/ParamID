"""Independent mathematical oracles and workflow invariants for identification."""

import copy
import csv
import io
import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

from paramid.core import IdentificationError, ModelSpec, ModelStream, RLS, fit_linear
from paramid.data import parse_csv, prepare
from paramid.service import OnlineSession, SessionStore, offline

ROOT = Path(__file__).resolve().parents[1]


def config(kind="regression"):
    # Existing tests exercise fixed-model mathematics independently of selection.
    return {**json.loads((ROOT / "examples" / f"{kind}.json").read_text()), "order_mode": "fixed"}


def example(kind="regression"):
    return (ROOT / "examples" / f"{kind}.csv").read_text()


def csv_text(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


class LeastSquaresTests(unittest.TestCase):
    def test_omitted_weights_are_exactly_ordinary_least_squares(self):
        x = np.column_stack((np.linspace(-2, 3, 40), np.ones(40)))
        y = 1.7 * x[:, 0] - .4 + np.sin(np.arange(40)) * .1
        ordinary = fit_linear(x, y)
        optional = fit_linear(x, y, "wls")
        np.testing.assert_array_equal(optional["theta"], ordinary["theta"])
        self.assertEqual(optional["diagnostics"], ordinary["diagnostics"])

    def test_parameter_recovery_with_disparate_units(self):
        rng = np.random.default_rng(7)
        x = rng.normal(size=(500, 3)) * [1e6, 1e-6, 1]
        theta = np.array([2e-6, -3e6, .2])
        result = fit_linear(x, x @ theta)
        np.testing.assert_allclose(result["theta"], theta, rtol=1e-11)
        self.assertLess(result["diagnostics"]["condition"], 2)

    def test_rank_deficiency_is_not_hidden_by_prior_or_regularization(self):
        x = np.column_stack((np.arange(30), np.arange(30)))
        with self.assertRaises(IdentificationError):
            fit_linear(x, np.arange(30))
        result = fit_linear(x, np.arange(30), algorithm="ridge", alpha=1)
        self.assertEqual(result["diagnostics"]["rank"], 1)
        self.assertTrue(result["warnings"])

    def test_wls_downweights_known_noisy_observations(self):
        rng = np.random.default_rng(2)
        x = np.column_stack((rng.normal(size=500), np.ones(500)))
        y = x @ [2, .3]
        y[250:] += rng.normal(0, 10, 250)
        weights = np.r_[np.ones(250), np.ones(250)*1e-5]
        ls = fit_linear(x, y)["theta"]
        wls = fit_linear(x, y, "wls", weights)["theta"]
        self.assertLess(np.linalg.norm(wls - [2,.3]), np.linalg.norm(ls - [2,.3]) / 100)
        with self.assertRaises(IdentificationError):
            fit_linear(x,y,"wls",np.zeros(500))

    def test_ridge_does_not_penalize_intercept(self):
        x = np.column_stack((np.linspace(-1,1,101),np.ones(101)))
        result = fit_linear(x, np.full(101,7.), "ridge", alpha=1e6, intercept=True)
        np.testing.assert_allclose(result["theta"], [0,7], atol=1e-10)


class RLSTests(unittest.TestCase):
    def test_matches_independent_exponentially_weighted_batch_solution(self):
        rng = np.random.default_rng(44)
        x = rng.normal(size=(500,3)) * [1e3, .01, 1]
        theta = np.array([.002, 30, -.4])
        y = x @ theta + rng.normal(0,.01,500)
        scales=np.array([1e3,.01,1]);forgetting=.99;p0=20.
        theta0=np.array([.001, 20., .1])
        estimator=RLS(3, forgetting, p0, scales, theta0)
        for row,value in zip(x,y): estimator.step(row,value)
        weights=forgetting**np.arange(len(y)-1,-1,-1)
        prior=np.sqrt(forgetting**len(y)/p0)
        design=np.vstack((x/scales*np.sqrt(weights[:,None]),np.eye(3)*prior))
        target=np.r_[y*np.sqrt(weights),theta0*scales*prior]
        oracle=np.linalg.lstsq(design,target,rcond=None)[0]/scales
        np.testing.assert_allclose(estimator.theta,oracle,rtol=1e-9,atol=1e-10)
        self.assertGreater(np.linalg.eigvalsh(estimator.p_matrix).min(),0)

    def test_prediction_precedes_correction(self):
        estimator=RLS(1,1,1000)
        result=estimator.step([1],5)
        self.assertEqual(result["prediction"],0)
        self.assertEqual(result["residual"],5)
        self.assertGreater(result["theta"][0],4.9)

    def test_prior_is_not_counted_as_data_excitation(self):
        estimator=RLS(2)
        self.assertEqual(estimator.diagnostics()["rank"],0)
        for _ in range(30): estimator.step([1,1],2)
        self.assertEqual(estimator.diagnostics()["rank"],1)

    def test_nonfinite_and_dimension_failure_do_not_mutate(self):
        estimator=RLS(2)
        for phi,y in [([1,2],float("nan")),([1],2),([float("inf"),1],2)]:
            with self.assertRaises(IdentificationError): estimator.step(phi,y)
        np.testing.assert_array_equal(estimator.theta,[0,0])
        self.assertEqual(estimator.count,0)

    def test_time_varying_parameter_tracking(self):
        rng=np.random.default_rng(9)
        estimator=RLS(1,.98)
        for i in range(1200):
            x=rng.normal();estimator.step([x],(1. if i<600 else 3.)*x)
        self.assertAlmostEqual(estimator.theta[0],3,places=4)

    def test_persistent_weak_excitation_fails_without_silent_overflow(self):
        estimator=RLS(1,.9,1e10)
        with self.assertRaises(IdentificationError):
            for _ in range(100): estimator.step([0],0)
        self.assertTrue(np.isfinite(estimator.p_matrix).all())
        self.assertLessEqual(estimator.p_matrix[0,0],1e14)


class ModelAndWorkflowTests(unittest.TestCase):
    def test_optional_weight_column_uses_ls_and_reports_actual_algorithm(self):
        for kind in ("regression", "arx"):
            baseline = {**config(kind), "csv": example(kind)}
            ordinary = offline(baseline)
            for option in ({}, {"weight_column": ""}, {"weight_column": None}, {"weight_column": "  "}):
                with self.subTest(kind=kind, option=option):
                    result = offline({**baseline, "algorithm": "wls", **option})
                    self.assertEqual(result["parameters"], ordinary["parameters"])
                    self.assertEqual(result["metrics"], ordinary["metrics"])
                    self.assertEqual(result["algorithm"], "ls")
                    self.assertEqual(result["settings"]["requested_algorithm"], "wls")
                    self.assertIsNone(result["settings"]["weight_column"])
                    self.assertIn("未选择权重列", result["report"])

    def test_selected_invalid_weights_do_not_silently_fall_back(self):
        baseline = {**config(), "csv": example(), "algorithm": "wls"}
        for column in ("missing_weight_column", "x1", "time_s"):
            with self.subTest(column=column), self.assertRaises(IdentificationError):
                offline({**baseline, "weight_column": column})

    def test_causal_arx_lags_including_direct_feedthrough(self):
        spec=ModelSpec.from_dict({"kind":"arx","input":"u","output":"y","na":2,"nb":2,"nk":0,"intercept":False})
        stream=ModelStream(spec)
        self.assertIsNone(stream.step({"u":1,"y":10}))
        self.assertIsNone(stream.step({"u":2,"y":20}))
        phi,y,_=stream.step({"u":3,"y":900})
        np.testing.assert_array_equal(phi,[20,10,3,2])
        self.assertEqual(y,900)

    def test_timestamp_rejection_is_transactional(self):
        stream=ModelStream(ModelSpec.from_dict(config("arx")["model"]))
        stream.step({"time_s":0,"u":1,"y":1})
        for t in [0,-1,.03]:
            with self.assertRaises(IdentificationError):stream.step({"time_s":t,"u":2,"y":3})
        self.assertEqual(stream.count,1)
        self.assertEqual(stream.last_time,0)

    def test_csv_missing_and_duplicate_columns_are_rejected(self):
        with self.assertRaises(IdentificationError):parse_csv("a,a\n1,2\n")
        with self.assertRaises(IdentificationError):parse_csv("a,b\n1,2,3\n")
        _,rows=parse_csv("time_s,x1,x2,y\n0,1,,3\n")
        with self.assertRaises(IdentificationError):prepare(rows,ModelSpec.from_dict(config()["model"]))

    def test_offline_known_truth_and_full_exports(self):
        result=offline({**config(),"csv":example()})
        np.testing.assert_allclose([p["value"] for p in result["parameters"]],[2.5,-.7,.3],atol=.004)
        self.assertLess(result["metrics"]["rmse"],.03)
        self.assertEqual(len(result["csv_export"].strip().splitlines()),result["validation_samples"]+1)

    def test_validation_output_cannot_change_fit_or_free_run(self):
        original=offline({**config("arx"),"csv":example("arx")})
        _,rows=parse_csv(example("arx"))
        start=original["train_samples"]+1
        changed=copy.deepcopy(rows)
        for row in changed[start:]:row["y"]=float(row["y"])+100
        perturbed=offline({**config("arx"),"csv":csv_text(changed)})
        self.assertEqual(original["parameters"],perturbed["parameters"])
        np.testing.assert_array_equal([r["simulation"] for r in original["series"]],[r["simulation"] for r in perturbed["series"]])
        self.assertGreater(perturbed["metrics"]["rmse"],10)

    def test_independent_validation_is_excluded_from_training(self):
        result=offline({**config(),"csv":example(),"validation_csv":example()})
        self.assertEqual(result["train_samples"],900)
        self.assertEqual(result["validation_samples"],900)
        self.assertEqual(result["validation_source"],"独立文件")

    def test_online_batch_is_atomic_and_sequence_is_enforced(self):
        session=OnlineSession(config())
        good={"time_s":0,"x1":1,"x2":2,"y":3}
        bad={"time_s":.01,"x1":1,"x2":2,"y":""}
        with self.assertRaises(IdentificationError):session.push([good,bad],0)
        self.assertEqual(session.sequence,0)
        self.assertEqual(session.stream.count,0)
        self.assertEqual(session.estimator.count,0)
        session.push([good],0)
        with self.assertRaises(IdentificationError):session.push([good],0)
        self.assertEqual(session.sequence,1)

    def test_arx_stream_and_batch_recover_same_parameters_without_forgetting(self):
        _,rows=parse_csv(example("arx"))
        spec=ModelSpec.from_dict(config("arx")["model"])
        prepared=prepare(rows,spec)
        expected=fit_linear(prepared["x"],prepared["y"])["theta"]
        session=OnlineSession({**config("arx"),"forgetting":1,"p0":1e8})
        for start in range(0,len(rows),256):session.push(rows[start:start+256],start)
        np.testing.assert_allclose(session.estimator.theta,expected,atol=1e-8)

    def test_online_history_memory_is_bounded(self):
        session=OnlineSession(config())
        for start in range(0,2300,100):
            samples=[{"time_s":i*.01,"x1":np.sin(i),"x2":np.cos(i),"y":2*np.sin(i)} for i in range(start,start+100)]
            session.push(samples,start)
        self.assertEqual(len(session.history),2000)
        self.assertEqual(session.sequence,2300)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        from http.server import ThreadingHTTPServer
        app.STORE=SessionStore()
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=2)

    def request(self,path,data=None,origin=None):
        headers={"Content-Type":"application/json"}
        if origin:headers["Origin"]=origin
        request=Request(self.base+path,None if data is None else json.dumps(data).encode(),headers)
        with urlopen(request,timeout=5) as response:return json.load(response)

    def test_real_online_protocol_create_push_snapshot_delete(self):
        created=self.request("/api/session/create",config())
        key=created["session_id"]
        _,rows=parse_csv(example())
        result=self.request("/api/session/push",{"session_id":key,"sequence":0,"samples":rows[:32]})
        self.assertEqual(result["next_sequence"],32)
        snapshot=self.request("/api/session?id="+key)
        self.assertEqual(snapshot["parameters"],result["parameters"])
        self.request("/api/session/delete",{"session_id":key})
        with self.assertRaises(HTTPError):self.request("/api/session?id="+key)

    def test_cross_origin_is_rejected(self):
        with self.assertRaises(HTTPError) as caught:self.request("/api/session/create",config(),"https://unrelated.example")
        self.assertEqual(caught.exception.code,403)

    def test_bad_request_returns_clear_error(self):
        with self.assertRaises(HTTPError) as caught:self.request("/api/offline",{"csv":"not data"})
        self.assertEqual(caught.exception.code,400)


if __name__=="__main__":
    unittest.main()
