'use strict';
// Native MathML keeps formulas readable offline; all column names are text nodes.
// The Python representation is the sole source of coefficients and sign conventions.
const MATH_NS='http://www.w3.org/1998/Math/MathML';
const continuousSection=document.createElement('section');
continuousSection.id='continuousModelBlock';continuousSection.className='continuous-model';
continuousSection.setAttribute('aria-labelledby','continuousTitle');
continuousSection.innerHTML=`<div class="card-heading"><div><h2 id="continuousTitle">连续时间等效模型</h2><p>按零阶保持假设转换，匹配采样时刻的模型响应</p></div><span class="tag" id="continuousStatus">等待转换</span></div>
<p id="continuousUnavailable" class="continuous-reason"></p><div id="continuousBody" hidden><div class="math-label">连续传递函数 G_c(s)</div><div id="mathContinuousTransfer" class="math-equation"></div><p id="continuousSampling" class="math-sampling"></p>
<details id="continuousStateSpace" class="math-state-space"><summary>连续状态方程与矩阵 <span id="continuousDimension"></span></summary><pre id="continuousEquations"></pre><p id="continuousDefinition" class="muted"></p><div id="continuousMatrices" class="matrix-grid"></div></details></div>
<details id="continuousAssumptionsDetails" class="continuous-assumptions"><summary>转换前提与延迟说明</summary><div id="continuousAssumptions" class="math-notes"></div></details>`;
$('mathStateSpace').before(continuousSection);
function mathNode(tag,...children){
  const element=document.createElementNS(MATH_NS,tag);
  children.forEach(child=>element.append(typeof child==='string'?document.createTextNode(child):child));
  return element;
}
function mathNumber(value){
  const text=fmt(value,8);
  if(!/[eE]/.test(text))return mathNode('mn',text);
  const [mantissa,exponent]=text.toLowerCase().split('e');
  return mathNode('mrow',mathNode('mn',mantissa),mathNode('mo','×'),mathNode('msup',mathNode('mn','10'),mathNode('mn',String(Number(exponent)))));
}
function mathSum(terms){
  const row=mathNode('mrow');let count=0;
  for(const term of terms){
    const value=term.coefficient;if(value===0)continue;
    if(value<0||count)row.append(mathNode('mo',value<0?'−':'+'));
    row.append(mathNumber(Math.abs(value)));
    if(term.symbol==='z'&&term.lag){
      row.append(mathNode('msup',mathNode('mi','z'),mathNode('mrow',mathNode('mo','−'),mathNode('mn',String(term.lag)))));
    }else if(term.symbol==='s'){
      if(term.power===1)row.append(mathNode('mi','s'));
      else if(term.power)row.append(mathNode('msup',mathNode('mi','s'),mathNode('mn',String(term.power))));
    }else if(term.symbol&&term.symbol!=='z'){
      row.append(mathNode('mo','·'),term.symbol==='phi'?mathNode('msub',mathNode('mi','φ'),mathNode('mn',String(term.feature))):mathNode('mi',term.symbol));
      row.append(mathNode('mo','['),mathNode('mi','k'));
      if(term.lag)row.append(mathNode('mo','−'),mathNode('mn',String(term.lag)));
      row.append(mathNode('mo',']'));
    }
    count++;
  }
  if(!count)row.append(mathNode('mn','0'));
  return row;
}
function mountMath(id,content,label){
  const math=mathNode('math',content);math.setAttribute('display','block');math.setAttribute('aria-label',label);$(id).replaceChildren(math);
}
function modelSignals(model,channels){
  $('mathSignals').replaceChildren();
  for(const signal of model.signals){
    const line=document.createElement('span');let label=signal.column;
    if(channels&&['input','output'].includes(signal.role)){
      const role=signal.role;
      label=`${channels[role+'_name']||channels[role]}${channels[role+'_unit']?' / '+channels[role+'_unit']:''}（源列 ${channels[role]} × ${fmt(channels[role+'_scale'],8)}）`;
    }
    line.textContent=`${signal.symbol.replace(/phi_(\d+)/g,'φ$1')}：${label}`;$('mathSignals').append(line);
  }
}
function renderContinuousModel(model){
  const continuous=model.continuous_model,available=continuous?.status==='available';
  $('continuousBody').hidden=!available;$('continuousUnavailable').hidden=available;
  $('continuousUnavailable').textContent=continuous?.reason||'当前结果尚未包含连续转换，请重新运行辨识。';
  $('continuousStatus').textContent=available?'ZOH 等效模型':continuous?.status==='not_applicable'?'不适用':'暂不可转换';
  $('continuousAssumptions').replaceChildren();
  for(const message of continuous?.assumptions||[]){const p=document.createElement('p');p.textContent=message;$('continuousAssumptions').append(p);}
  $('continuousAssumptionsDetails').hidden=!continuous?.assumptions?.length;
  if(!available)return;
  const tf=continuous.transfer_function,ss=continuous.state_space;
  const fraction=mathNode('mfrac',mathSum(tf.numerator_terms),mathSum(tf.denominator_terms));
  const row=mathNode('mrow',mathNode('mrow',mathNode('msub',mathNode('mi','G'),mathNode('mi','c')),mathNode('mo','('),mathNode('mi','s'),mathNode('mo',')')),mathNode('mo','='),fraction);
  if(continuous.delay_seconds){
    row.append(mathNode('mo','·'),mathNode('msup',mathNode('mi','e'),mathNode('mrow',mathNode('mo','−'),mathNumber(continuous.delay_seconds),mathNode('mi','s'))));
  }
  mountMath('mathContinuousTransfer',row,tf.text);
  $('continuousSampling').textContent=`连续纯延迟 τ = ${fmt(continuous.delay_seconds,8)} s　·　采样周期 Ts = ${fmt(continuous.sample_time,8)} s　·　重新离散化一致性误差 ${fmt(continuous.roundtrip_relative_error,3)}`;
  $('continuousDimension').textContent=`${ss.dimension} 个动态状态`;
  $('continuousEquations').textContent=ss.equations.join('\n');$('continuousDefinition').textContent=ss.state_definition;
  $('continuousMatrices').replaceChildren();
  for(const name of ['A','B','C','D','g','h']){
    if(!ss.dimension&&!['D','h'].includes(name))continue;
    const item=document.createElement('div'),label=document.createElement('span'),pre=document.createElement('pre');
    label.textContent=`${name}_c (${ss[name].length} × ${ss[name][0].length})`;
    pre.textContent='['+ss[name].map(row=>'['+row.map(value=>fmt(value,8)).join(', ')+']').join('\n ')+']';
    item.append(label,pre);$('continuousMatrices').append(item);
  }
}
function renderMathematicalModel(result){
  if($('transferMatrix'))$('transferMatrix').hidden=true;
  $('continuousModelBlock').hidden=false;
  const model=result.mathematical_model,online=result.mode==='online';
  $('mathModelBody').hidden=!model;$('mathModelPending').hidden=!!model;$('exportMathModel').disabled=!model;
  $('mathModelPending').textContent=result.order_selection?.status==='collecting'?'正在收集初始样本，完成自动定阶后显示具体数学模型。':'等待有效样本更新参数后显示数学模型。';
  $('mathModelCaption').textContent=online?'当前参数快照 · 在线更新时同步变化，停止后保留最后一次结果':'由本次辨识参数生成 · 数值系数可直接查看和导出';
  $('mathModelBadge').textContent=result.model.kind==='arx'?'离散动态系统':'线性参数模型';
  if(!model)return;
  renderContinuousModel(model);
  if(model.continuous_model?.status==='available')$('mathModelBadge').textContent='离散 / 连续模型';
  $('mathEquationLabel').textContent=model.kind==='arx'?'差分方程':'回归表达式';
  mountMath('mathEquation',mathNode('mrow',mathNode('mi','y'),mathNode('mo','['),mathNode('mi','k'),mathNode('mo',']'),mathNode('mo','='),mathSum(model.equation_terms)),model.equation_text);
  modelSignals(model,result.acquisition_channels);
  const tf=model.transfer_function,ss=model.state_space;
  $('mathTransferBlock').hidden=!tf;$('mathStateSpace').hidden=!ss;
  if(tf){
    mountMath('mathTransfer',mathNode('mrow',mathNode('mrow',mathNode('mi','G'),mathNode('mo','('),mathNode('mi','z'),mathNode('mo',')')),mathNode('mo','='),
      mathNode('mfrac',mathSum(tf.numerator_terms),mathSum(tf.denominator_terms))),tf.text);
    $('mathSampling').textContent=`采样周期 Ts = ${fmt(model.sample_time,8)} s　·　输入延迟 ${tf.delay_samples} 点 / ${fmt(tf.delay_seconds,8)} s　·　常值项 c = ${fmt(model.offset,8)}`;
  }
  if(ss){
    $('stateDimension').textContent=`${ss.dimension} 个滞后状态`;
    $('stateEquations').textContent=ss.equations.join('\n');$('stateDefinition').textContent=ss.state_definition;
    $('stateMatrices').replaceChildren();
    for(const name of ['A','B','C','D','g','h']){
      const item=document.createElement('div'),label=document.createElement('span'),pre=document.createElement('pre');
      label.textContent=`${name} (${ss[name].length} × ${ss[name][0].length})`;
      pre.textContent='['+ss[name].map(row=>'['+row.map(value=>fmt(value,8)).join(', ')+']').join('\n ')+']';
      item.append(label,pre);$('stateMatrices').append(item);
    }
  }
  $('mathNotes').replaceChildren();
  const notes=[...(online?['在线传递函数与状态矩阵按此刻系数冻结表示；参数随时间更新时，整段过程不等同于一个固定线性时不变系统。']:[]),...model.notes];
  if(result.diagnostics.rank<result.diagnostics.parameters)notes.unshift('当前数据的有效秩不足，以下为当前估计表达式，部分系数尚不能被独立确定。');
  for(const note of notes){const p=document.createElement('p');p.textContent=note;$('mathNotes').append(p);}
}
$('exportMathModel').addEventListener('click',()=>{
  const result=state.result,model=result?.mathematical_model;if(!model)return;
  const header=result.mode==='online'?'在线参数快照；采用当前系数表示模型。\n\n':'';
  const channels=result.acquisition_channels?'\n\n采集通道与单位：\n'+JSON.stringify(result.acquisition_channels,null,2):'';
  download(header+model.text+channels,'辨识数学模型.txt','text/plain;charset=utf-8');
});
