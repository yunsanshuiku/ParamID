"""Causal, transactional sliding-window frequency identification sessions."""

from collections import deque
from dataclasses import asdict
import copy
import threading
import time
import numpy as np

from .core import IdentificationError, ModelSpec, ModelStream, integer
from .frequency import frequency_config, fit_online_window
from .representation import mathematical_model


class FrequencySession:
    """Bounded SISO stream; FFT/model fits occur only at observed window boundaries.

    No past fitted output is presented as a live prediction. Every displayed
    sample is predicted using the model available BEFORE that sample is admitted
    to the fitting window. Later windows keep the first selected structure.
    A failed fit keeps the last valid model and reports its age and failure.
    push() rolls back timestamps, buffers, model and history on malformed data.
    """

    def __init__(self, config):
        self.spec=ModelSpec.from_dict(config.get('model',{}))
        if self.spec.kind!='arx':
            raise IdentificationError('在线窗口频域辨识需要单输入单输出动态模型。')
        if config.get('order_mode','auto') not in ('auto','fixed'):
            raise IdentificationError('order_mode 必须为 auto 或 fixed。')
        self.settings=frequency_config(config.get('frequency'))
        self.structure=(self.spec.na,self.spec.nb,self.spec.nk) if config.get('order_mode')=='fixed' else None
        self.stream=ModelStream(self.spec)
        self.buffer=deque(maxlen=self.settings['window_samples'])
        self.history=deque(maxlen=2000)
        self.fit=None; self.theta=None; self.math_model=None
        self.sequence=0; self.updates=0; self.last_attempt=0; self.last_success=0
        self.calibration_samples=0; self.predictions=0; self.fit_error=''
        self.updated=time.monotonic(); self.lock=threading.Lock()

    def push(self,samples,sequence):
        if not isinstance(samples,list) or not 1<=len(samples)<=256:
            raise IdentificationError('每次提交需要 1～256 个样本。')
        sequence=integer(sequence,'sequence',0,2**53-1)
        with self.lock:
            if sequence!=self.sequence:
                raise IdentificationError(f'样本序号不匹配，期望 sequence={self.sequence}。')
            trial=copy.copy(self)
            trial.stream=copy.deepcopy(self.stream)
            # Saved rows and fitted arrays are immutable; refresh replaces them.
            trial.buffer=deque(self.buffer,maxlen=self.buffer.maxlen)
            trial.history=deque(self.history,maxlen=2000)
            additions=[]
            for sample in samples:
                item=trial.stream.step(sample)
                if trial.theta is not None and item is not None:
                    phi,y,t=item
                    prediction=float(phi@trial.theta)
                    if not np.isfinite(prediction):
                        raise IdentificationError('在线预测超出数值范围，请检查单位。')
                    additions.append({'time':t,'measured':y,'prediction':prediction,
                                      'residual':y-prediction,'theta':trial.theta.tolist(),
                                      'model_revision':trial.updates})
                    trial.predictions+=1
                trial.sequence+=1
                trial.buffer.append(dict(trial.stream.rows[-1]))
                if len(trial.buffer)==trial.buffer.maxlen and (trial.last_attempt==0 or trial.sequence-trial.last_attempt>=trial.settings['update_every']):
                    trial._refresh()
            trial.history.extend(additions)
            trial.updated=time.monotonic()
            self.__dict__.update(trial.__dict__)
            return self._snapshot(additions,'append')

    def _refresh(self):
        self.last_attempt=self.sequence
        try:
            spec,fit=fit_online_window(list(self.buffer),self.spec,self.settings,self.structure)
        except (IdentificationError,np.linalg.LinAlgError) as exc:
            self.fit_error=str(exc)
            return
        stream=ModelStream(spec)
        for row in list(self.buffer)[-spec.warmup-1:]:
            stream.step(row)
        stream.count=self.sequence
        if not spec.time:
            stream.last_time=(self.sequence-1)*spec.sample_time
        self.stream,self.spec=stream,spec
        self.fit,self.theta=fit,fit['theta']
        self.math_model=mathematical_model(spec,self.theta)
        self.structure=(spec.na,spec.nb,spec.nk)
        self.updates+=1;self.last_success=self.sequence;self.fit_error=''
        if not self.calibration_samples:
            self.calibration_samples=self.sequence

    def _snapshot(self,series=None,history_mode='replace'):
        pending=self.fit is None
        target=self.settings['window_samples'] if not self.last_attempt else self.last_attempt+self.settings['update_every']
        frequency={**(self.fit['frequency'] if self.fit else self.settings),
                   'buffered_samples':len(self.buffer),'next_update_at':target,
                   'last_success_at':self.last_success,'model_age_samples':self.sequence-self.last_success if self.last_success else None,
                   'last_attempt_at':self.last_attempt,'last_error':self.fit_error,'model_updates':self.updates}
        warning=['窗口频域 H1 适用于稳定线性系统、准确输入及与输入不相关的输出噪声；不保证消除输入噪声或闭环偏差。',
                 '分窗存在频率分辨率与泄漏误差；相干度高不等于参数一定准确。',
                 '在线曲线是前一模型的一步预测；窗口选模误差不是独立实验验证。']
        if pending:
            warning.append(self.fit_error or f'等待收满 {target} 点后进行第一次分窗频域拟合。')
        elif self.fit_error:
            warning.append('本窗口拟合失败，保留上次模型：'+self.fit_error)
        if not pending and self.fit['frequency']['mean_coherence']<.6:
            warning.append('平均相干度偏低，请检查噪声、激励频段、窗长与线性假设。')
        if pending:
            selection={'mode':'auto','status':'collecting','received':self.sequence,'target_samples':target,'message':warning[-1]}
        else:
            selected={k:self.fit[k] for k in ('na','nb','nk','parameters','selection_rmse')}
            selection={'mode':'auto','status':'selected','selected':selected,
                       'candidate_count':self.fit['frequency']['candidate_count'],'valid_count':self.fit['frequency']['valid_count'],
                       'calibration_samples':self.calibration_samples,'candidates':[],
                       'criterion':'窗口训练频谱拟合，末尾 25% 自由运行误差与复杂度选模；首次定阶后固定结构。'}
        p=0 if pending else self.spec.na+self.spec.nb
        return {'version':'0.7.0','mode':'online','algorithm':'frequency','model':asdict(self.spec),
                'next_sequence':self.sequence,'updates':self.updates,'prediction_samples':self.predictions,
                'warmup_remaining':max(0,target-self.sequence) if pending else 0,
                'calibration_samples':self.calibration_samples,'order_selection':selection,'frequency':frequency,
                'parameters':[] if pending else [{'name':n,'value':float(v)} for n,v in zip(self.spec.names,self.theta)],
                'mathematical_model':self.math_model,'scales':[],
                'diagnostics':{'rank':0 if pending else self.fit['rank'],'parameters':p,'condition':None if pending else self.fit['condition'],'updates':self.updates},
                'series':list(self.history) if series is None else series,'history_mode':history_mode,'warnings':warning}

    def snapshot(self):
        with self.lock:
            self.updated=time.monotonic()
            return self._snapshot()
