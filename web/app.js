'use strict';
// Transport, presentation and session lifecycle. All estimators live in Python.
const $ = id => document.getElementById(id);
const palette = ['#2369db', '#14a394', '#e79942', '#8964cc', '#df748b', '#637688'];
const state = {mode:'offline', csv:'', validation:'', rows:[], columns:[], channels:null, hasHeader:true, timeInfo:{}, result:null,
  session:null, sequence:0, cursor:0, paused:false, running:false, timer:null, live:false, busy:false};

function status(message, error=false) { $('status').textContent = message; $('status').classList.toggle('error', error); }
function fmt(value, digits=5) { return value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : Number(Number(value).toPrecision(digits)).toString(); }
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '本地服务请求失败');
  return body;
}
function buttonState() {
  const arx=$('modelKind').value==='arx',channels=arx?dynamicChannels():null;
  $('configuration').disabled = state.busy || !!state.session;
  $('fitButton').disabled = state.busy || !state.rows.length || (arx&&!channels.valid);
  $('replayButton').disabled = state.busy || !state.rows.length || !!state.session || (arx&&!channels.valid);
  $('liveButton').disabled = state.busy || !state.rows.length || !!state.session || (arx&&!channels.valid);
  $('pauseButton').disabled = state.busy || !state.session || state.live || state.cursor >= state.rows.length;
  $('endButton').disabled = state.busy || !state.session;
  document.querySelectorAll('[data-mode]').forEach(button => { button.disabled = state.busy; });
  if(typeof acquisitionControls==='function')acquisitionControls();
}
function invalidate() { state.result=null; $('resultContent').hidden=true; $('emptyState').hidden=false; }
function selectOptions(id, selected, empty=false, emptyLabel='无时间列') {
  const select=$(id); select.replaceChildren();
  if(empty) select.add(new Option(emptyLabel, ''));
  state.columns.forEach(name => select.add(new Option(name,name)));
  if (selected !== undefined && [...select.options].some(option => option.value === selected)) select.value=selected;
}
function renderFeatures(selected) {
  const chosen = selected || [...document.querySelectorAll('#features input:checked')].map(input=>input.value);
  $('features').replaceChildren();
  state.columns.filter(name=>name!==$('outputColumn').value && name!==$('timeColumn').value).forEach(name=>{
    const label=document.createElement('label'), input=document.createElement('input');
    input.type='checkbox'; input.value=name; input.checked=chosen.includes(name); label.classList.toggle('selected',input.checked);
    label.append(input, document.createTextNode(name)); $('features').append(label);
    input.addEventListener('change',()=>label.classList.toggle('selected',input.checked));
  });
}
function checkedChannels(id){return [...document.querySelectorAll(`#${id} input:checked`)].map(input=>input.value);}
function timeInfo(column){
  if(!column)return {sample_time:null,errors:[]};
  if(state.timeInfo[column])return state.timeInfo[column];
  const times=state.rows.map(row=>String(row[column]).trim()===''?NaN:Number(row[column]));
  const differences=times.slice(1).map((value,i)=>value-times[i]);
  const sorted=[...differences].sort((a,b)=>a-b),middle=Math.floor(sorted.length/2);
  const dt=sorted.length%2?sorted[middle]:(sorted[middle-1]+sorted[middle])/2;
  const valid=times.length>1&&times.every(Number.isFinite)&&Number.isFinite(dt)&&dt>0&&differences.every(value=>value>0&&Math.abs(value-dt)<=Math.max(1e-10,.01*dt));
  return state.timeInfo[column]={sample_time:valid?dt:null,errors:valid?[]:['时间列必须是严格递增、等间隔的秒数。']};
}
function dynamicChannels(){
  if(state.mode==='offline'&&$('channelMode').value==='auto')return state.channels||{inputs:[],outputs:[],time:'',sample_time:null,valid:false,errors:[],ignored:[]};
  const online=state.mode==='online';
  const inputs=online?[$('inputColumn').value].filter(Boolean):checkedChannels('dynamicInputs');
  const outputs=online?[$('outputColumn').value].filter(Boolean):checkedChannels('dynamicOutputs');
  const time=online?$('timeColumn').value:$('dynamicTimeColumn').value,info=timeInfo(time);
  const errors=[...info.errors];
  if(!inputs.length||!outputs.length)errors.push('请至少选择一路输入与一路输出。');
  if(inputs.some(name=>outputs.includes(name))||[...inputs,...outputs].includes(time))errors.push('时间、输入和输出列不能重复。');
  return {inputs,outputs,time,...info,errors,valid:!errors.length,detected:!!inputs.length&&!!outputs.length,
    system_type:`${inputs.length===1?'S':'M'}I${outputs.length===1?'S':'M'}O`,ignored:state.columns.filter(name=>![...inputs,...outputs,time].includes(name))};
}
function usesMultichannel(){
  if(state.mode!=='offline'||$('modelKind').value!=='arx')return false;
  const c=dynamicChannels();return c.inputs.length>1||c.outputs.length>1;
}
function renderDynamicMapping(inputs=[],outputs=[]){
  for(const [id,selected] of [['dynamicInputs',inputs],['dynamicOutputs',outputs]]){
    $(id).replaceChildren();
    state.columns.filter(name=>name!==$('dynamicTimeColumn').value).forEach(name=>{
      const label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.value=name;box.checked=selected.includes(name);
      label.classList.toggle('selected',box.checked);label.append(box,document.createTextNode(name));$(id).append(label);
    });
  }
}
function renderDataPreview(){
  $('dataPreview').hidden=!state.rows.length;$('dataPreviewTable').replaceChildren();
  const header=document.createElement('tr');for(const name of state.columns){const th=document.createElement('th');th.textContent=name;header.append(th);}$('dataPreviewTable').append(header);
  for(const data of state.rows.slice(0,3)){const row=document.createElement('tr');for(const name of state.columns){const cell=document.createElement('td');cell.textContent=data[name];row.append(cell);}$('dataPreviewTable').append(row);}
}
function conditionalFields() {
  const arx=$('modelKind').value==='arx',multi=usesMultichannel(),offlineDynamic=arx&&state.mode==='offline';
  $('regressionFields').hidden=arx; $('arxFields').hidden=!arx;
  $('inputColumn').parentElement.hidden=offlineDynamic;
  $('dynamicMapping').hidden=!offlineDynamic;$('dynamicManual').hidden=$('channelMode').value!=='manual';
  $('excitationAdvice').hidden=!arx;
  $('channelDetection').hidden=!offlineDynamic;$('manualMapping').hidden=offlineDynamic;
  $('multichannelAlgorithm').hidden=!multi;
  for(const option of $('algorithm').options)option.hidden=option.value==='frequency'?!arx:option.value==='oe'?!multi:multi;
  if(multi&&!['oe','frequency'].includes($('algorithm').value))$('algorithm').value='oe';
  if(!multi&&$('algorithm').value==='oe')$('algorithm').value='ls';
  if(!arx&&$('algorithm').value==='frequency')$('algorithm').value='ls';
  $('onlineAlgorithm').querySelector('[value="frequency"]').hidden=!arx;
  if(!arx)$('onlineAlgorithm').value='rls';
  const frequency=arx&&(state.mode==='offline'?$('algorithm').value:$('onlineAlgorithm').value)==='frequency';
  $('frequencySettings').hidden=!frequency;$('frequencyOnlineSettings').hidden=state.mode!=='online';
  $('rlsSettings').hidden=$('onlineAlgorithm').value==='frequency';
  $('multichannelAlgorithm').textContent=frequency?'各输入联合频谱估计 → 每路输出稳定传递函数拟合 → 自动选阶与独立验证':'多输入联合 ARX 初始化 → 输出误差精修 → 逐输出验证';
  const c=arx?dynamicChannels():null;
  $('sampleTime').disabled=offlineDynamic&&!!c.sample_time;
  if(offlineDynamic&&c.sample_time)$('sampleTime').value=Number(c.sample_time.toPrecision(12));
  $('frequencyHint').textContent=`50% 重叠；频率间隔约 ${fmt(1/(Number($('sampleTime').value)*Number($('frequencySegment').value)),5)} Hz。离线训练段至少容纳 4 个重叠窗；在线窗口至少为分段点数的 4 倍。长窗分辨率更高，但更新更慢。`;
  $('modelHelp').textContent=arx?'ARX 支持按表头识别，或手动选择时间、输入和输出；各输入共同参与所选输出的辨识。':'特征可以是预先计算的加速度、电流或非线性基函数列。参数单位沿用数据定义。';
  if(offlineDynamic){
    $('channelSummary').textContent=c.detected?`${c.inputs.length} 路输入 → ${c.outputs.length} 路输出 · ${c.system_type}`:'未发现完整的 input1… / output1… 表头';
    $('channelDetail').textContent=[$('channelMode').value==='auto'?'按表头自动识别':'手动选择信号列',`输入：${c.inputs.join('、')||'未选择'}`,`输出：${c.outputs.join('、')||'未选择'}`,
      c.time?`时间：${c.time}（秒），Ts = ${fmt(c.sample_time,8)} s`:'无时间列，请填写下方采样周期（秒）',
      ...c.errors,c.ignored.length?`忽略说明列：${c.ignored.join('、')}`:''].filter(Boolean).join('；');
  }
  $('scaleFields').hidden=arx; $('autoScaleHint').hidden=!arx;
  $('autoOrderHint').textContent=frequency?(state.mode==='online'?'先收满滑动窗口自动定阶，此后固定结构并定期更新参数。':'使用独立选模段的自由运行误差与复杂度选择结构。'):state.mode==='online'?'先收集 400 点自动定阶；激励不足时继续收集，再自动重试。':'使用独立选模段比较候选，最终验证数据不参与定阶。';
  $('autoOrderHint').previousElementSibling.textContent=frequency?'频域自动比较输出阶次 1–6、输入项数 1–7、延迟 0–10 点；模型约束为稳定。':multi?'自动比较输出阶次与每路输入项数 1–6、起始延迟 0–4 点；使用复杂度惩罚选模。':'自动比较输出阶次 1–8、输入项数 1–8、延迟 0–10 点。误差接近时优先更简洁的模型。';
  if(!state.validation) $('validationInfo').textContent=arx||multi?'未选择：60% 训练 / 20% 选模 / 20% 验证':'未选择：使用末尾 30% 数据验证';
  $('weightField').hidden=multi||$('algorithm').value!=='wls'; $('alphaField').hidden=multi||$('algorithm').value!=='ridge';
  $('weightHint').textContent=$('weightColumn').value?'使用所选列加权；训练样本的权重须为有限正数。':'未选择权重列：按普通最小二乘（LS）计算。';
  $('equation').textContent=multi?'Y(z) = G(z) U(z) · 传递函数矩阵':arx?'y[k] = Σ aᵢy[k−i] + Σ bⱼu[k−nk−j] + c':'y = φᵀθ + c';
  if (!$('intercept').checked) $('equation').textContent=$('equation').textContent.replace(' + c','');
}
function model() {
  if($('modelKind').value==='arx'&&state.mode==='offline'){
    const c=dynamicChannels();
    return {kind:usesMultichannel()?'multichannel':'arx',inputs:c.inputs,outputs:c.outputs,input:c.inputs[0],output:c.outputs[0],time:c.time,
      sample_time:Number($('sampleTime').value),intercept:$('intercept').checked};
  }
  return {kind:$('modelKind').value,output:$('outputColumn').value,time:$('timeColumn').value,
    features:[...document.querySelectorAll('#features input:checked')].map(input=>input.value),
    input:$('inputColumn').value,
    sample_time:Number($('sampleTime').value),intercept:$('intercept').checked};
}
function frequencyConfig(){return {window:$('frequencyWindow').value,segment_length:Number($('frequencySegment').value),window_samples:Math.max(Number($('frequencySamples').value),state.mode==='offline'?4*Number($('frequencySegment').value):0),update_every:Number($('frequencyHop').value)};}
function onlineConfig() {
  const scales=$('modelKind').value==='arx'?'':$('scales').value.trim();
  return {model:model(),order_mode:'auto',forgetting:Number($('forgetting').value),p0:Number($('p0').value),algorithm:$('onlineAlgorithm').value,frequency:frequencyConfig(),
    ...(scales?{scales:scales.split(/[,，\s]+/).map(Number)}:{})};
}
async function loadCSV(text, name, config) {
  const inspected=await api('/api/inspect',{csv:text,header_mode:$('headerMode').value});
  state.csv=text;state.rows=inspected.rows;state.columns=inspected.columns;
  state.channels=inspected.channels;state.hasHeader=inspected.has_header;state.timeInfo={};
  state.validation='';$('validationFile').value='';$('validationInfo').textContent='未选择：使用末尾 30% 数据验证';$('clearValidation').hidden=true;
  $('filename').textContent=name; $('dataInfo').textContent=`${inspected.samples.toLocaleString()} 个样本 · ${inspected.columns.length} 列 · ${inspected.has_header?'有表头':'无表头，首行采样已保留'}${config?' · 已知真值的仿真数据':''}`;
  const spec=config?.model||{};
  selectOptions('outputColumn',spec.output||state.channels?.outputs[0]||state.columns.at(-1));
  selectOptions('timeColumn',spec.time??state.columns.find(name=>/^(time|t|time_s)$/i.test(name))??'',true);
  selectOptions('inputColumn',spec.input||state.channels?.inputs[0]||state.columns.find(name=>name!==$('timeColumn').value && name!==$('outputColumn').value));
  selectOptions('dynamicTimeColumn',spec.time??state.channels?.time??'',true);
  renderDynamicMapping(spec.inputs||(spec.input?[spec.input]:state.channels?.inputs),spec.outputs||(spec.output?[spec.output]:state.channels?.outputs));
  $('channelMode').value=state.channels?.detected?'auto':'manual';
  selectOptions('weightColumn',config?.weight_column??'',true,'不加权（普通最小二乘）');
  if(config) {
    $('modelKind').value=spec.kind==='multichannel'?'arx':spec.kind; $('intercept').checked=spec.intercept!==false;
    $('sampleTime').value=spec.sample_time;
    $('algorithm').value=['ls','wls','ridge','oe','frequency'].includes(config.algorithm)?config.algorithm:'ls'; $('scales').value='';
  } else if ($('timeColumn').value && state.rows.length>1) {
    const delta=Number(state.rows[1][$('timeColumn').value])-Number(state.rows[0][$('timeColumn').value]);
    if(delta>0) $('sampleTime').value=Number(delta.toPrecision(10));
  }
  if(!config&&(state.channels?.detected||!state.hasHeader)) $('modelKind').value='arx';
  renderFeatures(spec.features||state.columns.filter(name=>![$('timeColumn').value,$('outputColumn').value].includes(name)));
  renderDataPreview();conditionalFields();invalidate();buttonState();status(state.channels?.detected&&!config?`已载入 ${name}，自动识别 ${state.channels.inputs.length} 路输入、${state.channels.outputs.length} 路输出。${state.channels.errors.join(' ')} 点击“开始离线辨识”即可计算。`:`已载入 ${name}。${!state.hasHeader?'首行采样已保留，请在 ARX 中选择时间、输入和输出列。':'请核对信号列和采样周期。'}`,!!state.channels?.errors.length);
}
async function fileText(file) {
  if (!file || file.size>12*1024*1024) throw new Error('请选择不超过 12MB 的 CSV 文件。');
  const bytes=await file.arrayBuffer();
  try {return new TextDecoder('utf-8',{fatal:true}).decode(bytes);} catch {return new TextDecoder('gb18030',{fatal:true}).decode(bytes);}
}
async function guarded(action) {
  if(state.busy) return;
  state.busy=true;buttonState();
  try {await action();} catch(error) {status(error.message,true);} finally {state.busy=false;buttonState();}
}
for(const kind of ['regression','arx']) $(`${kind}Demo`).addEventListener('click',()=>guarded(async()=>{
  $('headerMode').value='auto';
  const demoKind=kind==='arx'&&(state.mode==='offline'?$('algorithm').value:$('onlineAlgorithm').value)==='frequency'?'frequency':kind;
  const demo=await api(`/api/demo?kind=${demoKind}`);await loadCSV(demo.csv,`${kind}.csv · 仿真`,demo.config);
}));
$('dataFile').addEventListener('change',event=>guarded(async()=>{const file=event.target.files[0];if(file) await loadCSV(await fileText(file),file.name);}));
$('headerMode').addEventListener('change',()=>{if(state.csv)guarded(()=>loadCSV(state.csv,$('filename').textContent));});
$('validationFile').addEventListener('change',event=>guarded(async()=>{
  const file=event.target.files[0];if(!file) return;
  const text=await fileText(file);await api('/api/inspect',{csv:text});state.validation=text;
  $('validationInfo').textContent=file.name;$('clearValidation').hidden=false;invalidate();
}));
$('clearValidation').addEventListener('click',()=>{state.validation='';$('validationFile').value='';$('clearValidation').hidden=true;conditionalFields();invalidate();});
$('configuration').addEventListener('input',event=>{
  if(event.target.type==='file') return;
  if(['outputColumn','timeColumn'].includes(event.target.id)) renderFeatures();
  if(event.target.closest('#dynamicInputs,#dynamicOutputs')||event.target.id==='dynamicTimeColumn'){
    let inputs=checkedChannels('dynamicInputs'),outputs=checkedChannels('dynamicOutputs');
    if(event.target.checked&&event.target.closest('#dynamicInputs'))outputs=outputs.filter(name=>name!==event.target.value);
    if(event.target.checked&&event.target.closest('#dynamicOutputs'))inputs=inputs.filter(name=>name!==event.target.value);
    renderDynamicMapping(inputs,outputs);
  }
  conditionalFields();invalidate();buttonState();
});

const NS='http://www.w3.org/2000/svg';
function svgElement(tag,attrs,text) {const element=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs)) element.setAttribute(k,String(v));if(text!==undefined)element.textContent=text;return element;}
function chart(id,legendId,rows,series,xLabel='时间 / s') {
  const host=$(id), legend=$(legendId);host.replaceChildren();legend.replaceChildren();
  const width=760,height=270,left=63,right=15,top=15,bottom=38;
  const svg=svgElement('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':id==='responseChart'?'测量与模型响应随时间的变化':'参数估计随时间的变化'});
  host.append(svg);
  const values=rows.flatMap(row=>series.map(s=>s.get(row))).filter(v=>v!==null&&Number.isFinite(v));
  if(!rows.length||!values.length) {svg.append(svgElement('text',{x:width/2,y:height/2,'text-anchor':'middle'},'等待有效样本 / ARX 历史预热'));return;}
  let ymin=Math.min(...values),ymax=Math.max(...values),xmin=rows[0].time,xmax=rows.at(-1).time;
  const margin=(ymax-ymin)*0.08 || Math.max(Math.abs(ymin)*.05,1);ymin-=margin;ymax+=margin;
  if(id==='coherenceChart'){ymin=0;ymax=1;}
  if(xmin===xmax)xmax=xmin+1;
  const sx=x=>left+(x-xmin)/(xmax-xmin)*(width-left-right),sy=y=>top+(ymax-y)/(ymax-ymin)*(height-top-bottom);
  for(let i=0;i<5;i++) {
    const y=ymin+(ymax-ymin)*i/4;
    svg.append(svgElement('line',{x1:left,x2:width-right,y1:sy(y),y2:sy(y),class:'grid'}));
    svg.append(svgElement('text',{x:left-10,y:sy(y)+3,'text-anchor':'end'},fmt(y,3)));
    const x=xmin+(xmax-xmin)*i/4;
    svg.append(svgElement('text',{x:sx(x),y:height-16,'text-anchor':'middle'},fmt(x,3)));
  }
  svg.append(svgElement('text',{x:width-right,y:height-1,'text-anchor':'end'},xLabel));
  series.forEach((s,index)=>{
    const span=document.createElement('span'),marker=document.createElement('i');marker.style.background=s.color||palette[index];span.append(marker,document.createTextNode(s.name));legend.append(span);
    let active=false;const path=rows.map(row=>{const value=s.get(row);if(value===null||!Number.isFinite(value)){active=false;return '';}
      const segment=`${active?'L':'M'}${sx(row.time).toFixed(2)},${sy(value).toFixed(2)}`;active=true;return segment;}).join(' ');
    svg.append(svgElement('path',{d:path,stroke:s.color||palette[index],...(s.dashed?{'stroke-dasharray':'5 4'}:{})}));
  });
}
function renderOrder(result) {
  const selection=result.order_selection;
  $('orderCard').hidden=!selection;
  if(!selection) return;
  const pending=selection.status==='collecting';
  $('orderDetails').hidden=pending;
  $('orderTitle').textContent=pending?'正在收集自动定阶数据':'自动推荐结构';
  for(const [id,key] of [['selectedNa','na'],['selectedNb','nb'],['selectedNk','nk']]) $(id).textContent=pending?'待定':selection.selected[key];
  if(pending) {
    $('orderSummary').textContent=`已接收 ${selection.received} 点 / 下次定阶 ${selection.target_samples} 点`;
    $('orderContext').textContent=selection.message||'初始数据用于确定阶次、延迟和参数初值；定阶完成后开始显示在线预测。';
    $('orderCandidates').replaceChildren();return;
  }
  const chosen=selection.selected;
  $('orderSummary').textContent=`比较 ${selection.candidate_count} 个结构，${selection.valid_count} 个可用 · 推荐模型选模 RMSE ${fmt(chosen.selection_rmse)} · 输入延迟 ${fmt(chosen.nk*result.model.sample_time)} s`;
  $('orderContext').textContent=result.algorithm==='frequency'?selection.criterion:result.mode==='online'?`第 ${selection.calibration_samples} 点完成初始定阶，此后固定结构并更新参数。`:
    `训练 ${result.train_samples} 点 / 选模 ${result.selection_samples} 点 / 最终验证 ${result.validation_samples} 点。最终验证数据未参与定阶。`;
  $('orderCandidates').replaceChildren();
  selection.candidates.filter(candidate=>candidate.status==='valid').slice(0,12).forEach(candidate=>{
    const row=document.createElement('tr');row.classList.toggle('recommended',candidate.selected);
    for(const value of [candidate.selected?'推荐':'候选',candidate.na,candidate.nb,candidate.nk,candidate.parameters,fmt(candidate.selection_rmse,6)]) {
      const cell=document.createElement('td');cell.textContent=value;row.append(cell);
    }
    $('orderCandidates').append(row);
  });
}
function render(result) {
  if(result.model.kind==='multichannel'){renderMultichannel(result);return;}
  if($('multichannelResults'))$('multichannelResults').hidden=true;
  $('rankValue').previousElementSibling.textContent='有效秩 / 参数数';
  if(result.mode==='online'&&result.history_mode==='append') {
    const previous=state.result?.mode==='online'&&state.result.session_id===result.session_id?state.result.series:[];
    result={...result,series:[...previous,...result.series].slice(-2000),history_mode:'replace'};
  }
  state.result=result;$('emptyState').hidden=true;$('resultContent').hidden=false;
  const online=result.mode==='online',data=result.series,diagnostic=result.diagnostics;
  renderFrequency(result.frequency,result.model.input?[result.model.input]:[]);
  renderOrder(result);
  if(typeof renderMathematicalModel==='function')renderMathematicalModel(result);
  $('exportModel').disabled=!result.parameters.length;
  $('exportCSV').disabled=online&&!data.length;
  $('sessionCard').hidden=!online;$('parameterChartCard').hidden=!online;
  if(online) {
    $('sessionId').textContent=result.session_id||state.session;
    $('sessionMode').textContent=state.live?'实时 HTTP 数据接入':'CSV 回放 · 软实时';
    $('sessionHelp').textContent=state.live?'采集程序向 /api/session/push 提交样本；本页面每 0.5 秒刷新。':'回放只验证流式处理，不代表已经连接真实传感器。';
    const rmse=data.length?Math.sqrt(data.reduce((sum,row)=>sum+row.residual**2,0)/data.length):null;
    $('metric1Label').textContent='在线预测 RMSE';$('metric1').textContent=fmt(rmse);$('metric1Note').textContent='更新前 · 最近 2,000 点';
    $('metric2Label').textContent='已完成更新';$('metric2').textContent=result.updates.toLocaleString();$('metric2Note').textContent=result.calibration_samples?`含初始定阶数据的内部初始化`:`预热剩余 ${result.warmup_remaining} 点`;
    $('metric3Label').textContent='已接收样本';$('metric3').textContent=result.next_sequence.toLocaleString();$('metric3Note').textContent=state.live?'实际到达的数据':'CSV 回放数据';
    $('chartTitle').textContent='在线输出与预测';$('chartCaption').textContent='参数更新前的一步预测，不是独立验证';
    $('extraLabel').textContent='遗忘因子 λ';$('extraValue').textContent=fmt(diagnostic.forgetting);
    if(result.algorithm==='frequency'){
      $('metric2Label').textContent='窗口模型更新';$('metric2Note').textContent=`下次更新：第 ${result.frequency.next_update_at} 点`;
      $('extraLabel').textContent='平均相干度';$('extraValue').textContent=fmt(result.frequency.mean_coherence);
      $('rankValue').previousElementSibling.textContent='频域动态参数秩 / 参数数';
    }
    chart('parameterChart','parameterLegend',data,result.parameters.slice(0,6).map((parameter,index)=>({name:parameter.name,get:row=>row.theta[index]})));
  } else {
    $('metric1Label').textContent='验证 RMSE';$('metric1').textContent=fmt(result.metrics.rmse);$('metric1Note').textContent='一步预测';
    $('metric2Label').textContent='验证拟合度';$('metric2').textContent=result.metrics.fit_percent===null?'—':`${result.metrics.fit_percent.toFixed(2)}%`;$('metric2Note').textContent='归一化误差指标 · 可为负值';
    $('metric3Label').textContent='有效验证样本';$('metric3').textContent=result.validation_samples.toLocaleString();$('metric3Note').textContent=result.validation_source;
    $('chartTitle').textContent='验证响应';$('chartCaption').textContent=`训练 ${result.train_samples} 点 / ${result.selection_samples?'选模 '+result.selection_samples+' 点 / ':''}验证 ${result.validation_samples} 点 · 图表最多显示 1,500 点`;
    $('extraLabel').textContent=result.model.kind==='arx'?'自由运行 RMSE':'残差一阶相关系数';
    $('extraValue').textContent=fmt(result.model.kind==='arx'?result.simulation_metrics?.rmse:result.residual_lag1);
  }
  const response=[{name:'测量输出',get:row=>row.measured,color:'#8fa3b8'},{name:'一步预测',get:row=>row.prediction,color:'#2369db'}];
  if(!online&&result.simulation_metrics) response.push({name:'自由运行',get:row=>row.simulation,color:'#14a394',dashed:true});
  chart('responseChart','responseLegend',data,response);
  $('parameters').replaceChildren();result.parameters.forEach(parameter=>{
    const row=document.createElement('tr');for(const text of [parameter.name,fmt(parameter.value,8)]){const td=document.createElement('td');td.textContent=text;row.append(td);}$('parameters').append(row);
  });
  $('algorithmTag').textContent=result.algorithm.toUpperCase();$('rankValue').textContent=`${diagnostic.rank} / ${diagnostic.parameters}`;
  $('conditionValue').textContent=result.order_selection?.status==='collecting'?'等待定阶':diagnostic.condition===null?'秩不足':fmt(diagnostic.condition);
  $('warnings').replaceChildren();result.warnings.forEach(message=>{const p=document.createElement('p');p.textContent=message;$('warnings').append(p);});
}
$('fitButton').addEventListener('click',()=>guarded(async()=>{
  if(usesMultichannel()){
    status('正在联合全部输入，为每路输出自动选阶并精修模型…');
    const result=await api('/api/offline',{csv:state.csv,header_mode:$('headerMode').value,validation_csv:state.validation,model:model(),algorithm:$('algorithm').value,frequency:frequencyConfig()});
    render(result);status(`辨识完成：${result.model.inputs.length} 路输入 → ${result.model.outputs.length} 路输出。下方可切换输出曲线并查看全部传递函数。`);return;
  }
  status($('algorithm').value==='frequency'?'正在分窗估计频响并拟合稳定传递函数…':$('modelKind').value==='arx'?'正在比较 704 个阶次与延迟组合，再检查最终验证响应…':'正在拟合训练数据并检查验证响应…');
  const result=await api('/api/offline',{csv:state.csv,header_mode:$('headerMode').value,validation_csv:state.validation,model:model(),algorithm:$('algorithm').value,
    weight_column:$('algorithm').value==='wls'?$('weightColumn').value:'',alpha:Number($('alpha').value),train_ratio:.7,order_mode:'auto',frequency:frequencyConfig()});
  render(result);status((result.order_selection?`自动定阶完成：na=${result.model.na}，nb=${result.model.nb}，nk=${result.model.nk}。请查看候选比较与最终验证结果。`:'辨识完成。请结合参数表、验证响应和数值诊断评估模型。')+(result.algorithm_note||''));
}));

async function endSession() {
  clearTimeout(state.timer);state.running=false;
  if(state.session) await api('/api/session/delete',{session_id:state.session});
  state.session=null;state.paused=false;state.live=false;$('pauseButton').textContent='暂停回放';buttonState();
}
async function replayTick() {
  if(!state.session||state.paused||!state.running||state.live) return;
  state.busy=true;buttonState();
  try {
    const samples=state.rows.slice(state.cursor,state.cursor+10);
    const result=await api('/api/session/push',{session_id:state.session,sequence:state.sequence,samples});
    state.sequence=result.next_sequence;state.cursor+=samples.length;render(result);
    if(state.cursor>=state.rows.length){state.running=false;status(result.order_selection?.status==='collecting'?`回放已结束，尚未完成自动定阶：请提供更多或激励更充分的数据。`:`回放完成：接收 ${state.cursor} 点、更新 ${result.updates} 次。结束会话后可修改配置。`,result.order_selection?.status==='collecting');}
    else {status(`CSV 回放中 · ${state.cursor} / ${state.rows.length} 点`);state.timer=setTimeout(replayTick,100);}
  }catch(error){state.paused=true;state.running=false;status(error.message,true);$('pauseButton').textContent='继续回放';}
  finally{state.busy=false;buttonState();}
}
async function liveTick() {
  if(!state.session||!state.live) return;
  const id=state.session;
  try {const result=await api(`/api/session?id=${id}`);if(state.session!==id)return;state.sequence=result.next_sequence;render(result);state.timer=setTimeout(liveTick,500);}
  catch(error){status(error.message,true);}
}
async function startSession(live) {
  const result=await api('/api/session/create',onlineConfig());
  state.session=result.session_id;state.sequence=0;state.cursor=0;state.live=live;state.paused=false;state.running=!live;
  render(result);status(live?'实时会话已建立，等待采集程序提交样本。':'回放会话已建立，即将按顺序处理 CSV 样本。');
  state.timer=setTimeout(live?liveTick:replayTick,100);
}
$('replayButton').addEventListener('click',()=>guarded(()=>startSession(false)));
$('liveButton').addEventListener('click',()=>guarded(()=>startSession(true)));
$('pauseButton').addEventListener('click',()=>{
  state.paused=!state.paused;$('pauseButton').textContent=state.paused?'继续回放':'暂停回放';
  clearTimeout(state.timer);if(!state.paused){state.running=true;replayTick();}else status('回放已暂停；参数和历史保留。');
});
$('endButton').addEventListener('click',()=>guarded(async()=>{await endSession();status('会话已结束，最后一次结果仍可导出。');}));
document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>guarded(async()=>{
  if(state.mode===button.dataset.mode)return;
  if(window.leaveAcquisition)await window.leaveAcquisition();
  await endSession();state.mode=button.dataset.mode;invalidate();
  document.querySelectorAll('[data-mode]').forEach(item=>item.classList.toggle('active',item.dataset.mode===state.mode));
  document.querySelectorAll('.offline-only').forEach(element=>element.hidden=state.mode!=='offline');
  document.querySelectorAll('.online-only').forEach(element=>element.hidden=state.mode!=='online');
  conditionalFields();
  $('pageTitle').textContent=state.mode==='offline'?'离线参数辨识':'在线参数辨识';
  $('pageSubtitle').textContent=state.mode==='offline'?'从实验数据建立模型，用留出数据检验结果。':'逐样本更新参数，观察激励与时变特性。';
  status(state.mode==='offline'?'离线模式：训练数据与验证数据分开处理。':'在线模式：选择 CSV 回放，或建立会话接收采集程序的实时数据。');
  if(window.showAcquisition)await window.showAcquisition();
})));

function download(text,filename,type='text/plain') {
  const url=URL.createObjectURL(new Blob([text],{type}));const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('exportModel').addEventListener('click',()=>{if(!state.result)return;const {csv_export,report,series,...model}=state.result;download(JSON.stringify(model,null,2),'parameter_model.json','application/json');});
$('exportCSV').addEventListener('click',()=>{
  const result=state.result;if(!result)return;
  let csv=result.csv_export;
  if(result.mode==='online') {
    const quote=value=>'"'+String(value).replaceAll('"','""')+'"';
    csv=[['time_s','measured','prediction_before_update','residual',...result.parameters.map(parameter=>parameter.name)].map(quote).join(','),
      ...result.series.map(row=>[row.time,row.measured,row.prediction,row.residual,...row.theta].join(','))].join('\n');
  }
  download('\ufeff'+csv,result.mode==='online'?'online_recent_2000.csv':'validation.csv','text/csv;charset=utf-8');
});
$('exportReport').addEventListener('click',()=>{if(state.result?.report)download(state.result.report,'辨识报告.md','text/markdown;charset=utf-8');});
conditionalFields();buttonState();

// Frequency plots report physical FRF magnitude and multiple coherence.
let visibleFrequency=null,visibleFrequencyInputs=[];
function renderFrequency(info,inputs=[]){
  visibleFrequency=info;visibleFrequencyInputs=inputs;
  $('frequencyCard').hidden=!info;if(!info)return;
  const old=$('frequencyInput').value;$('frequencyInput').replaceChildren();
  inputs.forEach((name,i)=>$('frequencyInput').add(new Option(name,String(i))));
  $('frequencyInput').value=[...$('frequencyInput').options].some(option=>option.value===old)?old:'0';
  $('frequencySummary').textContent=info.spectrum?
    `${info.window} 窗 · 每段 ${info.segment_length} 点 · ${info.segments} 个重叠窗 · ${info.used_bins} 个有效频点 · Δf=${fmt(info.resolution_hz)} Hz · 平均相干度 ${fmt(info.mean_coherence)}${info.next_update_at?' · 下次更新第 '+info.next_update_at+' 点 · 当前模型已使用 '+info.model_age_samples+' 点':''}${info.last_error?' · 本窗未更新：'+info.last_error:''}`:
    `正在收集窗口：${info.buffered_samples||0} / ${info.window_samples} 点；收满后拟合并自动选阶。`;
  drawFrequency();
}
function drawFrequency(){
  const rows=(visibleFrequency?.spectrum||[]).map(r=>({...r,time:r.frequency_hz})),j=Number($('frequencyInput').value)||0;
  const db=(r,p)=>20*Math.log10(Math.max(1e-15,Math.hypot(r[p+'_real'][j],r[p+'_imag'][j])));
  chart('frequencyChart','frequencyLegend',rows,[{name:'H1 频响 / dB',get:r=>db(r,'measured')},{name:'拟合模型 / dB',get:r=>db(r,'model')}],'频率 / Hz');
  chart('coherenceChart','coherenceLegend',rows,[{name:'相干度 0–1',get:r=>r.coherence}],'频率 / Hz');
}
$('frequencyInput').addEventListener('change',drawFrequency);
