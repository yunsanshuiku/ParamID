"""Numerical and causal acceptance tests for windowed frequency identification."""

import csv
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

import numpy as np
from scipy.signal import lfilter

from paramid.core import IdentificationError, ModelSpec
from paramid.frequency import frequency_config, welch_response, fit_online_window, response
from paramid.service import offline, create_online_session
from paramid.acquisition import AcquisitionManager


SPEC={'kind':'arx','input':'u','output':'y','time':'time_s','sample_time':.02}
SETTINGS={'segment_length':128,'window_samples':1024,'update_every':256}


def records(n=4096, noise=.15, seed=51):
    """Known stable second-order plant plus independent output measurement noise."""
    rng=np.random.default_rng(seed);u=rng.normal(size=n)
    clean=lfilter([0,.2,.12],[1,-1.2,.36],u)
    y=clean+noise*rng.normal(size=n)
    return [{'time_s':i*.02,'u':float(a),'y':float(b)} for i,(a,b) in enumerate(zip(u,y))],clean


def as_csv(rows):
    buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    return buf.getvalue()


def push_all(session,rows,batch=128):
    result=None
    for k in range(0,len(rows),batch):
        result=session.push(rows[k:k+batch],session.sequence)
    return result


class FrequencyNumerics(unittest.TestCase):
    def test_noise_bias_improvement_and_transfer_units(self):
        rows,_=records(8192,noise=.25)
        spec,result=fit_online_window(rows,ModelSpec.from_dict(SPEC),SETTINGS,structure=(2,2,1))
        theta=result['theta']
        target=np.array([1.2,-.36,.2,.12])
        y=np.array([r['y'] for r in rows]);u=np.array([r['u'] for r in rows]);i=np.arange(2,6144)
        X=np.column_stack([y[i-1],y[i-2],u[i-1],u[i-2],np.ones(len(i))])
        ls=np.linalg.lstsq(X,y[i],rcond=None)[0]
        self.assertLess(np.linalg.norm(theta[:-1]-target),.08)
        self.assertLess(np.linalg.norm(theta[:-1]-target),.3*np.linalg.norm(ls[:-1]-target))
        self.assertLess(max(abs(np.roots(np.r_[1,-theta[:2]]))),1.)
        self.assertEqual(spec.sample_time,.02)
        # Conversion back from normalization preserves physical gain.
        gain=theta[2:4].sum()/(1-theta[:2].sum())
        self.assertAlmostEqual(gain,2.,delta=.12)

    def test_h1_mimo_orientation_and_delay(self):
        rng=np.random.default_rng(4);period=128
        u=rng.normal(size=(period*24,2))
        y=2*u[:,0]-.7*u[:,1]
        spec=welch_response(u,y,.02,frequency_config(SETTINGS))
        np.testing.assert_allclose(spec['h'],np.tile([2.,-.7],(len(spec['h']),1)),atol=1e-12)
        np.testing.assert_allclose(spec['coherence'],1.,atol=1e-12)
        q=np.exp(-2j*np.pi*np.array([1.,3.])*.02)
        h=response(np.array([.5,2.,-.7]),1,1,2,q,2)
        np.testing.assert_allclose(h[:,0],2*q**2/(1-.5*q))

    def test_singular_spectra_and_constant_input_rejected(self):
        rng=np.random.default_rng(1);u=rng.normal(size=2048);y=u+.01*rng.normal(size=len(u))
        for matrix in [np.column_stack([u,u]),np.ones((2048,1))]:
            with self.assertRaises(IdentificationError):
                welch_response(matrix,y,.01,frequency_config(SETTINGS))

    def test_settings_and_short_windows_rejected(self):
        for config in [{'window':'unknown'},{'segment_length':16},{'segment_length':512,'window_samples':1024},
                       {'update_every':9000},{'window_samples':True}]:
            with self.assertRaises(IdentificationError):frequency_config(config)
        with self.assertRaises(IdentificationError):
            welch_response(np.ones((150,1)),np.ones(150),.01,frequency_config(SETTINGS))


class FrequencyWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows,cls.clean=records()
        cls.payload={'csv':as_csv(cls.rows),'model':SPEC,'algorithm':'frequency','frequency':SETTINGS}
        cls.result=offline(cls.payload)

    def test_automatic_order_original_data_and_exports(self):
        result=self.result;model=result['model']['output_models'][0]
        self.assertEqual(result['algorithm'],'frequency')
        self.assertLessEqual(model['na'],3)
        self.assertGreater(result['output_results'][0]['metrics']['fit_percent'],65)
        self.assertIn('G(z)',result['mathematical_model']['text'])
        self.assertNotIn('output_filter',result)
        self.assertIn('y_measured',result['csv_export'])
        json.dumps(result,allow_nan=False)
        spectrum=result['output_results'][0]['frequency']
        self.assertEqual(spectrum['segment_length'],128)
        self.assertGreater(spectrum['used_bins'],12)
        for sample in result['output_results'][0]['series']:
            idx=round(sample['time']/.02)
            self.assertEqual(sample['measured'],self.rows[idx]['y'])

    def test_final_validation_cannot_change_model_or_spectra(self):
        changed=[dict(r) for r in self.rows]
        for r in changed[int(.8*len(changed)):]:r['y']+=500
        result=offline({**self.payload,'csv':as_csv(changed)})
        self.assertEqual(result['parameters'],self.result['parameters'])
        self.assertEqual(result['output_results'][0]['frequency'],self.result['output_results'][0]['frequency'])
        self.assertGreater(result['output_results'][0]['metrics']['rmse'],100)

    def test_multichannel_joint_identification(self):
        rng=np.random.default_rng(43);u=rng.normal(size=(4096,2))
        y1=lfilter([0,.3],[1,-.6],u[:,0])+lfilter([.2],[1,-.6],u[:,1])
        y2=lfilter([0,-.2],[1,-.4],u[:,0])+lfilter([0,.5],[1,-.4],u[:,1])
        rows=[dict(time_s=i*.02,input1=a,input2=b,output1=c+.02*rng.normal(),output2=d+.02*rng.normal())
              for i,((a,b),c,d) in enumerate(zip(u,y1,y2))]
        result=offline({'csv':as_csv(rows),'algorithm':'frequency','frequency':SETTINGS})
        self.assertEqual(len(result['mathematical_model']['transfer_matrix']),2)
        self.assertEqual(len(result['mathematical_model']['transfer_matrix'][0]),2)
        for output in result['output_results']:
            self.assertGreater(output['metrics']['fit_percent'],90)
            self.assertEqual(len(output['frequency']['spectrum'][0]['model_real']),2)

    def test_removed_filter_rejected_and_regression_scope(self):
        with self.assertRaisesRegex(IdentificationError,'已移除'):
            offline({**self.payload,'output_filter':{'enabled':True,'cutoff_hz':5}})
        with self.assertRaisesRegex(IdentificationError,'已移除'):
            create_online_session({'model':SPEC,'output_filter':{'enabled':True}})
        with self.assertRaises(IdentificationError):
            offline({**self.payload,'model':{'kind':'regression'}})


class FrequencyStream(unittest.TestCase):
    def new_session(self):
        return create_online_session({'model':SPEC,'algorithm':'frequency','frequency':SETTINGS})

    def test_causal_updates_transaction_and_batch_invariance(self):
        rows,_=records(1408,noise=.05)
        a,b=self.new_session(),self.new_session()
        push_all(a,rows[:1024])
        self.assertEqual(a.snapshot()['updates'],1)
        self.assertEqual(a.snapshot()['series'],[])
        theta=a.theta.copy(); prior=a.stream
        import copy
        validator=copy.deepcopy(prior);phi,y,t=validator.step(rows[1024])
        expected=float(phi@theta)
        saved=json.dumps(a.snapshot(),allow_nan=False)
        with self.assertRaises(IdentificationError):a.push([rows[1024],rows[1024]],1024)
        self.assertEqual(json.dumps(a.snapshot(),allow_nan=False),saved)
        result=a.push(rows[1024:1025],1024)
        self.assertAlmostEqual(result['series'][0]['prediction'],expected)
        push_all(a,rows[1025:])
        push_all(b,rows,batch=73)
        np.testing.assert_array_equal(a.theta,b.theta)
        self.assertEqual(a.snapshot()['series'],b.snapshot()['series'])
        self.assertEqual(len(a.buffer),1024)
        self.assertEqual(a.snapshot()['updates'],2)
        with self.assertRaises(IdentificationError):a.push(rows[:1],0)

    def test_failed_refresh_keeps_last_model_and_reports_age(self):
        rows,_=records(1024,noise=.02);s=self.new_session();push_all(s,rows)
        tail=[{'time_s':i*.02,'u':0.,'y':0.} for i in range(1024,2304)]
        push_all(s,tail)
        result=s.snapshot()
        self.assertTrue(result['parameters'])
        self.assertTrue(result['frequency']['last_error'])
        self.assertGreater(result['frequency']['model_age_samples'],0)

    def test_file_acquisition_template_and_archive(self):
        rows,_=records(1280,noise=.03)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'stream.csv';path.write_text(as_csv(rows[:3]),encoding='utf-8')
            manager=AcquisitionManager(root/'saved')
            preview=manager.connect({'kind':'file','path':str(path)})
            acquisition=manager.get(preview['id'])
            try:
                deadline=time.monotonic()+5
                while len(acquisition.snapshot()['preview'])<3 and time.monotonic()<deadline:time.sleep(.02)
                channels={'input':'u','output':'y','time':'time_s','algorithm':'frequency','frequency':SETTINGS}
                manager.save_template({'id':preview['id'],'name':'frequency fixture','channels':channels})
                acquisition.start(channels)
                with path.open('a',encoding='utf-8') as f:
                    f.write(as_csv(rows[3:]).split('\n',1)[1])
                deadline=time.monotonic()+20
                while acquisition.snapshot()['fitted']<len(rows)-3 and not acquisition.snapshot()['error'] and time.monotonic()<deadline:time.sleep(.05)
            finally:
                acquisition.stop();acquisition.done.wait(10)
            saved=acquisition.snapshot()
            self.assertEqual(saved['error'],'')
            self.assertEqual(saved['fitted'],len(rows)-3)
            self.assertEqual(saved['result']['algorithm'],'frequency')
            self.assertTrue(saved['result']['parameters'])
            self.assertEqual(manager.templates()[0]['channels']['frequency']['segment_length'],128)
            with zipfile.ZipFile(manager.download(saved['id'])) as z:
                self.assertNotIn('filtered_samples.csv',z.namelist())
                model=json.loads(z.read('model.json'))
                self.assertEqual(model['frequency']['window_samples'],1024)
                self.assertEqual(model['acquisition_channels']['algorithm'],'frequency')


if __name__=='__main__':unittest.main()
