'use strict';
// Beginner workflow shares the existing result renderer; transport and fitting
// run on the local server so browser repaint cannot disturb sample acquisition.
const acquisition = {current:null, busy:false, advanced:false, timer:null, templates:[], preferred:null, columnKey:'', downloadAfterStop:false};
const settingsHost = document.querySelector('.settings');
const legacySettings = document.createElement('div');
legacySettings.id='legacySettings';
while(settingsHost.firstChild) legacySettings.append(settingsHost.firstChild);
settingsHost.append(legacySettings);
const wizard=document.createElement('div');wizard.id='acquisitionWizard';wizard.hidden=true;
wizard.innerHTML=`
<div class="card source-card"><div class="card-heading"><h2><span>01</span>选择采样来源</h2><span class="tag">简单接入</span></div>
<p class="auto-order-note"><strong>推荐激励信号：周期脉冲信号</strong><br>记录多个上升沿和下降沿，脉冲宽度与周期应覆盖对象响应过程，减少长时间不变的稳态段。</p>
<button class="demo-start" id="demoStart">▷ 一键体验演示<span>无需设备 · 仿真数据 · 自动建模</span></button>
<fieldset id="sourceSettings"><label>接入方式<select id="sourceKind"><option value="serial">USB 串口设备</option><option value="file">跟随正在更新的 CSV</option></select></label>
<label>设备模板<select id="deviceTemplate"><option value="">首次使用 · 配置一次即可保存</option></select></label>
<div id="serialSource"><label>采样设备<select id="serialPort"><option value="">正在查找设备…</option></select></label><button class="text-button" id="refreshPorts">↻ 刷新设备列表</button>
<label>设备发送的数据格式<select id="serialFormat"><option value="binary">二进制 · 选择数值类型</option><option value="custom">二进制 · 可编程解码</option><option value="csv">文本 CSV · 逗号分隔数值</option></select></label>
<div id="binarySettings" class="decoder-settings">
<div id="binaryPresetFields"><label>每个值的类型<select id="binaryType"><option value="float64">double / float64 · 8 字节</option><option value="float32">float / float32 · 4 字节</option><option value="uint8">uint8 · 1 字节无符号</option><option value="int8">int8 · 1 字节有符号</option><option value="uint16">uint16 · 2 字节无符号</option><option value="int16">int16 · 2 字节有符号</option><option value="uint32">uint32 · 4 字节无符号</option><option value="int32">int32 · 4 字节有符号</option><option value="uint64">uint64 · 8 字节无符号</option><option value="int64">int64 · 8 字节有符号</option></select></label>
<label>通道名称（按发送顺序，英文逗号分隔）<input id="binaryColumns" value="input1,output1" placeholder="time_s,input1,output1"></label></div>
<label>字节序<select id="binaryEndian"><option value="little">小端 Little-endian</option><option value="big">大端 Big-endian</option></select></label>
<div id="binaryCustomFields" hidden><label>载荷字节数（不含帧头、帧尾）<input id="binaryPayload" type="number" min="1" max="4096" value="16"></label>
<label>解码表达式<textarea id="decoderCode" rows="5" spellcheck="false">input1 = f64(0)
output1 = f64(8)</textarea></label>
<details><summary>表达式用法与内置函数</summary><p class="muted">偏移从载荷第 0 字节开始。每行赋值生成一个通道，可引用前面定义的通道，支持四则运算、位运算及条件表达式。例：output1 = u16(2) * 3.3 / 4095。</p><p class="muted">读取：f64、f32、u8/i8、u16/i16、u32/i32、u64/i64；均传入字节偏移。bits(起始位, 位数) 读取 1～64 位，按每字节最低位优先编号，适用于打包的 uint6。sum8(偏移, 长度)、xor8(偏移, 长度) 可配合 assert 校验；支持 abs、min、max。每帧独立计算，表达式不支持循环、导入或文件操作。</p></details></div>
<details id="frameSettings"><summary>帧头与帧尾（设备有分帧标记时填写）</summary><label>帧头 · 十六进制<input id="binaryHeader" placeholder="例如 AA 55；没有则留空"></label><label>帧尾 · 十六进制<input id="binaryTrailer" placeholder="例如 0D 0A；没有则留空"></label><p class="muted">固定载荷长度，不含编译器额外填充。结构体有填充时请按实际偏移使用可编程解码。</p></details>
<p id="binaryFrameSummary" class="sampling-note"></p><p class="muted" id="binaryAlignmentNote"></p>
<details id="decoderTest"><summary>试解码 · 不连接设备也能核对格式</summary>
<label>示例配置<select id="decoderExample"><option value="double">两个 double：输入、输出</option><option value="timed">三个 double：时间、输入、输出</option><option value="mixed">混合类型：uint32 时间 + float 输入 + int16 输出</option><option value="bits">两个打包 uint6（共 12 位）</option><option value="checksum">两个 uint8 + 和校验</option></select></label>
<button type="button" class="text-button" id="loadDecoderExample">填入示例配置与字节</button>
<label>收到的十六进制字节<textarea id="decoderHex" rows="3" spellcheck="false" placeholder="例如 00 00 00 00 00 00 F0 3F …"></textarea></label><button type="button" class="secondary wide" id="testDecoder">试解码</button><p id="decoderFeedback" class="muted" role="status"></p><pre id="decoderPreview" class="decoder-output" hidden></pre></details>
</div></div>
<div id="fileSource" hidden><button class="secondary wide" id="chooseCaptureFile">选择采样软件正在写入的文件…</button><p class="muted" id="captureFilename">仅接收新增行；历史末尾数据用于预览。</p><details><summary>手动填写文件位置</summary><label>本机完整路径<input id="capturePath" placeholder="例如 D:\\data\\sampling.csv"></label></details></div>
<details class="connection-details"><summary>连接设置（首次连接时核对）</summary><p class="muted">默认 115200 波特、8 数据位、无校验、1 停止位；须与设备一致。二进制数据按字节读取，无需选择文字编码。</p>
<div class="two-cols"><label>波特率<input id="serialBaud" type="number" value="115200"></label><label id="textEncodingField">文字编码<select id="streamEncoding"><option value="utf-8-sig">UTF-8</option><option value="gb18030">GB18030</option></select></label></div>
<div class="three-cols"><label>数据位<select id="serialBits"><option>8</option><option>7</option><option>6</option><option>5</option></select></label><label>校验<select id="serialParity"><option value="N">无</option><option value="E">偶</option><option value="O">奇</option></select></label><label>停止位<select id="serialStop"><option>1</option><option>1.5</option><option>2</option></select></label></div>
<div id="csvHeaderField"><label>设备不发送表头时填写（可选）<input id="serialHeader" placeholder="time_s,input_v,output_rad_s"></label><p class="muted">文本 CSV 支持逗号、分号或 Tab 分隔，每条采样以换行结束。</p></div></details></fieldset>
<button class="primary" id="connectSource">连接并预览 <span>→</span></button></div>
<div class="card" id="mappingCard" hidden><div class="card-heading"><h2><span>02</span>核对输入与输出</h2></div><p class="muted">输入是施加给设备的信号（如电压），输出是测得的响应（如转速）。第一次使用时请核对。</p>
<fieldset id="channelSettings"><div class="two-cols"><label>输入信号<select id="captureInput"></select></label><label>输出信号<select id="captureOutput"></select></label></div>
<details id="channelDetails"><summary>通道名称、单位与采样时间</summary><div class="two-cols"><label>输入名称<input id="inputName" placeholder="例如 电压"></label><label>输入单位<input id="inputUnit" placeholder="例如 V"></label><label>输出名称<input id="outputName" placeholder="例如 转速"></label><label>输出单位<input id="outputUnit" placeholder="例如 rad/s"></label><label>输入倍率<input id="inputScale" type="number" value="1" step="any"></label><label>输出倍率<input id="outputScale" type="number" value="1" step="any"></label></div>
<label>设备时间列<select id="captureTime"></select></label><div class="two-cols"><label>时间单位<select id="timeScale"><option value="1">秒 s</option><option value="0.001">毫秒 ms</option><option value="0.000001">微秒 μs</option></select></label><label>固定采样周期 / 秒<input id="captureTs" type="number" value="0.01" min="0" step="any"></label></div>
<label class="plain-check"><input id="autoCaptureTime" type="checkbox" checked>从设备时间戳自动识别周期</label><p class="muted">没有时间列时，请填写设备实际固定采样周期。电脑收到数据的快慢不代表设备采样周期。</p></details>
<label>辨识算法<select id="captureAlgorithm"><option value="rls">RLS · 递推最小二乘</option><option value="frequency">窗口频域 · Welch H1</option></select></label>
<div id="captureFrequencySettings" hidden><p class="muted">收满窗口后自动选阶；之后定期更新参数。适用于稳定线性系统，输入应准确且与输出噪声不相关。</p><label>频谱窗函数<select id="captureWindow"><option value="hann">Hann（推荐）</option><option value="hamming">Hamming</option><option value="boxcar">矩形窗（完整同步周期）</option></select></label><label>频谱分段点数<input id="captureSegment" type="number" min="64" max="2048" step="64" value="256"></label><label>滑动窗口点数<input id="captureWindowSamples" type="number" min="512" max="16384" value="2048" step="256"></label><label>每增加多少点更新模型<input id="captureUpdateEvery" type="number" min="128" max="8192" value="512" step="128"></label><p id="captureFrequencyHint" class="muted"></p></div>
<label class="plain-check"><input id="mappingConfirmed" type="checkbox">已核对输入、输出和采样时间</label></fieldset>
<p class="sampling-note" id="samplingNote">等待采样预览</p>
<details><summary>保存为设备模板，下次直接使用</summary><label>模板名称<input id="templateName" placeholder="例如 电机试验台"></label><button class="secondary wide" id="saveDeviceTemplate">保存设备模板</button><p class="muted" id="templateFeedback"></p></details></div>
<div class="card start-card"><div class="card-heading"><h2><span>03</span>开始自动建模</h2></div><p class="muted">自动收集初始样本、选择阶次与延迟，再持续更新模型。可选择辨识算法，阶数自动确定。</p><button class="primary" id="startCapture" disabled>开始自动建模 <span>▶</span></button><button class="secondary wide" id="stopCapture" disabled>停止并保存实验</button><a id="captureDownload" class="download-link" hidden>下载完整实验 ZIP ↓</a><p class="muted" id="saveLocation">实验含采样 CSV、模型 JSON 和说明报告。</p></div>`;
settingsHost.prepend(wizard);
const advancedButton=document.createElement('button');advancedButton.id='toggleAdvanced';advancedButton.className='text-button advanced-toggle';advancedButton.textContent='高级 / 开发者接入（CSV 回放、HTTP）';advancedButton.hidden=true;settingsHost.append(advancedButton);
const monitor=document.createElement('div');monitor.id='captureMonitor';monitor.className='card capture-monitor';monitor.hidden=true;
monitor.innerHTML=`<div class="card-heading"><div><h2>采样预览与建模进度</h2><p id="sourceDescription">连接设备，先确认信号再建模</p></div><span class="connection-badge" id="captureBadge">未连接</span></div>
<div class="capture-steps"><span id="stepConnect">① 连接采样</span><span id="stepPreview">② 核对信号</span><span id="stepModel">③ 自动建模</span></div>
<p id="captureProgress" role="status" aria-live="polite">没有设备也可以先点击“一键体验演示”。</p><progress id="calibrationProgress" max="400" value="0"></progress>
<div class="signal-values"><div><span id="inputReadoutLabel">输入信号</span><strong id="inputReadout">—</strong></div><div><span id="outputReadoutLabel">输出响应</span><strong id="outputReadout">—</strong></div><div><span>已接收 / 本次已保存</span><strong id="captureCount">0 / 0</strong></div></div>
<div class="preview-plots"><div><div class="legend" id="inputPreviewLegend"></div><div class="chart" id="inputPreviewChart"></div></div><div><div class="legend" id="outputPreviewLegend"></div><div class="chart" id="outputPreviewChart"></div></div></div><p class="muted" id="captureHealth">实时预览最近 120 点，开始建模后记录完整实验。</p><details id="binaryReceiveDetails" hidden><summary>二进制接收诊断</summary><p class="muted" id="binaryReceiveStats"></p><p class="muted">最近收到的字节（十六进制）</p><pre class="decoder-output" id="binaryReceivedHex"></pre></details>`;
document.querySelector('.results-area').prepend(monitor);

const binarySizes={float64:8,float32:4,uint8:1,int8:1,uint16:2,int16:2,uint32:4,int32:4,uint64:8,int64:8};
function decoderFields(){
  const binary=$('sourceKind').value==='serial'&&$('serialFormat').value!=='csv',custom=$('serialFormat').value==='custom';
  $('binarySettings').hidden=!binary;$('binaryPresetFields').hidden=custom;$('binaryCustomFields').hidden=!custom;
  $('textEncodingField').hidden=binary;$('csvHeaderField').hidden=binary||$('sourceKind').value!=='serial';
  const columns=$('binaryColumns').value.split(',').filter(c=>c.trim());
  const size=custom?Number($('binaryPayload').value):columns.length*binarySizes[$('binaryType').value];
  $('binaryFrameSummary').textContent=`每帧载荷 ${size} 字节${custom?'':` · ${columns.length} 个通道`}；一帧对应一个采样点。`;
  $('binaryAlignmentNote').textContent=$('binaryHeader').value.trim()?'连接时寻找第一个帧头；接收后如帧结构错误，将停止并保留此前有效采样。':'无帧头时，先连接软件，再让设备从完整一帧的起点开始发送；持续字节流无法自动判断边界。';
}
function sourceFields(){ $('serialSource').hidden=$('sourceKind').value!=='serial';$('fileSource').hidden=$('sourceKind').value!=='file';decoderFields(); }
function selectedDecoder(){return {mode:$('serialFormat').value==='custom'?'custom':'preset',dtype:$('binaryType').value,
  columns:$('binaryColumns').value.split(',').map(c=>c.trim()),byte_order:$('binaryEndian').value,
  payload_bytes:Number($('binaryPayload').value),code:$('decoderCode').value,header_hex:$('binaryHeader').value,trailer_hex:$('binaryTrailer').value};}
function selectedSource(){const source={kind:$('sourceKind').value,port:$('serialPort').value,path:$('capturePath').value,
  baudrate:Number($('serialBaud').value),encoding:$('streamEncoding').value,header:$('serialHeader').value,
  bytesize:Number($('serialBits').value),parity:$('serialParity').value,stopbits:Number($('serialStop').value)};
  if(source.kind==='serial'){source.format=$('serialFormat').value==='csv'?'csv':'binary';if(source.format==='binary')source.decoder=selectedDecoder();}return source;}
function selectedChannels(){return {input:$('captureInput').value,output:$('captureOutput').value,time:$('captureTime').value,
  input_name:$('inputName').value,output_name:$('outputName').value,input_unit:$('inputUnit').value,output_unit:$('outputUnit').value,
  input_scale:Number($('inputScale').value),output_scale:Number($('outputScale').value),time_scale:Number($('timeScale').value),
  sample_time:Number($('captureTs').value),auto_time:$('autoCaptureTime').checked,
  algorithm:$('captureAlgorithm').value,frequency:{window:$('captureWindow').value,segment_length:Number($('captureSegment').value),window_samples:Number($('captureWindowSamples').value),update_every:Number($('captureUpdateEvery').value)}};}
function setChannels(c){for(const [key,id] of Object.entries({input:'captureInput',output:'captureOutput',time:'captureTime',
  input_name:'inputName',output_name:'outputName',input_unit:'inputUnit',output_unit:'outputUnit',input_scale:'inputScale',output_scale:'outputScale',time_scale:'timeScale',sample_time:'captureTs'})){
  if(c[key]!==undefined)$(id).value=c[key];
} $('autoCaptureTime').checked=c.auto_time!==false;
  $('captureAlgorithm').value=c.algorithm||'rls';
  $('captureWindow').value=c.frequency?.window||'hann';
  $('captureSegment').value=c.frequency?.segment_length||256;
  $('captureWindowSamples').value=c.frequency?.window_samples||2048;
  $('captureUpdateEvery').value=c.frequency?.update_every||512;
  $('captureFrequencySettings').hidden=$('captureAlgorithm').value!=='frequency';
}
function acquisitionControls(){
  const a=acquisition.current,active=a&&!a.finished,recording=!!a?.channels,busy=acquisition.busy||state.busy;
  $('sourceSettings').disabled=busy||active;$('demoStart').disabled=busy||active;$('connectSource').disabled=busy||active;
  $('channelSettings').disabled=busy||recording||!active;$('startCapture').disabled=busy||!active||recording||a.phase!=='preview'||!a.preview.length||!$('mappingConfirmed').checked;
  $('stopCapture').disabled=busy||!active||a.phase==='stopping';$('stopCapture').textContent=recording?'停止并保存实验':'断开预览连接';
  $('saveDeviceTemplate').disabled=busy||!a?.preview.length||!$('templateName').value.trim();
  $('toggleAdvanced').disabled=busy||!!state.session;
}
async function acquisitionAction(fn){await guarded(async()=>{acquisition.busy=true;acquisitionControls();try{await fn();}finally{acquisition.busy=false;acquisitionControls();}});}
async function refreshPorts(){
  const ports=await api('/api/acquisition/ports'),selected=$('serialPort').value;$('serialPort').replaceChildren();
  if(!ports.length)$('serialPort').add(new Option('未发现串口 · 请插入设备后刷新',''));
  ports.forEach(p=>$('serialPort').add(new Option(p.label,p.port)));
  if(ports.some(p=>p.port===selected))$('serialPort').value=selected;
}
async function refreshTemplates(){
  acquisition.templates=await api('/api/acquisition/templates');const selected=$('deviceTemplate').value;
  $('deviceTemplate').replaceChildren(new Option('首次使用 · 配置一次即可保存',''));
  acquisition.templates.forEach(t=>$('deviceTemplate').add(new Option(t.name,t.name)));$('deviceTemplate').value=selected;
}
function applyTemplate(t){
  if(!t)return;const s=t.source;acquisition.preferred=t.channels;
  $('sourceKind').value=s.kind==='demo'?'serial':s.kind;
  for(const [key,id] of Object.entries({port:'serialPort',path:'capturePath',baudrate:'serialBaud',encoding:'streamEncoding',header:'serialHeader',bytesize:'serialBits',parity:'serialParity',stopbits:'serialStop'})){
    if(s[key]!==undefined)$(id).value=s[key];
  }
  $('serialFormat').value=s.format==='binary'?(s.decoder?.mode==='custom'?'custom':'binary'):'csv';
  const d=s.decoder||{};
  for(const [key,id] of Object.entries({dtype:'binaryType',byte_order:'binaryEndian',payload_bytes:'binaryPayload',code:'decoderCode',header_hex:'binaryHeader',trailer_hex:'binaryTrailer'})){
    if(d[key]!==undefined)$(id).value=d[key];
  }
  if(d.columns)$('binaryColumns').value=d.columns.join(',');
  $('captureFilename').textContent=s.path||'仅接收新增行；历史末尾数据用于预览。';
  $('templateName').value=t.name;sourceFields();
}
function drawAcquisition(a){
  if(a&&a.id!==acquisition.current?.id){
    $('orderDetails').open=false;$('captureDownload').hidden=true;
    $('saveLocation').textContent='实验含采样 CSV、模型 JSON 和说明报告。';
    $('templateFeedback').textContent='';
    if(a.source.kind!=='demo')applyTemplate({source:a.source,channels:a.channels||acquisition.preferred||{},name:$('templateName').value});
  }
  acquisition.current=a;const visible=state.mode==='online'&&!acquisition.advanced;
  if(!visible)return;
  if(!a){acquisitionControls();return;}
  const columns=a.columns,key=a.id+JSON.stringify(columns);
  if(key!==acquisition.columnKey&&columns.length){
    acquisition.columnKey=key;const c=a.channels||acquisition.preferred||{};
    for(const id of ['captureInput','captureOutput','captureTime']){
      $(id).replaceChildren();if(id==='captureTime')$(id).add(new Option('无时间列 · 使用固定周期',''));
      columns.forEach(name=>$(id).add(new Option(name,name)));
    }
    const timeCol=columns.find(c=>/^(t|time|time_s|time_ms|timestamp)$/i.test(c))||'';
    const signals=columns.filter(c=>c!==timeCol);
    setChannels({input:signals[0],output:signals.at(-1),time:timeCol,time_scale:/ms$/i.test(timeCol)?.001:1,
      input_name:'',output_name:'',input_unit:'',output_unit:'',input_scale:1,output_scale:1,sample_time:.01,auto_time:true,...c});
    $('mappingConfirmed').checked=a.source.kind==='demo';
  }
  $('mappingCard').hidden=!columns.length;
  const c=a.channels||selectedChannels(),last=a.preview.at(-1);
  $('captureFrequencySettings').hidden=$('captureAlgorithm').value!=='frequency';
  const diagnostics=a.decoder_diagnostics;
  $('binaryReceiveDetails').hidden=!diagnostics;
  if(diagnostics){$('binaryReceiveStats').textContent=`收到 ${diagnostics.received_bytes} 字节 · 解码 ${diagnostics.decoded_frames} 帧 · 等待拼接 ${diagnostics.pending_bytes} 字节 · 初始对齐跳过 ${diagnostics.discarded_bytes} 字节 · 每帧 ${diagnostics.frame_bytes} 字节`;$('binaryReceivedHex').textContent=diagnostics.raw_hex||'等待设备发送数据';}
  const label=(which)=>`${c[which+'_name']||c[which]||which}${c[which+'_unit']?' / '+c[which+'_unit']:''}`;
  $('inputReadoutLabel').textContent='输入 · '+label('input');$('outputReadoutLabel').textContent='输出 · '+label('output');
  $('inputReadout').textContent=last?fmt(last[c.input]*c.input_scale):'—';$('outputReadout').textContent=last?fmt(last[c.output]*c.output_scale):'—';
  const dt=a.preview.length>1&&c.time?(a.preview.at(-1)[c.time]-a.preview.at(-2)[c.time])*c.time_scale:c.sample_time;
  $('samplingNote').textContent=c.time?`设备时间间隔约 ${fmt(dt)} 秒 · 开始前自动检查连续性`:`使用已填写的设备固定周期 ${fmt(c.sample_time)} 秒；无时间戳时无法检查设备丢样。`;
  $('captureFrequencyHint').textContent=`首次等待约 ${fmt(Number($('captureWindowSamples').value)*(c.auto_time&&c.time?dt:c.sample_time))} 秒采样；窗口至少为分段点数的 4 倍，频谱采用 50% 重叠。`;
  const rows=a.preview.map((row,i)=>({...row,time:c.time?row[c.time]*c.time_scale:i*c.sample_time}));
  chart('inputPreviewChart','inputPreviewLegend',rows,[{name:label('input'),get:r=>r[c.input]*c.input_scale,color:'#14a394'}]);
  chart('outputPreviewChart','outputPreviewLegend',rows,[{name:label('output'),get:r=>r[c.output]*c.output_scale,color:'#2369db'}]);
  $('inputPreviewChart').querySelector('svg')?.setAttribute('aria-label','输入采样信号随时间的变化');
  $('outputPreviewChart').querySelector('svg')?.setAttribute('aria-label','输出采样信号随时间的变化');
  $('captureCount').textContent=`${a.received.toLocaleString()} / ${a.recorded.toLocaleString()}`;
  $('sourceDescription').textContent={demo:'仿真演示 · 100 Hz · 非真实设备',serial:`USB 串口 · ${a.source.port||''}`,file:'跟随 CSV · '+(a.source.path||'')}[a.source.kind];
  const pending=a.result?.order_selection?.status==='collecting';
  let message='已连接，等待采样。请确认采样设备正在发送数据。',badge='等待数据';
  if(diagnostics?.received_bytes&&!a.preview.length){message=`已收到 ${diagnostics.received_bytes} 字节，尚未得到完整采样。请展开二进制接收诊断，核对帧头、帧长度与发送格式。`;badge='等待完整帧';}
  if(a.preview.length){message='已收到采样，请核对输入、输出和时间，再点击“开始自动建模”。';badge='接收正常';}
  if(a.channels){
    if(pending){const n=a.result.order_selection;message=`正在收集初始样本：${a.fitted} / ${n.target_samples} 点。${n.message?'当前数据暂不能定阶，将继续收集：'+n.message:'软件将自动选择阶次与延迟。'}`;badge='收集 / 自动定阶';}
    else{message=a.result?.algorithm==='frequency'?`窗口模型已更新 ${a.result.updates} 次；下次更新第 ${a.result.frequency.next_update_at} 点。${a.result.frequency.last_error?'本窗口未更新，保留上次模型：'+a.result.frequency.last_error:''}`:'模型已建立，正在用新采样持续更新参数。下方曲线显示测量值与更新前的预测。';badge='更新模型';}
    if(pending&&a.accepted>=a.result.order_selection.target_samples&&a.fitted<a.accepted){message='正在比较候选模型，后台继续接收并记录采样…';badge='自动选择模型';}
  }
  if(a.sample_age>3&&!a.finished&&a.phase!=='stopping'){message+=` 已有 ${Math.floor(a.sample_age)} 秒未收到新采样，请检查设备或文件是否仍在更新。`;badge='等待新采样';}
  if(a.phase==='stopping'){message='正在结束采集、处理已接收数据并保存实验，请稍候…';badge='保存中';}
  if(a.finished){message=a.error||`本次采集已结束，保存 ${a.recorded} 个采样点。${a.channels?'可下载完整实验。':'尚未开始建模。'}`;badge=a.error?'采集已停止':'已结束';}
  if(a.error)message=a.error+(a.finished?'':' 正在保存此前数据…');
  $('captureBadge').textContent=badge;$('captureBadge').classList.toggle('fault',!!a.error);
  $('captureProgress').textContent=message;
  $('calibrationProgress').max=a.result?.order_selection?.target_samples||a.result?.frequency?.window_samples||400;
  $('calibrationProgress').value=pending?a.fitted:(a.result?.parameters.length?$('calibrationProgress').max:0);
  $('stepConnect').classList.toggle('done',a.preview.length>0);$('stepPreview').classList.toggle('done',!!a.channels);$('stepModel').classList.toggle('done',!!a.result?.parameters.length);
  $('captureHealth').textContent=`预览最近 120 点 · 待处理 ${a.queued} 点 · 已用于建模 ${a.fitted} 点${a.channels?' · 开始建模后的新数据完整记入实验文件':''}${c.algorithm==='frequency'?' · 窗口频域辨识':''}`;
  if(a.result){render({...a.result,session_id:a.id,history_mode:'replace'});$('sessionCard').hidden=true;$('metric3Note').textContent=a.source.kind==='demo'?'仿真采样数据':'实际接收的采样';}
  if(a.download){$('captureDownload').hidden=false;$('captureDownload').href=a.download;$('captureDownload').download=`experiment-${a.id.slice(0,8)}.zip`;$('saveLocation').textContent=(window.paramidDataDirectory ? '已自动保存至 ' + window.paramidDataDirectory + '\\acquisitions；也可另存完整 ZIP。' : '已自动保存至本机 outputs/acquisitions；也可下载完整 ZIP。');}
  if(acquisition.downloadAfterStop&&a.finished){acquisition.downloadAfterStop=false;if(a.download)$('captureDownload').click();}
  acquisitionControls();
}
async function acquisitionPoll(){
  clearTimeout(acquisition.timer);
  if(state.mode!=='online'||acquisition.advanced)return;
  try{const a=await api('/api/acquisition');if(state.mode==='online'&&!acquisition.advanced)drawAcquisition(a);}
  catch(error){status('采集状态读取失败：'+error.message+'。请检查本地服务。',true);}
  if(state.mode==='online'&&!acquisition.advanced)acquisition.timer=setTimeout(acquisitionPoll,500);
}
async function stopAcquisition(wait=false){
  const a=acquisition.current;if(!a||a.finished)return;
  drawAcquisition(await api('/api/acquisition/stop',{id:a.id}));
  if(wait){
    const deadline=Date.now()+60000;
    while(!acquisition.current.finished){
      if(Date.now()>deadline)throw new Error('实验仍在保存，请稍候再切换。');
      await new Promise(resolve=>setTimeout(resolve,300));drawAcquisition(await api('/api/acquisition'));
    }
  }
}
window.leaveAcquisition=async()=>{clearTimeout(acquisition.timer);await stopAcquisition(true);};
window.showAcquisition=async()=>{
  const online=state.mode==='online';document.body.classList.toggle('beginner-online',online&&!acquisition.advanced);
  $('acquisitionWizard').hidden=!online||acquisition.advanced;$('legacySettings').hidden=online&&!acquisition.advanced;
  $('captureMonitor').hidden=!online||acquisition.advanced;$('toggleAdvanced').hidden=!online;
  if(online&&!acquisition.advanced){
    $('pageSubtitle').textContent='连接采样 → 核对信号 → 自动建立模型';
    status('选择设备并预览采样；没有硬件时，可先用一键演示体验完整流程。');
    await acquisitionPoll();
  }
};
$('sourceKind').addEventListener('change',()=>{acquisition.preferred=null;$('deviceTemplate').value='';sourceFields();});
function resetDecoderPreview(){ $('decoderFeedback').textContent='';$('decoderPreview').hidden=true; }
$('serialFormat').addEventListener('change',()=>{decoderFields();resetDecoderPreview();});
$('binarySettings').addEventListener('input',()=>{decoderFields();resetDecoderPreview();});
$('loadDecoderExample').addEventListener('click',()=>{
  const kind=$('decoderExample').value,custom=['mixed','bits','checksum'].includes(kind);
  $('serialFormat').value=custom?'custom':'binary';$('binaryType').value='float64';$('binaryEndian').value='little';
  $('binaryColumns').value=kind==='timed'?'time_s,input1,output1':'input1,output1';
  $('binaryHeader').value=custom?'AA 55':'';$('binaryTrailer').value=custom?'0D 0A':'';
  let hex='00 00 00 00 00 00 F0 3F 00 00 00 00 00 00 00 40';
  if(kind==='timed')hex='00 00 00 00 00 00 00 00 '+hex;
  if(kind==='mixed'){$('binaryPayload').value=10;$('decoderCode').value='time_s = u32(0) * 0.001\ninput1 = f32(4)\noutput1 = i16(8) * 0.01';hex='AA 55 0A 00 00 00 00 00 C0 3F 38 FF 0D 0A';}
  if(kind==='bits'){$('binaryPayload').value=2;$('decoderCode').value='input1 = bits(0, 6)\noutput1 = bits(6, 6)';hex='AA 55 91 0A 0D 0A';}
  if(kind==='checksum'){$('binaryPayload').value=3;$('decoderCode').value='assert sum8(0, 2) == u8(2)\ninput1 = u8(0)\noutput1 = u8(1)';hex='AA 55 01 02 03 0D 0A';}
  $('decoderHex').value=hex;decoderFields();resetDecoderPreview();
});
$('testDecoder').addEventListener('click',()=>acquisitionAction(async()=>{
  resetDecoderPreview();
  try{
    const result=await api('/api/acquisition/decode-preview',{decoder:selectedDecoder(),hex:$('decoderHex').value});
    $('decoderFeedback').textContent=result.error?`解码失败：${result.error}`:`已解码 ${result.decoded_frames} 帧 · 每帧 ${result.frame_bytes} 字节 · 剩余 ${result.pending_bytes} 字节待拼接 · 初始跳过 ${result.discarded_bytes} 字节。${result.decoded_frames?'请核对下方数值与设备一致。':'请检查帧头及数据长度。'}`;
    $('decoderPreview').hidden=!result.rows.length;$('decoderPreview').textContent=JSON.stringify(result.rows,null,2);
    status(result.error?`试解码失败：${result.error}`:(result.decoded_frames?'试解码完成，请核对预览值后连接设备。':'尚未解码出完整帧，请核对帧头和字节数量。'),!!result.error);
  }catch(error){$('decoderFeedback').textContent=error.message;throw error;}
}));
$('refreshPorts').addEventListener('click',()=>acquisitionAction(refreshPorts));
$('chooseCaptureFile').addEventListener('click',()=>acquisitionAction(async()=>{
  status('请在桌面的文件选择窗口中，选择采样软件正在写入的文件。');
  const picked=await api('/api/acquisition/pick-file',{});if(picked.path){$('capturePath').value=picked.path;$('captureFilename').textContent=picked.path;status('文件已选择，点击“连接并预览”接收新增采样。');}
}));
$('capturePath').addEventListener('input',()=>$('captureFilename').textContent=$('capturePath').value);
$('connectSource').addEventListener('click',()=>acquisitionAction(async()=>{
  const a=await api('/api/acquisition/connect',selectedSource());invalidate();$('captureDownload').hidden=true;drawAcquisition(a);
  status('已连接。先核对预览信号，再开始自动建模。');
}));
$('demoStart').addEventListener('click',()=>acquisitionAction(async()=>{
  acquisition.preferred={input:'input_v',output:'output_rad_s',time:'time_s',input_name:'模拟电压',output_name:'模拟转速',input_unit:'V',output_unit:'rad/s',sample_time:.01,time_scale:1,input_scale:1,output_scale:1};
  let a=await api('/api/acquisition/connect',{kind:'demo'});invalidate();$('captureDownload').hidden=true;drawAcquisition(a);
  for(let i=0;i<40&&!a.preview.length;i++){await new Promise(resolve=>setTimeout(resolve,100));a=await api('/api/acquisition');drawAcquisition(a);}
  drawAcquisition(await api('/api/acquisition/start',{id:a.id,channels:acquisition.preferred}));
  status('正在接收仿真采样。软件将自动建模，无需配置阶次或运行脚本。');
}));
$('startCapture').addEventListener('click',()=>acquisitionAction(async()=>{
  drawAcquisition(await api('/api/acquisition/start',{id:acquisition.current.id,channels:selectedChannels()}));
  status('已开始记录新采样并自动建模，完成初始定阶后显示在线预测。');
}));
$('stopCapture').addEventListener('click',()=>acquisitionAction(async()=>{acquisition.downloadAfterStop=!!acquisition.current?.channels;await stopAcquisition();}));
$('channelSettings').addEventListener('input',event=>{if(event.target.id!=='mappingConfirmed')$('mappingConfirmed').checked=false;drawAcquisition(acquisition.current);});
$('templateName').addEventListener('input',acquisitionControls);
$('saveDeviceTemplate').addEventListener('click',()=>acquisitionAction(async()=>{
  await api('/api/acquisition/template',{id:acquisition.current.id,name:$('templateName').value,channels:selectedChannels()});
  await refreshTemplates();$('deviceTemplate').value=$('templateName').value;
  try{localStorage.setItem('paramid-template',$('templateName').value);}catch{}
  $('templateFeedback').textContent='已保存。下次选择此模板即可恢复连接和通道设置。';
}));
$('deviceTemplate').addEventListener('change',()=>{acquisition.preferred=null;applyTemplate(acquisition.templates.find(t=>t.name===$('deviceTemplate').value));});
$('toggleAdvanced').addEventListener('click',()=>acquisitionAction(async()=>{
  await window.leaveAcquisition();acquisition.advanced=!acquisition.advanced;invalidate();
  $('toggleAdvanced').textContent=acquisition.advanced?'返回简单采集向导':'高级 / 开发者接入（CSV 回放、HTTP）';await window.showAcquisition();
  if(acquisition.advanced)status('高级接入：可回放 CSV，或通过 HTTP 接入已有采集程序。');
}));
window.addEventListener('beforeunload',event=>{if(acquisition.current&&!acquisition.current.finished){event.preventDefault();event.returnValue='';}});
Promise.allSettled([refreshPorts(),refreshTemplates()]).then(results=>{
  results.forEach(r=>{if(r.status==='rejected')status(r.reason.message,true);});
  try{const name=localStorage.getItem('paramid-template');const t=acquisition.templates.find(t=>t.name===name);if(t){$('deviceTemplate').value=name;applyTemplate(t);}}catch{}
});
sourceFields();acquisitionControls();
