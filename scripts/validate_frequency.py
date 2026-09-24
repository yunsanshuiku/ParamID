"""Reproducible noisy-LTI benchmark and CSV example; never touches acquisition.

The benchmark fixes the known structure to isolate coefficient noise bias;
automatic structure selection and causal streaming are covered by unit/UI tests.
Run with python scripts/validate_frequency.py from the repository root.
"""
from pathlib import Path
import csv
import io
import json
import sys
import time

import numpy as np
from scipy.signal import lfilter

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paramid.core import ModelSpec
from paramid.frequency import fit_online_window


def main():
    spec=ModelSpec(kind='arx',input='u',output='y',time='time_s',sample_time=.02)
    truth=np.array([1.2,-.36,.2,.12])
    findings=[]
    for noise in [0.,.05,.15,.3]:
        cases=[]
        for seed in [7,19,51]:
            rng=np.random.default_rng(seed);u=rng.normal(size=4096)
            clean=lfilter([0,.2,.12],[1,-1.2,.36],u)
            y=clean+noise*rng.normal(size=len(u))
            rows=[dict(time_s=i*.02,u=float(a),y=float(b)) for i,(a,b) in enumerate(zip(u,y))]
            before=time.perf_counter()
            _,fit=fit_online_window(rows,spec,{},structure=(2,2,1))
            elapsed=time.perf_counter()-before
            i=np.arange(2,3072)
            X=np.column_stack([y[i-1],y[i-2],u[i-1],u[i-2],np.ones(len(i))])
            ls=np.linalg.lstsq(X,y[i],rcond=None)[0]
            cases.append({'seed':seed,'ls_coefficient_relative_error':float(np.linalg.norm(ls[:-1]-truth)/np.linalg.norm(truth)),
                          'frequency_coefficient_relative_error':float(np.linalg.norm(fit['theta'][:-1]-truth)/np.linalg.norm(truth)),
                          'frequency_seconds':elapsed})
            if noise==.15 and seed==51:
                buf=io.StringIO();writer=csv.DictWriter(buf,fieldnames=['time_s','u','y']);writer.writeheader();writer.writerows(rows)
                (ROOT/'examples/frequency.csv').write_text(buf.getvalue(),encoding='utf-8')
                (ROOT/'examples/frequency.json').write_text(json.dumps({'model':{'kind':'arx','input':'u','output':'y','time':'time_s','sample_time':.02},
                    'algorithm':'frequency','frequency':{'window':'hann','segment_length':256,'window_samples':2048,'update_every':512},
                    'truth':truth.tolist()},indent=2),encoding='utf-8')
        findings.append({'output_noise_std':noise,'runs':cases,
                         'median_ls_error':float(np.median([c['ls_coefficient_relative_error'] for c in cases])),
                         'median_frequency_error':float(np.median([c['frequency_coefficient_relative_error'] for c in cases]))})
    out=ROOT/'outputs/frequency_validation';out.mkdir(exist_ok=True,parents=True)
    (out/'benchmark.json').write_text(json.dumps(findings,indent=2),encoding='utf-8')
    print(json.dumps(findings,indent=2))


if __name__=='__main__':main()
