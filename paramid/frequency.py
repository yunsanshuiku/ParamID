"""Windowed frequency-domain identification for stable, uniformly sampled LTI plants.

Welch averages estimate S_yu and S_uu, then H1 = S_yu S_uu^-1.
This avoids putting noisy past outputs in a time-domain LS regressor. It assumes
accurate inputs and output noise uncorrelated with them; it is NOT an EIV or
closed-loop noise correction. Finite windows cause leakage and resolution bias.
Real rational models are initialized by scaled complex LS (SK iterations), then
refined against the complex H1 response using stable reflection coefficients.
Final order selection uses a separate chronological free-run validation block.
"""

from dataclasses import replace
import numpy as np
from scipy.optimize import least_squares
from scipy.signal import get_window, lfilter, lfiltic

from .core import IdentificationError, integer


def frequency_config(config=None):
    """Bound CPU/memory and validate all settings before starting acquisition."""
    config = {} if config is None else config
    if not isinstance(config, dict):
        raise IdentificationError("frequency 必须是设置对象。")
    window = config.get("window", "hann")
    if window not in ("hann", "hamming", "boxcar"):
        raise IdentificationError("频域窗函数须为 hann、hamming 或 boxcar。")
    segment = integer(config.get("segment_length", 256), "频谱分段点数", 64, 2048)
    size = integer(config.get("window_samples", 2048), "在线滑动窗口点数", 512, 16384)
    hop = integer(config.get("update_every", 512), "模型更新间隔点数", 128, 8192)
    if size < 4 * segment:
        raise IdentificationError("在线窗口至少为频谱分段点数的 4 倍，以保留足够的训练分窗和选模数据。")
    if hop > size:
        raise IdentificationError("模型更新间隔不能大于在线窗口。")
    return {"window": window, "segment_length": segment,
            "window_samples": size, "update_every": hop}


def welch_response(u, y, dt, config):
    """Joint MIMO H1; singular input spectra are excluded, never pseudo-inverted.

    u: (N,m), y: (N,). Fourier arrays use (segment,frequency,channel).
    All segments are training-only, 50% overlapping, with per-segment mean
    removal. DC/Nyquist and input-power bins below 1e-6 of peak are excluded.
    Multiple coherence is a diagnostic, not a probability of correct parameters.
    """
    u = np.asarray(u, dtype=float)
    y = np.asarray(y, dtype=float)
    if u.ndim != 2 or y.shape != (len(u),) or not np.isfinite(u).all() or not np.isfinite(y).all():
        raise IdentificationError("频域输入输出维度不匹配或含非有限数值。")
    length = config["segment_length"]
    starts = range(0, len(u)-length+1, length//2)
    if len(starts) < max(4, u.shape[1]+2):
        raise IdentificationError(f"训练段不足：当前 {length} 点分窗至少需要 {max(4,u.shape[1]+2)} 个重叠窗；请增加数据或减小分段点数。")
    window = get_window(config["window"], length, fftbins=True)
    us = np.stack([u[k:k+length]-u[k:k+length].mean(axis=0) for k in starts])
    ys = np.stack([y[k:k+length]-y[k:k+length].mean() for k in starts])
    U = np.fft.rfft(us*window[None,:,None], axis=1)
    Y = np.fft.rfft(ys*window, axis=1)
    suu = np.einsum('kfi,kfj->fij', U, U.conj())/len(starts)
    syu = np.einsum('kf,kfi->fi', Y, U.conj())/len(starts)
    syy = np.mean(abs(Y)**2, axis=0)
    eigen = np.linalg.eigvalsh(suu)
    power = np.trace(suu, axis1=1, axis2=2).real
    keep = (power > max(float(power.max()), 1e-300)*1e-6) & (eigen[:,0] > np.maximum(eigen[:,-1],1e-300)*1e-8)
    keep[0] = False
    keep[-1] = False
    if np.count_nonzero(keep) < 12:
        raise IdentificationError("有效激励频点不足或多输入频谱无法分离；请增加窗长，使用宽频脉冲/PRBS，且各输入独立激励。")
    h = np.linalg.solve(suu[keep].transpose(0,2,1), syu[keep][...,None])[...,0]
    coherence = np.clip(np.real(np.sum(h*syu[keep].conj(),axis=1))/np.maximum(syy[keep],1e-300),0,1)
    # Input spectral power emphasizes observable bands; coherence is reported,
    # not thresholded on a noisy output estimate (which would select on noise).
    weight = np.sqrt(power[keep]/power[keep].max())
    freq = np.fft.rfftfreq(length, dt)[keep]
    return {"h":h, "q":np.exp(-2j*np.pi*freq*dt), "weight":weight,
            "frequency_hz":freq, "coherence":coherence, "segments":len(starts),
            "bins":len(freq), "resolution_hz":1/(length*dt),
            "input_condition":float(np.max(eigen[keep,-1]/eigen[keep,0])),
            "config":config}


def _real_solve(x, target):
    """Real coefficients, complex residuals; SVD avoids normal-equation squaring."""
    X = np.vstack((x.real, x.imag)); rhs = np.r_[target.real, target.imag]
    scale = np.linalg.norm(X,axis=0)
    if np.any(scale < 1e-14):
        raise IdentificationError("频域候选缺少可辨识参数方向。")
    value, _, rank, singular = np.linalg.lstsq(X/scale, rhs, rcond=1e-10)
    if rank < X.shape[1]:
        raise IdentificationError("频域候选秩不足。")
    return value/scale, float(singular[0]/singular[-1])


def _stable_denominator(a):
    roots = np.roots(np.r_[1.,-a]).astype(complex)
    outside = abs(roots) >= .9999
    roots[outside] = .9998/np.conj(roots[outside])
    return np.poly(roots).real


def _reflection(den):
    """Step down Schur recursion for a real stable monic denominator."""
    current = den.copy(); values = []
    for order in range(len(den)-1,0,-1):
        k = float(np.clip(current[-1],-.99999,.99999)); values.append(k)
        current = np.r_[1.,(current[1:-1]-k*current[-2:0:-1])/(1-k*k)]
    return np.arctanh(values[::-1])


def _denominator(reflection):
    """tanh reflection parameters keep every trial denominator Schur-stable."""
    den = np.array([1.])
    for k in np.tanh(reflection):
        den = np.r_[den,0.] + k*np.r_[0.,den[::-1]]
    return den


def response(theta, na, nb, nk, q, inputs):
    a = 1 - q[:,None]**np.arange(1,na+1) @ theta[:na]
    b = theta[na:na+inputs*nb].reshape(inputs,nb)
    return (q[:,None]**np.arange(nk,nk+nb) @ b.T)/a[:,None]


def _seed(spectrum, na, nb, nk):
    h,q,w = spectrum['h'],spectrum['q'],spectrum['weight']
    n,m = h.shape
    powers_a=q[:,None]**np.arange(1,na+1)
    powers_b=q[:,None]**np.arange(nk,nk+nb)
    X=np.zeros((n*m,na+m*nb),complex)
    for j in range(m):
        X[j*n:(j+1)*n,:na]=h[:,j,None]*powers_a
        X[j*n:(j+1)*n,na+j*nb:na+(j+1)*nb]=powers_b
    rhs=h.T.ravel(); denominator=np.ones(n,complex)
    for _ in range(3):
        weight=np.tile(w/np.maximum(abs(denominator),1e-5),m)
        theta,condition=_real_solve(X*weight[:,None],rhs*weight)
        denominator=1-powers_a@theta[:na]
    den=_stable_denominator(theta[:na]); theta[:na]=-den[1:]
    denominator=np.polynomial.polynomial.polyval(q,den)
    for j in range(m):
        theta[na+j*nb:na+(j+1)*nb],_=_real_solve(powers_b/denominator[:,None]*w[:,None],h[:,j]*w)
    error=(response(theta,na,nb,nk,q,m)-h)*w[:,None]
    return {"na":na,"nb":nb,"nk":nk,"theta":theta,"condition":condition,
            "frequency_loss":float(np.mean(abs(error)**2)),"refined":False,
            "optimizer_converged":False,"parameters":na+m*nb}


def _refine(spectrum, seed):
    na,nb,nk=(seed[k] for k in ('na','nb','nk'))
    h,q,w=spectrum['h'],spectrum['q'],spectrum['weight']
    start=np.r_[_reflection(np.r_[1.,-seed['theta'][:na]]),seed['theta'][na:]]
    def unpack(x):
        return np.r_[-_denominator(x[:na])[1:],x[na:]]
    def residual(x):
        e=((response(unpack(x),na,nb,nk,q,h.shape[1])-h)*w[:,None]).ravel()
        return np.r_[e.real,e.imag]
    opt=least_squares(residual,start,max_nfev=60,x_scale='jac',ftol=1e-7,xtol=1e-7,gtol=1e-7)
    return {**seed,"theta":unpack(opt.x),"refined":True,"optimizer_converged":bool(opt.success),
            "frequency_loss":float(np.mean(residual(opt.x)**2)*2)}


def simulate(u,y,theta,na,nb,nk,intercept,start):
    """Free-run response; only measured history BEFORE start initializes state."""
    force=np.full(len(u),theta[-1] if intercept else 0.)
    for j in range(u.shape[1]):
        force+=lfilter(np.r_[np.zeros(nk),theta[na+j*nb:na+(j+1)*nb]],[1.],u[:,j])
    den=np.r_[1.,-theta[:na]]
    initial=lfiltic([1.],den,y[max(0,start-na):start][::-1])
    with np.errstate(over='ignore',invalid='ignore'):
        pred=lfilter([1.],den,force[start:],zi=initial)[0]
    if not np.isfinite(pred).all() or np.max(abs(pred),initial=0)>1e50:
        raise IdentificationError("频域候选的自由运行响应超出数值范围。")
    return pred


def _offset(u,y,theta,na,nb,nk):
    """Estimate affine equation offset on training samples, after fitting dynamics.

    Using the equation residual avoids assuming the window has reached steady
    state. No output differences or filtering are applied to the recorded data.
    """
    start=max(na,nk+nb-1); idx=np.arange(start,len(y))
    pred=sum(theta[j]*y[idx-j-1] for j in range(na))
    for c in range(u.shape[1]):
        pred+=sum(theta[na+c*nb+j]*u[idx-nk-j,c] for j in range(nb))
    return float(np.mean(y[idx]-pred))


def fit_frequency(u,y,train_end,select_end,intercept,config=None,structure=None,dt=1.):
    """Fit training spectra, select on time holdout; final validation stays unseen.

    At most 462 scaled SK candidates and six nonlinear refinements per output.
    Online subsequent windows reuse the initially selected structure, avoiding
    order jumps and bounding periodic fitting work. Input arrays are normalized
    by the caller using training-only scales.
    """
    config=frequency_config(config)
    spectrum=welch_response(u[:train_end],y[:train_end],dt,config)
    orders=[structure] if structure else [(a,b,k) for a in range(1,7) for b in range(1,8) for k in range(11)]
    candidates=[]
    for na,nb,nk in orders:
        if 2*spectrum['bins']*u.shape[1] <= na+nb*u.shape[1]+4:
            continue
        try:
            item=_seed(spectrum,na,nb,nk)
            candidates.append(item)
        except (IdentificationError,np.linalg.LinAlgError,FloatingPointError):
            continue
    if not candidates:
        raise IdentificationError("频域没有可辨识候选，请增加有效频点或调整窗口。")
    # Refine the best frequency candidate of each denominator order only.
    for order in sorted({x['na'] for x in candidates}):
        item=min((x for x in candidates if x['na']==order),key=lambda x:x['frequency_loss']*(1+.01*x['parameters']))
        try:
            candidates.append(_refine(spectrum,item))
        except (ValueError,np.linalg.LinAlgError,FloatingPointError):
            pass
    valid=[]
    for item in candidates:
        na,nb,nk=(item[k] for k in ('na','nb','nk'))
        theta=item['theta']
        if intercept:
            theta=np.r_[theta,_offset(u[:train_end],y[:train_end],theta,na,nb,nk)]
        try:
            pred=simulate(u[:select_end],y[:select_end],theta,na,nb,nk,intercept,train_end)
            rmse=float(np.sqrt(np.mean((pred-y[train_end:select_end])**2)))
            p=item['parameters']+int(intercept)
            valid.append({**item,'theta':theta,'parameters':p,'selection_rmse':rmse,
                          'selection_score':np.log(max(rmse**2,1e-20))+p*np.log(select_end-train_end)/(select_end-train_end)})
        except IdentificationError:
            continue
    if not valid:
        raise IdentificationError("频域候选在选模段均无法仿真。")
    chosen=min(valid,key=lambda x:(x['selection_score'],x['parameters']))
    shortlist=sorted(valid,key=lambda x:x['selection_score'])[:12]
    chosen={**chosen,'refined_candidates':sum(x['refined'] for x in candidates),
            'candidates':[{k:x[k] for k in ('na','nb','nk','parameters','selection_rmse','refined')} for x in shortlist]}
    fitted=response(chosen['theta'],chosen['na'],chosen['nb'],chosen['nk'],spectrum['q'],u.shape[1])
    # Local output-error Jacobian, not the ARX/normal-equation condition number.
    # The separately estimated affine offset is excluded from spectral rank.
    na,nb,nk=(chosen[k] for k in ('na','nb','nk'))
    q=spectrum['q']; m=u.shape[1]; n=len(q)
    den=1-q[:,None]**np.arange(1,na+1)@chosen['theta'][:na]
    jac=np.zeros((n*m,na+m*nb),complex)
    for j in range(m):
        jac[j*n:(j+1)*n,:na]=fitted[:,j,None]*(q[:,None]**np.arange(1,na+1))/den[:,None]
        jac[j*n:(j+1)*n,na+j*nb:na+(j+1)*nb]=q[:,None]**np.arange(nk,nk+nb)/den[:,None]
    jac*=np.tile(spectrum['weight'],m)[:,None]
    real_jac=np.vstack([jac.real,jac.imag]); scale=np.linalg.norm(real_jac,axis=0)
    singular=np.linalg.svd(real_jac/np.maximum(scale,1e-300),compute_uv=False)
    rank=int(np.count_nonzero(singular>singular[0]*max(real_jac.shape)*np.finfo(float).eps))
    chosen['rank']=rank
    chosen['condition']=float(singular[0]/singular[-1]) if rank==len(singular) else None
    chosen['frequency']={**config,'overlap':.5,'segments':spectrum['segments'],'used_bins':spectrum['bins'],
        'dynamic_parameters':na+m*nb,'jacobian_rank':rank,'jacobian_condition':chosen['condition'],
        'resolution_hz':spectrum['resolution_hz'],'mean_coherence':float(np.mean(spectrum['coherence'])),
        'input_spectral_condition':spectrum['input_condition'], 'candidate_count':len(orders),
        'valid_count':len(candidates), 'estimator':'Welch H1 + stable rational fit',
        'spectrum':[{'frequency_hz':float(f),'coherence':float(c),
                     'measured_real':h.real.tolist(),'measured_imag':h.imag.tolist(),
                     'model_real':g.real.tolist(),'model_imag':g.imag.tolist()}
                    for f,c,h,g in zip(spectrum['frequency_hz'],spectrum['coherence'],spectrum['h'],fitted)]}
    return chosen


def fit_online_window(rows,spec,config,structure=None):
    """Normalize using training prefix only, then restore physical units."""
    u=np.array([r[spec.input] for r in rows],float)[:,None]
    y=np.array([r[spec.output] for r in rows],float)
    end=int(.75*len(rows))
    us=float(np.std(u[:end])); ys=float(np.std(y[:end]))
    if us<=1e-12*max(1.,float(np.max(abs(u)))) or ys<=1e-12*max(1.,float(np.max(abs(y)))):
        raise IdentificationError("窗口内输入或输出没有足够变化，保留上次模型并等待新激励。")
    um=float(u[:end].mean()) if spec.intercept else 0.
    ym=float(y[:end].mean()) if spec.intercept else 0.
    fitted=fit_frequency((u-um)/us,(y-ym)/ys,end,len(rows),spec.intercept,config,structure,spec.sample_time)
    na,nb,nk=(fitted[k] for k in ('na','nb','nk'))
    theta=fitted['theta'].copy(); theta[na:na+nb]*=ys/us
    if spec.intercept:
        theta[-1]=ys*theta[-1]+ym*(1-theta[:na].sum())-um*theta[na:na+nb].sum()
    # Spectra are exported in physical output/input units.
    rescale_spectrum(fitted['frequency'],ys,np.array([us]))
    fitted['theta']=theta; fitted['selection_rmse']*=ys
    return replace(spec,na=na,nb=nb,nk=nk),fitted


def rescale_spectrum(info,ys,us):
    """Undo training normalization on complex frequency responses in-place."""
    for row in info['spectrum']:
        for name in ('measured_real','measured_imag','model_real','model_imag'):
            row[name]=(np.asarray(row[name])*ys/us).tolist()
