/** Real browser/API workflow verification using isolated headless Chrome + CDP.
 * No npm packages or access to the user's normal browser profile are needed.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdir, readFile, writeFile, appendFile, mkdtemp, readdir} from 'node:fs/promises';
import path from 'node:path';
import {existsSync} from 'node:fs';
import os from 'node:os';
import {fileURLToPath} from 'node:url';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.join(root,'outputs','ui_test');
await mkdir(output,{recursive:true});
const downloads=await mkdtemp(path.join(output,'downloads-'));
const profile=await mkdtemp(path.join(os.tmpdir(),'paramid-browser-'));
const localPython=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const python=process.env.PARAMID_PYTHON||(existsSync(localPython)?localPython:'python');
const browserCandidates=process.platform==='win32'
  ? [process.env.PROGRAMFILES,process.env['PROGRAMFILES(X86)'],process.env.LOCALAPPDATA]
      .filter(Boolean).flatMap(base=>[
        path.join(base,'Google','Chrome','Application','chrome.exe'),
        path.join(base,'Microsoft','Edge','Application','msedge.exe')])
  : ['/usr/bin/google-chrome','/usr/bin/chromium','/usr/bin/chromium-browser',
     '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
const browserPath=process.env.PARAMID_BROWSER||browserCandidates.find(existsSync);
if(!browserPath)throw new Error('Chrome/Edge not found. Set PARAMID_BROWSER to the browser executable.');
if(typeof WebSocket==='undefined')throw new Error('UI tests require Node.js 22 or newer.');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let service,browser,ws,base,session,id=0;
const pending=new Map(),jsErrors=[];

async function until(fn,timeout=30000){const deadline=Date.now()+timeout;while(Date.now()<deadline){const value=await fn();if(value)return value;await pause(70);}throw new Error('Timed out waiting for browser condition');}
function command(method,params={},sessionId=session){
  const current=++id;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(current);reject(new Error('CDP timeout: '+method));},30000);
    pending.set(current,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});
    ws.send(JSON.stringify({id:current,method,params,...(sessionId?{sessionId}:{})}));
  });
}
async function evaluate(expression){
  const result=await command('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function click(id){await until(()=>evaluate(`!document.getElementById('${id}').disabled&&document.getElementById('${id}').getClientRects().length>0`));await evaluate(`document.getElementById('${id}').click()`);}
async function switchMode(mode){
  await until(()=>evaluate(`!document.querySelector('[data-mode="${mode}"]').disabled`));
  await evaluate(`document.querySelector('[data-mode="${mode}"]').click()`);
  const title=mode==='offline'?'离线参数辨识':'在线参数辨识';
  await until(()=>evaluate(`document.getElementById('pageTitle').textContent==='${title}'&&!document.getElementById('configuration').disabled`));
  if(mode==='online'&&await evaluate('!acquisition.advanced'))await click('toggleAdvanced');
}
async function upload(selector,filename){
  const {root:document}=await command('DOM.getDocument');
  const {nodeId}=await command('DOM.querySelector',{nodeId:document.nodeId,selector});
  await command('DOM.setFileInputFiles',{nodeId,files:[filename]});
}
async function screenshot(name){const result=await command('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});await writeFile(path.join(output,name),Buffer.from(result.data,'base64'));}
async function screenshotElement(elementId,name){
  const clip=await evaluate(`(()=>{const r=document.getElementById('${elementId}').getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1}})()`);
  const result=await command('Page.captureScreenshot',{format:'png',clip,captureBeyondViewport:true});await writeFile(path.join(output,name),Buffer.from(result.data,'base64'));
}

try{
  service=spawn(python,['app.py','--port','0','--capture-dir',path.join(downloads,'acquisitions')],{cwd:root,windowsHide:true});
  let serviceLog='';service.stdout.on('data',data=>serviceLog+=data);service.stderr.on('data',data=>serviceLog+=data);
  service.on('error',error=>{serviceLog+=error.message;});
  base=await until(()=>serviceLog.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]);
  browser=spawn(browserPath,['--headless=new','--disable-gpu','--remote-debugging-port=0','--no-first-run',
    '--no-default-browser-check','--disable-extensions','--disable-background-networking','--disable-component-update',
    '--disable-sync',`--user-data-dir=${profile}`,'about:blank'],{windowsHide:true});
  let browserLog='';browser.stderr.on('data',data=>browserLog+=data);
  const endpoint=await until(()=>browserLog.match(/ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[\w-]+/)?.[0]);
  ws=new WebSocket(endpoint);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=event=>{const data=JSON.parse(event.data);if(data.id&&pending.has(data.id)){
    const promise=pending.get(data.id);pending.delete(data.id);data.error?promise.reject(new Error(JSON.stringify(data.error))):promise.resolve(data.result);
  }if(data.method==='Runtime.exceptionThrown')jsErrors.push(data.params.exceptionDetails);};
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads},null);
  const {targetId}=await command('Target.createTarget',{url:base},null);
  ({sessionId:session}=await command('Target.attachToTarget',{targetId,flatten:true},null));
  await command('Runtime.enable');await command('Page.enable');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await until(()=>evaluate('!!document.getElementById("regressionDemo")'));
  // The user's workflow: import shuffled input*/output* headers, with no manual
  // channel mapping; fit, switch output, inspect every path and export all data.
  await upload('#dataFile',path.join(root,'examples','multichannel.csv'));
  await until(()=>evaluate('state.rows.length===1000&&!state.busy'));
  assert.equal(await evaluate('document.getElementById("modelKind").value'),'arx');
  assert.equal(await evaluate('document.querySelectorAll("#modelKind option").length'),2);
  assert.equal(await evaluate('document.getElementById("regressionDemo").parentElement.children.length'),2);
  assert.ok((await evaluate('document.getElementById("excitationAdvice").textContent')).includes('周期脉冲'));
  assert.ok((await evaluate('document.getElementById("channelSummary").textContent')).includes('2 路输入 → 2 路输出'));
  assert.equal(await evaluate('Number(document.getElementById("sampleTime").value)'),.02);
  assert.ok(await evaluate('document.getElementById("manualMapping").hidden'));
  await click('fitButton');await until(()=>evaluate('state.result?.model.kind==="multichannel"&&!state.busy'),90000);
  assert.equal(await evaluate('document.querySelectorAll("#multiOutput option").length'),2);
  assert.equal(await evaluate('document.querySelectorAll("#transferMatrix math").length'),8);
  assert.ok(await evaluate('state.result.output_results.every(o=>o.metrics.fit_percent>98)'));
  await evaluate('document.getElementById("multiOutput").value="1";document.getElementById("multiOutput").dispatchEvent(new Event("change"))');
  assert.ok((await evaluate('document.getElementById("chartTitle").textContent')).includes('output2'));
  assert.equal(await evaluate('document.querySelectorAll("#responseChart path").length'),2);
  const multiDownloads=path.join(downloads,'multichannel');await mkdir(multiDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:multiDownloads},null);
  for(const id of ['exportModel','exportCSV','exportReport','exportMathModel'])await click(id);
  await until(async()=> (await readdir(multiDownloads)).filter(name=>!name.endsWith('.crdownload')).length===4);
  const multiModel=JSON.parse(await readFile(path.join(multiDownloads,'parameter_model.json'),'utf8'));
  assert.deepEqual(multiModel.model.inputs,['input1','input2']);
  assert.ok((await readFile(path.join(multiDownloads,'validation.csv'),'utf8')).includes('output2_free_run'));
  assert.ok((await readFile(path.join(multiDownloads,'辨识数学模型.txt'),'utf8')).includes('input2 → output1'));
  await screenshot('multichannel.png');await screenshotElement('mathModelCard','multichannel_models.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'multichannel mobile layout overflows');
  await screenshot('multichannel_mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads},null);
  console.log('PASS numbered-header auto detection, coupled MIMO fitting, output switch, all transfer paths and four exports');
  // Removed preprocessing has no UI control, request setting or export branch.
  assert.equal(await evaluate('document.getElementById("difference")'),null);
  assert.equal(await evaluate('document.getElementById("differenceField")'),null);
  assert.equal(await evaluate('"preprocessing" in state.result'),false);
  const originalMulti=(await readFile(path.join(root,'examples','multichannel.csv'),'utf8')).trim().split(/\r?\n/);
  assert.equal(await evaluate('state.result.output_results[0].series.at(-1).measured'),Number(originalMulti.at(-1).split(',')[4]));
  const noHeaderFile=path.join(downloads,'headerless.csv');await writeFile(noHeaderFile,originalMulti.slice(1).join('\n'));
  await upload('#dataFile',noHeaderFile);await until(()=>evaluate('!state.hasHeader&&state.rows.length===1000&&!state.busy'));
  assert.equal(await evaluate('document.getElementById("channelMode").value'),'manual');
  assert.ok(await evaluate('document.getElementById("fitButton").disabled'));
  assert.equal(await evaluate('state.rows[0].column1'),originalMulti[1].split(',')[0]);
  await evaluate('document.getElementById("dynamicTimeColumn").value="column1";document.getElementById("dynamicTimeColumn").dispatchEvent(new Event("input",{bubbles:true}))');
  for(const [host,column] of [['dynamicInputs','column2'],['dynamicInputs','column4'],['dynamicOutputs','column3'],['dynamicOutputs','column5']]){
    await evaluate(`(()=>{const box=document.querySelector('#${host} input[value="${column}"]');box.checked=true;box.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  }
  assert.equal(await evaluate('Number(document.getElementById("sampleTime").value)'),.02);
  await click('fitButton');await until(()=>evaluate('state.result?.model.inputs[0]==="column2"&&!state.busy'),90000);
  assert.deepEqual(await evaluate('state.result.model.outputs'),['column3','column5']);
  assert.equal(await evaluate('state.result.output_results[1].series.at(-1).measured'),Number(originalMulti.at(-1).split(',')[4]));
  await evaluate('document.getElementById("dataPreview").open=true');
  await screenshot('headerless_mapping.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'headerless preview overflows mobile layout');
  await screenshot('headerless_mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  const originalDownloads=path.join(downloads,'headerless');await mkdir(originalDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:originalDownloads},null);
  await click('exportModel');await click('exportMathModel');
  await until(async()=>{const files=await readdir(originalDownloads);return files.includes('parameter_model.json')&&files.includes('辨识数学模型.txt');});
  assert.equal('preprocessing' in JSON.parse(await readFile(path.join(originalDownloads,'parameter_model.json'),'utf8')),false);
  assert.ok(!(await readFile(path.join(originalDownloads,'辨识数学模型.txt'),'utf8')).includes('Δu'));
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads},null);
  console.log('PASS two ARX/regression choices, pulse recommendation, headerless first sample, manual multichannel roles and original-signal fitting/exports; removed preprocessing absent');
  await click('regressionDemo');await until(()=>evaluate('!document.getElementById("fitButton").disabled'));
  await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  const offline=await evaluate('({fit:document.getElementById("metric2").textContent,parameters:[...document.querySelectorAll("#parameters tr")].map(row=>row.innerText),curves:document.querySelectorAll("#responseChart path").length})');
  assert.ok(parseFloat(offline.fit)>98);assert.equal(offline.curves,2);
  assert.ok(offline.parameters[0].includes('2.50'));
  await screenshot('offline.png');
  for(const id of ['exportModel','exportCSV','exportReport'])await click(id);
  await until(async()=> {const names=await readdir(downloads);return ['parameter_model.json','validation.csv','辨识报告.md'].every(name=>names.includes(name));});
  const model=JSON.parse(await readFile(path.join(downloads,'parameter_model.json'),'utf8'));
  assert.equal(model.algorithm,'ls');assert.equal(model.parameters.length,3);
  assert.ok((await readFile(path.join(downloads,'validation.csv'),'utf8')).includes('one_step_prediction'));
  assert.ok((await readFile(path.join(downloads,'辨识报告.md'),'utf8')).includes('参数辨识报告'));
  assert.ok(await evaluate('!!document.querySelector("#mathEquation math")'));
  assert.ok(await evaluate('document.getElementById("mathTransferBlock").hidden'));
  assert.ok((await evaluate('document.getElementById("mathSignals").textContent')).includes('x1'));
  assert.equal(model.mathematical_model.kind,'regression');
  assert.equal(model.mathematical_model.continuous_model.status,'not_applicable');
  assert.ok(await evaluate('document.getElementById("continuousBody").hidden'));
  assert.ok((await evaluate('document.getElementById("continuousUnavailable").textContent')).includes('未定义动态状态'));
  assert.ok((await readFile(path.join(downloads,'辨识报告.md'),'utf8')).includes(model.mathematical_model.equation_text));
  await click('exportMathModel');
  await until(async()=> (await readdir(downloads)).includes('辨识数学模型.txt'));
  assert.ok((await readFile(path.join(downloads,'辨识数学模型.txt'),'utf8')).includes(model.mathematical_model.equation_text));
  await screenshotElement('mathModelCard','math_regression.png');
  console.log('PASS offline regression, validation and three exports');

  const ordinaryParameters=await evaluate('state.result.parameters');
  await evaluate('document.getElementById("algorithm").value="wls";document.getElementById("algorithm").dispatchEvent(new Event("input",{bubbles:true}))');
  assert.equal(await evaluate('document.getElementById("weightColumn").value'),'');
  assert.ok((await evaluate('document.getElementById("weightColumn").selectedOptions[0].textContent')).includes('不加权'));
  await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  assert.deepEqual(await evaluate('state.result.parameters'),ordinaryParameters);
  assert.equal(await evaluate('document.getElementById("algorithmTag").textContent'),'LS');
  assert.ok((await evaluate('document.getElementById("status").textContent')).includes('未选择权重列'));
  await screenshot('optional_weights.png');
  const optionalDownloads=path.join(downloads,'optional_weights');
  await mkdir(optionalDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:optionalDownloads},null);
  await click('exportModel');await click('exportReport');
  await until(async()=> (await readdir(optionalDownloads)).includes('parameter_model.json')&&(await readdir(optionalDownloads)).includes('辨识报告.md'));
  const optionalModel=JSON.parse(await readFile(path.join(optionalDownloads,'parameter_model.json'),'utf8'));
  assert.equal(optionalModel.algorithm,'ls');assert.equal(optionalModel.settings.weight_column,null);
  assert.equal(optionalModel.settings.requested_algorithm,'wls');
  assert.ok((await readFile(path.join(optionalDownloads,'辨识报告.md'),'utf8')).includes('未选择权重列'));
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads},null);
  await evaluate('document.getElementById("weightColumn").value="x1";document.getElementById("weightColumn").dispatchEvent(new Event("input",{bubbles:true}))');
  await click('fitButton');await until(()=>evaluate('document.getElementById("status").classList.contains("error")'));
  assert.ok((await evaluate('document.getElementById("status").textContent')).includes('正权重'));
  await until(()=>evaluate('!document.getElementById("configuration").disabled'));
  await evaluate('document.getElementById("weightColumn").value="";document.getElementById("weightColumn").dispatchEvent(new Event("input",{bubbles:true}))');
  await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  console.log('PASS optional weights default to LS, identical parameters, truthful JSON/report, and invalid selected weights rejected');

  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile layout overflows');
  await screenshot('mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await switchMode('online');
  await click('replayButton');await until(()=>evaluate('Number(document.getElementById("metric3").textContent.replaceAll(",",""))>=50'));
  await click('pauseButton');
  const paused=await evaluate('document.getElementById("metric3").textContent');await pause(350);
  assert.equal(await evaluate('document.getElementById("metric3").textContent'),paused);
  await click('pauseButton');await until(()=>evaluate('document.getElementById("metric3").textContent==="900"'),30000);
  assert.equal(await evaluate('document.querySelectorAll("#parameterChart path").length'),3);
  await screenshot('online.png');await click('exportCSV');
  await until(async()=> (await readdir(downloads)).includes('online_recent_2000.csv'));
  assert.equal((await readFile(path.join(downloads,'online_recent_2000.csv'),'utf8')).trim().split('\n').length,901);
  console.log('PASS stream replay, pause/resume, parameter trajectories and online CSV');

  await click('endButton');await click('liveButton');
  await until(()=>evaluate('document.getElementById("sessionMode").textContent==="实时 HTTP 数据接入"&&!document.getElementById("endButton").disabled'));
  const liveId=await until(()=>evaluate('document.getElementById("sessionId").textContent'));
  const inspection=await (await fetch(base+'/api/inspect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv:await readFile(path.join(root,'examples','regression.csv'),'utf8')})})).json();
  const live=await (await fetch(base+'/api/session/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:liveId,sequence:0,samples:inspection.rows.slice(0,20)})})).json();
  assert.equal(live.next_sequence,20);
  await until(()=>evaluate('document.getElementById("metric3").textContent==="20"'));
  await click('endButton');console.log('PASS external HTTP stream reflected in live UI');

  await switchMode('offline');
  await click('arxDemo');await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  const ordinarySelection=await evaluate('state.result.order_selection');
  await evaluate('document.getElementById("algorithm").value="wls";document.getElementById("algorithm").dispatchEvent(new Event("input",{bubbles:true}))');
  assert.equal(await evaluate('document.getElementById("weightColumn").value'),'');
  await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  assert.equal(await evaluate('document.getElementById("algorithmTag").textContent'),'LS');
  assert.deepEqual(await evaluate('state.result.order_selection'),ordinarySelection);
  assert.ok(await evaluate('!document.getElementById("na")&&!document.getElementById("nb")&&!document.getElementById("nk")'),'manual order fields must be removed');
  assert.deepEqual(await evaluate('["selectedNa","selectedNb","selectedNk"].map(id=>document.getElementById(id).textContent)'),['1','1','1']);
  assert.ok((await evaluate('document.getElementById("orderSummary").textContent')).includes('704'));
  assert.ok((await evaluate('document.getElementById("chartCaption").textContent')).includes('选模'));
  await evaluate('document.getElementById("orderDetails").open=true');
  assert.equal(await evaluate('document.querySelectorAll("#orderCandidates tr").length'),12);
  assert.equal(await evaluate('document.querySelectorAll("#responseChart path").length'),3);
  assert.ok(parseFloat(await evaluate('document.getElementById("metric2").textContent'))>99);
  await screenshot('arx.png');
  const automaticDownloads=path.join(downloads,'automatic');
  await mkdir(automaticDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:automaticDownloads},null);
  await click('exportModel');
  await until(async()=> (await readdir(automaticDownloads)).includes('parameter_model.json'));
  const automaticModel=JSON.parse(await readFile(path.join(automaticDownloads,'parameter_model.json'),'utf8'));
  assert.equal(automaticModel.order_selection.candidate_count,704);
  assert.equal(automaticModel.order_selection.candidates.length,704);
  assert.equal(automaticModel.model.na,1);
  assert.ok(await evaluate('!document.getElementById("mathTransferBlock").hidden'));
  assert.equal(await evaluate('document.querySelectorAll("#mathTransfer mfrac").length'),1);
  assert.ok((await evaluate('document.getElementById("mathSampling").textContent')).includes('0.01 s'));
  assert.deepEqual(automaticModel.mathematical_model.transfer_function.numerator,[0,automaticModel.parameters[1].value]);
  assert.deepEqual(automaticModel.mathematical_model.transfer_function.denominator,[1,-automaticModel.parameters[0].value]);
  const converted=automaticModel.mathematical_model.continuous_model;
  assert.equal(converted.status,'available');assert.equal(converted.delay_samples,0);
  assert.ok(converted.roundtrip_relative_error<1e-8);
  assert.ok(Math.abs(converted.state_space.A[0][0]-Math.log(automaticModel.parameters[0].value)/.01)<1e-9);
  assert.ok(await evaluate('!document.getElementById("continuousBody").hidden'));
  assert.equal(await evaluate('document.querySelectorAll("#mathContinuousTransfer mfrac").length'),1);
  await evaluate('document.getElementById("continuousStateSpace").open=true;document.getElementById("continuousAssumptionsDetails").open=true');
  assert.equal(await evaluate('document.querySelectorAll("#continuousMatrices pre").length'),6);
  assert.ok((await evaluate('document.getElementById("continuousSampling").textContent')).includes('τ = 0 s'));
  await screenshotElement('continuousModelBlock','continuous_offline.png');
  const convertedDownloads=path.join(downloads,'continuous');await mkdir(convertedDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:convertedDownloads},null);
  await click('exportMathModel');await click('exportReport');
  await until(async()=> (await readdir(convertedDownloads)).includes('辨识数学模型.txt')&&(await readdir(convertedDownloads)).includes('辨识报告.md'));
  assert.ok((await readFile(path.join(convertedDownloads,'辨识数学模型.txt'),'utf8')).includes(converted.transfer_function.text));
  assert.ok((await readFile(path.join(convertedDownloads,'辨识报告.md'),'utf8')).includes(converted.transfer_function.text));
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:automaticDownloads},null);
  await evaluate('document.getElementById("mathStateSpace").open=true');
  assert.equal(await evaluate('document.querySelectorAll("#stateMatrices pre").length'),6);
  assert.ok((await evaluate('document.getElementById("stateDefinition").textContent')).includes('y[k-1], u[k-1]'));
  await screenshotElement('mathModelCard','math_arx.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'automatic order panel overflows on mobile');
  await screenshot('auto_mobile.png');
  // A real negative-pole fit must explain the unsupported principal real log.
  await evaluate(`(async()=>{
    window.savedPositiveModel=state.result;
    let y=0,oldU=0,csv='time_s,u,y\\n';
    for(let k=0;k<200;k++){const u=Math.sin(k*.37)+Math.cos(k*.17);y=-.5*y+.3*oldU+.02;csv+=[k*.01,u,y].join(',')+'\\n';oldU=u;}
    const result=await api('/api/offline',{csv,model:{kind:'arx',input:'u',output:'y',time:'time_s',sample_time:.01},order_mode:'fixed'});render(result);
  })()`);
  assert.ok(await evaluate('document.getElementById("continuousBody").hidden'));
  assert.ok((await evaluate('document.getElementById("continuousUnavailable").textContent')).includes('复数'));
  assert.ok(await evaluate('!document.getElementById("mathTransferBlock").hidden'));
  await screenshotElement('continuousModelBlock','continuous_unavailable.png');
  await evaluate('render(window.savedPositiveModel);delete window.savedPositiveModel');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await evaluate('document.getElementById("intercept").checked=false;document.getElementById("intercept").dispatchEvent(new Event("input",{bubbles:true}))');
  assert.ok(await evaluate('document.getElementById("resultContent").hidden'),'configuration must invalidate exports');
  await evaluate('document.getElementById("intercept").checked=true');
  await upload('#dataFile',path.join(root,'examples','arx.csv'));
  await until(()=>evaluate('document.getElementById("filename").textContent==="arx.csv"'));
  await upload('#validationFile',path.join(root,'examples','arx.csv'));
  await until(()=>evaluate('document.getElementById("validationInfo").textContent==="arx.csv"'));
  await click('fitButton');await until(()=>evaluate('!document.getElementById("resultContent").hidden'));
  assert.equal(await evaluate('document.getElementById("metric3Note").textContent'),'独立文件');
  console.log('PASS automatic ARX order/delay, 704-candidate export, separate selection/final validation, candidate table and mobile layout');

  // Frequency selection, physical model, spectra and exports on noisy LTI data.
  assert.equal(await evaluate('document.getElementById("outputLowpass")'),null);
  await upload('#dataFile',path.join(root,'examples','frequency.csv'));
  await until(()=>evaluate('document.getElementById("filename").textContent==="frequency.csv"'));
  await evaluate(`document.getElementById('algorithm').value='frequency';document.getElementById('algorithm').dispatchEvent(new Event('input',{bubbles:true}))`);
  assert.ok(await evaluate('!document.getElementById("frequencySettings").hidden'));
  await click('fitButton');await until(()=>evaluate('!state.busy&&state.result?.algorithm==="frequency"'),30000);
  assert.ok(await evaluate('state.result.output_results[0].metrics.fit_percent>65'));
  assert.ok(await evaluate('!document.getElementById("frequencyCard").hidden'));
  assert.ok(await evaluate('state.result.output_results[0].frequency.spectrum.length>12'));
  assert.equal(await evaluate('state.result.output_filter??null'),null);
  assert.ok((await evaluate('state.result.csv_export')).includes('y_measured'));
  const frequencyDownloads=path.join(downloads,'frequency');await mkdir(frequencyDownloads);
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:frequencyDownloads},null);
  for(const id of ['exportModel','exportCSV','exportMathModel'])await click(id);
  await until(async()=> (await readdir(frequencyDownloads)).length===3);
  assert.equal(JSON.parse(await readFile(path.join(frequencyDownloads,'parameter_model.json'),'utf8')).algorithm,'frequency');
  await screenshot('frequency_offline.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'));
  await screenshot('frequency_mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await command('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:automaticDownloads},null);
  await click('arxDemo');
  await until(()=>evaluate('!state.busy'));
  // The frequency-aware ARX example keeps this choice; explicitly restore LS fixture.
  await evaluate(`document.getElementById('algorithm').value='ls';conditionalFields()`);
  await click('arxDemo');await until(()=>evaluate('!state.busy&&state.rows.length===900'));
  console.log('PASS offline windowed frequency selection, spectrum/coherence, noisy fit, original data exports and mobile layout');

  await switchMode('online');
  await click('replayButton');
  await until(()=>evaluate('document.getElementById("orderTitle").textContent==="正在收集自动定阶数据"'));
  assert.equal(await evaluate('document.querySelectorAll("#parameters tr").length'),0);
  assert.ok(await evaluate('document.getElementById("exportModel").disabled'));
  assert.ok(await evaluate('document.getElementById("mathModelBody").hidden&&document.getElementById("exportMathModel").disabled'));
  await until(()=>evaluate('document.getElementById("selectedNa").textContent==="1"'),15000);
  await until(()=>evaluate('document.getElementById("metric3").textContent==="900"'),20000);
  assert.deepEqual(await evaluate('["selectedNa","selectedNb","selectedNk"].map(id=>document.getElementById(id).textContent)'),['1','1','1']);
  assert.ok((await evaluate('document.getElementById("orderContext").textContent')).includes('400'));
  const retained=await evaluate('state.result.series.length');
  assert.equal(retained,500,'calibration data must not appear as live prediction errors');
  assert.equal(await evaluate('state.result.mathematical_model.transfer_function.denominator[1]===-state.result.parameters[0].value'),true);
  await screenshot('auto_online.png');
  await click('endButton');
  await click('liveButton');
  await until(()=>evaluate('document.getElementById("sessionMode").textContent==="实时 HTTP 数据接入"&&!document.getElementById("endButton").disabled'));
  const arxLiveId=await evaluate('document.getElementById("sessionId").textContent');
  const arxInspection=await (await fetch(base+'/api/inspect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csv:await readFile(path.join(root,'examples','arx.csv'),'utf8')})})).json();
  for(const [start,end] of [[0,200],[200,400],[400,420]]) {
    const response=await fetch(base+'/api/session/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:arxLiveId,sequence:start,samples:arxInspection.rows.slice(start,end)})});
    assert.ok(response.ok,await response.text());
  }
  await until(()=>evaluate('document.getElementById("metric3").textContent==="420"'));
  assert.equal(await evaluate('state.result.series.length'),20);
  assert.equal(await evaluate('document.getElementById("selectedNa").textContent'),'1');
  await click('endButton');
  // Advanced streaming uses the frequency selector and causal window boundaries.
  await evaluate(`document.getElementById('onlineAlgorithm').value='frequency';document.getElementById('frequencySegment').value='64';document.getElementById('frequencySamples').value='512';document.getElementById('frequencyHop').value='128';conditionalFields()`);
  await click('liveButton');await until(()=>evaluate('!!state.session&&!state.busy'));
  const frequencySession=await evaluate('state.session');
  for(let k=0;k<800;k+=128){
    const response=await fetch(base+'/api/session/push',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:frequencySession,sequence:k,samples:arxInspection.rows.slice(k,Math.min(k+128,800))})});
    assert.ok(response.ok,await response.text());
  }
  await until(()=>evaluate('state.result?.algorithm==="frequency"&&state.result.next_sequence===800'));
  assert.ok(await evaluate('state.result.updates>=2'));
  assert.equal(await evaluate('state.result.series.length'),288);
  assert.ok(await evaluate('!document.getElementById("frequencyCard").hidden'));
  await screenshot('frequency_online.png');
  await click('endButton');
  await evaluate(`document.getElementById('onlineAlgorithm').value='rls';conditionalFields()`);
  // Exercise the beginner flow using actual controls and a real background source.
  await click('toggleAdvanced');
  await until(()=>evaluate('!document.getElementById("acquisitionWizard").hidden'));
  await click('demoStart');
  await until(()=>evaluate('!!acquisition.current?.channels&&!state.busy'));
  assert.ok(await evaluate('document.getElementById("legacySettings").hidden'));
  await until(()=>evaluate('acquisition.current?.result?.parameters.length>0&&acquisition.current.fitted>500'),20000);
  assert.ok(await evaluate('document.getElementById("sessionCard").hidden'));
  assert.deepEqual(await evaluate('[state.result.model.na,state.result.model.nb,state.result.model.nk]'),[1,1,1]);
  assert.ok((await evaluate('document.getElementById("sourceDescription").textContent')).includes('仿真'));
  assert.ok((await evaluate('document.getElementById("mathSignals").textContent')).includes('模拟电压'));
  assert.ok((await evaluate('document.getElementById("mathModelCaption").textContent')).includes('当前参数快照'));
  assert.equal(await evaluate('state.result.mathematical_model.continuous_model.status'),'available');
  await screenshotElement('continuousModelBlock','continuous_online.png');
  await screenshotElement('mathModelCard','math_online.png');
  await screenshot('beginner_demo.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'beginner workflow overflows mobile');
  await screenshot('beginner_mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  const demoAcquisition=await evaluate('acquisition.current.id');
  await click('stopCapture');
  await until(()=>evaluate('acquisition.current?.finished&&!!acquisition.current?.download'));
  const savedDemo=await evaluate('acquisition.current');
  assert.equal(savedDemo.accepted,savedDemo.recorded);assert.equal(savedDemo.recorded,savedDemo.fitted);
  await until(async()=> (await readdir(automaticDownloads)).includes(`experiment-${demoAcquisition.slice(0,8)}.zip`));
  assert.ok((await readFile(path.join(automaticDownloads,`experiment-${demoAcquisition.slice(0,8)}.zip`))).length>1000);
  console.log('PASS one-click beginner demo, automatic order selection, live previews, mobile layout and full experiment ZIP download');

  // Decode the exact same wire format as the serial receiver, without hardware.
  await evaluate(`document.getElementById('sourceKind').value='serial';sourceFields();document.getElementById('decoderTest').open=true;document.getElementById('decoderExample').value='double'`);
  await click('loadDecoderExample');await click('testDecoder');
  await until(()=>evaluate('document.getElementById("decoderFeedback").textContent.includes("已解码 1 帧")&&!state.busy'));
  assert.deepEqual(await evaluate('JSON.parse(document.getElementById("decoderPreview").textContent)'),[{input1:1,output1:2}]);
  assert.equal(await evaluate('selectedSource().format'),'binary');
  assert.ok(await evaluate('document.getElementById("textEncodingField").hidden'));
  await evaluate(`document.getElementById('binaryEndian').value='big';document.getElementById('decoderHex').value='3F F0 00 00 00 00 00 00 40 00 00 00 00 00 00 00'`);
  await click('testDecoder');await until(()=>evaluate('!state.busy'));
  assert.deepEqual(await evaluate('JSON.parse(document.getElementById("decoderPreview").textContent)'),[{input1:1,output1:2}]);
  for(const [kind,expected] of [['timed',{time_s:0,input1:1,output1:2}],['mixed',{time_s:.01,input1:1.5,output1:-2}],['bits',{input1:17,output1:42}],['checksum',{input1:1,output1:2}]]){
    await evaluate(`document.getElementById('decoderExample').value=${JSON.stringify(kind)}`);
    await click('loadDecoderExample');await click('testDecoder');
    await until(()=>evaluate('document.getElementById("decoderFeedback").textContent.includes("已解码 1 帧")&&!state.busy'));
    assert.deepEqual(await evaluate('JSON.parse(document.getElementById("decoderPreview").textContent)'),[expected]);
  }
  await evaluate(`window.binaryTemplate={name:'Binary settings',source:selectedSource(),channels:{input:'input1',output:'output1',sample_time:.01}};document.getElementById('serialFormat').value='csv';document.getElementById('decoderCode').value='';applyTemplate(window.binaryTemplate)`);
  assert.equal(await evaluate('document.getElementById("serialFormat").value'),'custom');
  assert.ok((await evaluate('document.getElementById("decoderCode").value')).includes('sum8'));
  assert.equal(await evaluate('document.getElementById("binaryHeader").value'),'AA 55');
  await evaluate(`document.getElementById('decoderHex').value='AA 55 01 02 00 0D 0A'`);
  await click('testDecoder');await until(()=>evaluate('document.getElementById("decoderFeedback").textContent.includes("assert")&&!state.busy'));
  assert.ok(await evaluate('document.getElementById("decoderPreview").hidden'));
  await evaluate(`document.getElementById('decoderCode').value='input1 = open(0)\\noutput1 = 1'`);
  await click('testDecoder');await until(()=>evaluate('document.getElementById("decoderFeedback").textContent.includes("不支持")&&!state.busy'));
  await evaluate(`document.getElementById('decoderExample').value='mixed'`);
  await click('loadDecoderExample');await click('testDecoder');await until(()=>evaluate('!state.busy'));
  assert.ok(await evaluate('!document.getElementById("status").classList.contains("error")'),'successful decode must clear an earlier error');
  await screenshot('binary_decoder.png');
  await screenshotElement('binarySettings','binary_decoder_controls.png');
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'binary decoder overflows mobile');
  await screenshot('binary_decoder_mobile.png');
  await command('Emulation.setDeviceMetricsOverride',{width:1500,height:1080,deviceScaleFactor:1,mobile:false});
  await evaluate(`document.getElementById('serialFormat').value='csv';decoderFields();acquisition.preferred=null;document.getElementById('templateName').value=''`);
  assert.ok(await evaluate('document.getElementById("binarySettings").hidden&&!document.getElementById("textEncodingField").hidden'));
  console.log('PASS binary presets, byte order, mixed types, packed uint6, checksums, expression validation, template restoration, CSV mode and mobile decoder UI');

  const captureFile=path.join(downloads,'live_input.csv');
  const fileRows=arxInspection.rows;
  const asLine=row=>`${row.time_s},${row.u},${row.y}\n`;
  await writeFile(captureFile,'time_s,u,y\n'+fileRows.slice(0,3).map(asLine).join(''));
  await evaluate(`document.getElementById('sourceKind').value='file';document.getElementById('sourceKind').dispatchEvent(new Event('change'));document.getElementById('capturePath').value=${JSON.stringify(captureFile)}`);
  await click('connectSource');
  await until(()=>evaluate('acquisition.current?.source.kind==="file"&&acquisition.current.received===3&&!state.busy'));
  assert.ok(await evaluate('!document.getElementById("mappingConfirmed").checked'));
  assert.ok(await evaluate('document.getElementById("startCapture").disabled'));
  await evaluate(`document.getElementById('captureAlgorithm').value='frequency';document.getElementById('captureSegment').value='64';document.getElementById('captureWindowSamples').value='512';document.getElementById('captureUpdateEvery').value='128';document.getElementById('captureAlgorithm').dispatchEvent(new Event('input',{bubbles:true}))`);
  await evaluate(`document.getElementById('templateName').value='UI test file';document.getElementById('templateName').dispatchEvent(new Event('input'))`);
  await click('saveDeviceTemplate');
  await until(()=>evaluate('document.getElementById("templateFeedback").textContent.includes("已保存")'));
  await click('mappingConfirmed');await click('startCapture');
  await until(()=>evaluate('!!acquisition.current?.channels&&!state.busy'));
  const body=fileRows.slice(3,603).map(asLine).join('');
  await appendFile(captureFile,body.slice(0,-5));
  await until(()=>evaluate('acquisition.current?.recorded===599'),20000);
  await appendFile(captureFile,body.slice(-5));
  await until(()=>evaluate('acquisition.current?.recorded===600&&acquisition.current.fitted===600'),20000);
  assert.equal(await evaluate('acquisition.current.received'),603);
  assert.equal(await evaluate('acquisition.current.result.algorithm'),'frequency');
  assert.ok(await evaluate('acquisition.current.result.parameters.length>0'));
  assert.ok(await evaluate('document.getElementById("captureAlgorithm").disabled||document.getElementById("channelSettings").disabled'));
  await screenshot('frequency_file_capture.png');
  await screenshot('beginner_file.png');
  await click('stopCapture');await until(()=>evaluate('acquisition.current?.finished'));
  const savedFile=await evaluate('acquisition.current');
  assert.equal(savedFile.recorded,600);assert.equal(savedFile.fitted,600);assert.equal(savedFile.error,'');
  await evaluate(`document.getElementById('deviceTemplate').value='UI test file';document.getElementById('deviceTemplate').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate('document.getElementById("capturePath").value'),captureFile);
  assert.equal(await evaluate('acquisition.preferred.input'),'u');
  assert.equal(await evaluate('document.getElementById("captureAlgorithm").value'),'frequency');
  assert.equal(await evaluate('Number(document.getElementById("captureWindowSamples").value)'),512);
  // Switching mode must stop the active source and preserve its accepted tail.
  await click('demoStart');await until(()=>evaluate('!!acquisition.current?.channels&&!state.busy'));
  await switchMode('offline');
  const endedBySwitch=await (await fetch(base+'/api/acquisition')).json();
  assert.ok(endedBySwitch.finished);assert.equal(endedBySwitch.accepted,endedBySwitch.recorded);
  assert.ok(endedBySwitch.download);
  console.log('PASS CSV file follow with partial lines, mapping confirmation, persisted device template, new-sample-only modeling, and save-on-mode-switch');
  console.log('PASS mathematical model: regression/difference equations, discrete transfer function, affine state matrices, pending state, live coefficients, signal units, TXT/JSON/report exports');
  console.log('PASS ZOH continuous models, state matrices, explicit assumptions, correct zero dead time, live updates, unsupported-model explanation, and TXT/JSON/report exports');
  assert.equal(jsErrors.length,0,JSON.stringify(jsErrors));
  console.log('PASS causal online automatic selection in replay and external HTTP streaming; no retrospective forecast errors, no JavaScript errors');
  await writeFile(path.join(output,'summary.json'),JSON.stringify({passed:true,version:'0.7.0',windowedFrequency:true,outputLowpassRemoved:true,binaryDecoder:true,headerlessOriginalSignals:true,multichannel:{inputs:multiModel.model.inputs,outputs:multiModel.model.outputs,metrics:multiModel.output_results.map(r=>r.metrics)},offline,automaticOrder:automaticModel.order_selection.selected,beginnerDemo:{recorded:savedDemo.recorded,fitted:savedDemo.fitted},fileFollow:{recorded:savedFile.recorded,fitted:savedFile.fitted},jsErrors,downloads},null,2));
}catch(error){
  if(session&&ws?.readyState===WebSocket.OPEN){
    try{await screenshot('failure.png');console.error(await evaluate('({status:document.getElementById("status").textContent,busy:state.busy,mode:state.mode,advanced:acquisition.advanced,capture:acquisition.current?{phase:acquisition.current.phase,error:acquisition.current.error,received:acquisition.current.received,recorded:acquisition.current.recorded}:null})'));}catch{}
  }
  throw error;
}finally{
  if(ws&&ws.readyState===WebSocket.OPEN){try{await command('Browser.close',{},null);}catch{}ws.close();}
  if(service)service.kill();if(browser&&browser.exitCode===null)browser.kill();
}
