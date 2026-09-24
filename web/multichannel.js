'use strict';
// Channel names are always text nodes; each transfer path uses server coefficients.
const multiCard=document.createElement('div');multiCard.id='multichannelResults';multiCard.className='card';multiCard.hidden=true;
multiCard.innerHTML='<div class="card-heading"><h2>自动识别的多通道模型</h2><span class="tag" id="multiSystemType"></span></div><p id="multiChannelList"></p><label>查看输出<select id="multiOutput"></select></label><p id="multiOrderSummary" class="muted"></p><div class="table-scroll"><table><thead><tr><th>输出</th><th>na</th><th>nb / 每个输入</th><th>nk</th><th>最终验证 RMSE</th></tr></thead><tbody id="multiModelRows"></tbody></table></div>';
$('orderCard').before(multiCard);
const transferMatrix=document.createElement('div');transferMatrix.id='transferMatrix';transferMatrix.hidden=true;
$('continuousModelBlock').before(transferMatrix);

function multiMatrices(ss){
  $('mathStateSpace').hidden=false;$('stateDimension').textContent=`${ss.dimension} 个状态`;
  $('stateEquations').textContent=ss.equations.join('\n');$('stateDefinition').textContent=ss.state_definition;
  $('stateMatrices').replaceChildren();
  for(const name of ['A','B','C','D','g','h']){
    const item=document.createElement('div'),label=document.createElement('span'),pre=document.createElement('pre');
    label.textContent=`${name} (${ss[name].length} × ${ss[name][0].length})`;
    pre.textContent='['+ss[name].map(row=>'['+row.map(value=>fmt(value,8)).join(', ')+']').join('\n ')+']';
    item.append(label,pre);$('stateMatrices').append(item);
  }
}
function pathFraction(tf,continuous=false){
  const row=mathNode('mrow',mathNode('mfrac',mathSum(tf.numerator_terms),mathSum(tf.denominator_terms)));
  if(continuous&&tf.delay_seconds){
    row.append(mathNode('mo','·'),mathNode('msup',mathNode('mi','e'),mathNode('mrow',mathNode('mo','−'),mathNumber(tf.delay_seconds),mathNode('mi','s'))));
  }
  const math=mathNode('math',row);math.setAttribute('display','block');math.setAttribute('aria-label',tf.text);return math;
}
function renderTransferMatrix(result){
  const model=result.mathematical_model;
  $('mathModelBody').hidden=false;$('mathModelPending').hidden=true;$('exportMathModel').disabled=false;
  $('mathModelCaption').textContent='行对应输出，列对应输入；每一路传递函数均来自全部输入共同参与的模型。';
  $('mathModelBadge').textContent=result.channels.system_type+' · 传递函数矩阵';
  $('mathEquationLabel').textContent='联合差分方程';$('mathEquation').replaceChildren();
  model.equations.forEach(equation=>{const line=document.createElement('p');line.textContent=equation;line.style.whiteSpace='normal';line.style.overflowWrap='anywhere';$('mathEquation').append(line);});
  $('mathSignals').textContent=`采样周期 Ts = ${fmt(result.model.sample_time,8)} s；输入顺序：${result.model.inputs.join('、')}；输出顺序：${result.model.outputs.join('、')}`;
  $('mathTransferBlock').hidden=true;$('continuousModelBlock').hidden=true;
  transferMatrix.replaceChildren();transferMatrix.hidden=false;
  for(const continuous of [false,true]){
    const title=document.createElement('h3');title.textContent=continuous?'连续等效传递函数矩阵 Gc(s)':'离散传递函数矩阵 G(z)';transferMatrix.append(title);
    const wrapper=document.createElement('div');wrapper.className='table-scroll';
    const table=document.createElement('table'),head=document.createElement('thead'),header=document.createElement('tr');
    for(const text of ['输出 / 输入',...result.model.inputs]){const th=document.createElement('th');th.textContent=text;header.append(th);}
    head.append(header);table.append(head);const body=document.createElement('tbody');
    model.transfer_matrix.forEach((pairs,i)=>{
      const tr=document.createElement('tr'),label=document.createElement('th');label.textContent=result.model.outputs[i];tr.append(label);
      pairs.forEach(pair=>{
        const td=document.createElement('td');td.style.minWidth='190px';
        if(!continuous||pair.continuous_model.status==='available')td.append(pathFraction(continuous?pair.continuous_model.transfer_function:pair.transfer_function,continuous));
        else{td.textContent=pair.continuous_model.reason;td.style.whiteSpace='normal';}
        tr.append(td);
      });body.append(tr);
    });table.append(body);wrapper.append(table);transferMatrix.append(wrapper);
  }
  multiMatrices(model.state_space);$('mathNotes').replaceChildren();
  for(const message of model.notes){const p=document.createElement('p');p.textContent=message;$('mathNotes').append(p);}
}
function renderMultiOutput(){
  const result=state.result;if(result?.model.kind!=='multichannel')return;
  const index=Number($('multiOutput').value),output=result.output_results[index],selected=result.output_selections[index];
  $('metric1Label').textContent=`${output.output} · 验证 RMSE`;$('metric1').textContent=fmt(output.metrics.rmse);$('metric1Note').textContent='自由运行 · 原始输出单位';
  $('metric2Label').textContent='验证拟合度';$('metric2').textContent=output.metrics.fit_percent===null?'—':`${output.metrics.fit_percent.toFixed(2)}%`;$('metric2Note').textContent='仅针对当前输出 · 可为负值';
  $('metric3Label').textContent='有效验证样本';$('metric3').textContent=result.validation_samples.toLocaleString();$('metric3Note').textContent=result.validation_source;
  $('multiOrderSummary').textContent=`${output.output}：na=${selected.na}，每路输入 nb=${selected.nb}，公共起始延迟 nk=${selected.nk}；${selected.refined?'采用输出误差精修候选':'保留联合 ARX 候选'}。使用原始信号辨识，自动选模不使用最终验证数据。`;
  if(result.algorithm==='frequency')$('multiOrderSummary').textContent=`${output.output}：na=${selected.na}，nb=${selected.nb}，nk=${selected.nk}；分窗 H1 频响与稳定有理模型拟合，原始输出验证。`;
  renderFrequency(output.frequency,result.model.inputs);
  $('chartTitle').textContent=`${output.output} · 验证响应`;$('chartCaption').textContent=`训练 ${result.train_samples} 点 / 选模 ${result.selection_samples} 点 / 验证 ${result.validation_samples} 点`;
  chart('responseChart','responseLegend',output.series,[{name:'测量输出',get:r=>r.measured,color:'#8fa3b8'},{name:'自由运行',get:r=>r.simulation,color:'#14a394'}]);
  $('parameters').replaceChildren();
  result.parameters.filter(item=>item.name.startsWith(output.output+'.')||item.name.startsWith(output.output+'←')).forEach(item=>{
    const tr=document.createElement('tr');for(const value of [item.name,fmt(item.value,8)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('parameters').append(tr);
  });
  $('rankValue').previousElementSibling.textContent=result.algorithm==='frequency'?'频域动态参数秩 / 参数数':'初始化回归秩 / 参数数';$('rankValue').textContent=`${output.frequency?.jacobian_rank??selected.parameters} / ${output.frequency?.dynamic_parameters??selected.parameters}`;
  $('conditionValue').textContent=fmt(selected.condition);$('extraLabel').textContent='选模自由运行 RMSE';$('extraValue').textContent=fmt(selected.selection_rmse);
}
$('multiOutput').addEventListener('change',renderMultiOutput);
function renderMultichannel(result){
  state.result=result;$('emptyState').hidden=true;$('resultContent').hidden=false;
  $('orderCard').hidden=true;$('sessionCard').hidden=true;$('parameterChartCard').hidden=true;
  multiCard.hidden=false;$('multiSystemType').textContent=result.channels.system_type;
  $('multiChannelList').textContent=`${result.model.inputs.join('、')} → ${result.model.outputs.join('、')}；已计算 ${result.model.inputs.length*result.model.outputs.length} 条输入输出路径。`;
  $('multiOutput').replaceChildren();result.model.outputs.forEach((name,i)=>$('multiOutput').add(new Option(name,String(i))));
  $('multiModelRows').replaceChildren();result.output_selections.forEach((item,i)=>{
    const tr=document.createElement('tr');for(const value of [item.output,item.na,item.nb,item.nk,fmt(result.output_results[i].metrics.rmse,6)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('multiModelRows').append(tr);
  });
  $('algorithmTag').textContent=result.algorithm==='frequency'?'窗口频域 H1':'ARX + OE';$('warnings').replaceChildren();
  result.warnings.forEach(message=>{const p=document.createElement('p');p.textContent=message;$('warnings').append(p);});
  $('exportModel').disabled=false;$('exportCSV').disabled=false;
  renderMultiOutput();renderTransferMatrix(result);
}
