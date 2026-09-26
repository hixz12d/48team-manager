const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const root = path.resolve(__dirname, '../extensions/chatgpt-signup');
const shared = import(pathToFileURL(path.join(root, 'shared.mjs')).href);
const mail = import(pathToFileURL(path.join(root, 'cloudflare.mjs')).href);
const MAILBOX = {baseUrl:'https://apimail.xiaozhudf2026.foo',address:'inbox@example.com',adminPassword:'fixture-secret'};

async function worker({incognito = true, session = {}, local = {}, failMail = false, debuggerAPI = false, commandHook, team48, fetch, poll} = {}) {
  const handlers = {}, apiCalls = [], createdTabs = [], commands = [], navigation = {}, updatedTabs = [];
  const area = store => ({
    setAccessLevel: async () => {}, get: async key => ({[key]: structuredClone(store[key])}),
    set: async values => Object.assign(store, structuredClone(values)), remove: async key => { delete store[key]; },
  });
  const chrome = {
    extension: {inIncognitoContext: incognito}, storage: {local: area(local), session: area(session)},
    runtime: {id: 'test-extension', getManifest: () => ({version:'0.3.7'}), getURL: file => `chrome-extension://test-extension/${file}`, onMessage: {addListener: cb => { handlers.message = cb; }}},
    alarms: {create: async () => {}, clear: async () => {}, onAlarm: {addListener: cb => { handlers.alarm = cb; }}},
    windows: {getCurrent: async () => ({id: 1, incognito}), getLastFocused: async () => ({id: 1, incognito})},
    tabs: {create: async tab => { createdTabs.push(tab); return {id: 7}; }, get: async id => ({id}),
      update: async (id, change) => { updatedTabs.push({id, ...change}); return {id, windowId: 1}; },
      onRemoved: {addListener: cb => { handlers.removed = cb; }}, onUpdated: {addListener: cb => { handlers.updated = cb; }}},
    webNavigation: Object.fromEntries(['onBeforeNavigate', 'onCommitted', 'onErrorOccurred'].map(name =>
      [name, {addListener: (cb, filter) => { navigation[name] = {cb, filter}; }}])),
  };
  if (debuggerAPI) chrome.debugger = {
    attach: async () => {}, detach: async () => {}, onDetach: {addListener: cb => {handlers.detach = cb;}},
    sendCommand: async (target, method, params) => { commands.push({target,method,params}); return commandHook?.(method,params,session) || {}; },
  };
  const context = vm.createContext({
    setTimeout: callback => setTimeout(callback, 0),
    ...(await shared), chrome, crypto: globalThis.crypto, URL, console, Date, MAILBOX, RUN_TTL: 600000,
    PRIVATE_CONFIG: team48 ? {MAILBOX, TEAM48: team48} : {MAILBOX}, AbortSignal,
    fetch: fetch || (async () => { throw new Error('unexpected network request'); }),
    startMailbox: async (config,email) => {
      apiCalls.push({config,email,stage:'baseline',tabs:createdTabs.length});
      if(failMail) throw new Error('Cloudflare 邮箱返回 HTTP 401。');
      return {started:Date.now(),seen:['old-mail'],codes:['123456']};
    },
    pollMailbox: async (config,email,baseline,ignored) => {apiCalls.push({config,email,baseline,ignored});return poll ? poll() : '654321';},
  });
  const source = fs.readFileSync(path.join(root, 'background.js'), 'utf8').replace(/^import .*;\r?\n/gm, '');
  vm.runInContext(source, context);
  const popup = {id: 'test-extension', url: chrome.runtime.getURL('popup.html')};
  const content = {id:'test-extension',url:'https://auth.openai.com/email-verification',frameId:0,tab:{id:7,incognito:true}};
  const send = (message,sender=popup) => new Promise(resolve => handlers.message({version:'0.3.7',...message},sender,resolve));
  return {send,content,session,local,apiCalls,createdTabs,commands,navigation,updatedTabs,handlers};
}

test('only email is needed; snapshot precedes opening tab; no configuration or login', async () => {
  const w = await worker({local:{config:{token:'obsolete',profile:{name:'old'}}}});
  const view = await w.send({type:'view'});
  assert.equal(w.local.config,undefined);
  assert.equal(view.config,undefined);
  assert.equal(w.apiCalls.length,0);
  const started = await w.send({type:'start',email:'TEST@icloud.com'});
  assert.equal(started.ok,true);
  assert.equal(w.session.job.email,'test@icloud.com');
  assert.match(w.session.job.profile.name,/^[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+){1,2}$/);
  assert.equal(w.apiCalls[0].tabs,0);
  assert.equal(w.apiCalls[0].config,MAILBOX);
  assert.ok(w.session.job.password.length>20);
  assert.ok(!JSON.stringify(w.session).includes('fixture-secret'));
  assert.equal((await w.send({type:'start',email:'other@icloud.com'})).ok,false);
});

test('CDP focus is configured per page, and pause/stop/expiry reject all new input', async () => {
  const w = await worker({debuggerAPI:true});
  await w.send({type:'start',email:'test@icloud.com',hidePanel:true});
  const jobId = w.session.job.id;
  const input = (type,extra={}) => w.send({type,jobId,...extra},w.content);
  assert.equal((await input('input-ready')).trusted,true);
  await input('input-key',{key:'Tab'});
  await input('input-text',{text:'fixture-password'});
  await input('input-wheel',{x:30,y:40,deltaY:100,deltaX:0});
  assert.equal(w.commands.filter(c=>c.method==='Emulation.setFocusEmulationEnabled').length,1);
  await input('input-ready');
  assert.equal(w.commands.filter(c=>c.method==='Emulation.setFocusEmulationEnabled').length,2);
  assert.equal((await input('state')).hidePanel,true);
  await w.send({type:'pause'});
  const count=w.commands.length;
  for(const type of ['input-ready','input-key','input-text','input-wheel','input-drift']) {
    assert.equal((await input(type,{key:'Enter',text:'blocked',x:30,y:40,deltaY:10,width:800,height:600})).active,false);
  }
  assert.equal(w.commands.length,count);
  await w.send({type:'resume'});
  w.session.job.expiresAt=Date.now()-1;
  assert.equal((await input('input-text',{text:'expired'})).active,false);
  assert.equal(w.commands.length,count);
});

test('pause interrupts a mouse path before any press and manual mode cannot acquire CDP', async () => {
  const w=await worker({debuggerAPI:true,commandHook:(method,params,session)=>{
    if(params.type==='mouseMoved')session.job.status='paused';
  }});
  await w.send({type:'start',email:'test@icloud.com'});
  const result=await w.send({type:'input-click',jobId:w.session.job.id,x:200,y:200},w.content);
  assert.equal(result.ok,false);
  assert.ok(!w.commands.some(c=>c.params.type==='mousePressed'));
  const manual=await worker({debuggerAPI:true});
  await manual.send({type:'start',email:'test@icloud.com',mode:'manual',hidePanel:true});
  assert.equal(manual.session.job.hidePanel,false);
  assert.equal((await manual.send({type:'input-ready',jobId:manual.session.job.id},manual.content)).active,false);
  assert.equal(manual.commands.length,0);
});

test('pausing after Shift down releases Shift without typing the character', async () => {
  const w=await worker({debuggerAPI:true,commandHook:(method,params,session)=>{
    if(params.type==='rawKeyDown' && params.key==='Shift')session.job.status='paused';
  }});
  await w.send({type:'start',email:'test@icloud.com'});
  assert.equal((await w.send({type:'input-key',key:'A',jobId:w.session.job.id},w.content)).ok,false);
  const keys=w.commands.filter(c=>c.method==='Input.dispatchKeyEvent').map(c=>[c.params.type,c.params.key]);
  assert.deepEqual(keys,[['rawKeyDown','Shift'],['keyUp','Shift']]);
});

test('generated profiles vary while birthdays and ages remain valid across calendar boundaries', async () => {
  const {defaultProfile}=await shared;
  const names=new Set(), birthdays=new Set();
  for(const now of [new Date(2026,0,1),new Date(2026,1,28),new Date(2024,1,29),new Date(2026,11,31)]) {
    for(let index=0;index<150;index++) {
      const profile=defaultProfile(now);
      assert.match(profile.name,/^[A-Z][A-Za-z.]+(?: [A-Z][A-Za-z.]+){1,2}$/);
      assert.match(profile.birthday,/^\d{4}-\d{2}-\d{2}$/);
      const [year,month,day]=profile.birthday.split('-').map(Number);
      const birthday=new Date(year,month-1,day);
      assert.equal(birthday.getMonth()+1,month);
      assert.equal(birthday.getDate(),day);
      const age=now.getFullYear()-year-(now.getMonth()+1<month || (now.getMonth()+1===month && now.getDate()<day) ? 1:0);
      assert.ok(age>=22 && age<=45);
      names.add(profile.name);birthdays.add(profile.birthday);
    }
  }
  assert.ok(names.size>1);
  assert.ok(birthdays.size>1);
});

test('regular windows still cannot start; failed mailbox read opens no tab', async () => {
  const w = await worker({incognito:false});
  assert.equal((await w.send({type:'start',email:'test@icloud.com'})).ok,false);
  assert.equal(w.apiCalls.length,0);
  const denied = await worker({failMail:true});
  assert.equal((await denied.send({type:'start',email:'test@icloud.com'})).ok,false);
  assert.equal(denied.createdTabs.length,0);
});

test('only bound top-level incognito tab receives state; mailbox credentials stay in worker', async () => {
  const w = await worker(); await w.send({type:'start',email:'test@icloud.com'});
  for(const sender of [{...w.content,tab:{id:8,incognito:true}},{...w.content,frameId:1},
    {...w.content,url:'https://evil.example'},{...w.content,url:'http://chatgpt.com'}, {...w.content,tab:{id:7,incognito:false}}]) {
    assert.equal((await w.send({type:'state'},sender)).active,false);
  }
  const state = await w.send({type:'state'},w.content);
  assert.equal(state.email,'test@icloud.com');
  assert.ok(!JSON.stringify(state).includes('fixture-secret'));
  assert.equal((await w.send({type:'view'},w.content)).active,false);
});

test('worker restart preserves snapshot and claims; stop and expiry prevent polling', async () => {
  let w = await worker(); await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  const originalProfile=structuredClone(w.session.job.profile);
  assert.equal((await w.send({type:'claim',stage:'email',jobId},w.content)).granted,true);
  w=await worker({session:w.session});
  const restoredState=await w.send({type:'state'},w.content);
  assert.deepEqual(restoredState.profile,originalProfile);
  assert.equal((await w.send({type:'claim',stage:'email',jobId},w.content)).granted,false);
  assert.equal((await w.send({type:'code',jobId},w.content)).code,'654321');
  assert.equal(w.apiCalls[0].baseline.seen[0],'old-mail');
  assert.equal((await w.send({type:'code',jobId},w.content)).code,'654321');
  assert.equal(w.session.job.ignoredCodes.length,0);
  assert.equal((await w.send({type:'claim',stage:'otp',prepare:true,jobId},w.content)).granted,true);
  assert.equal(w.session.job.claims['otp:/email-verification'],undefined);
  assert.equal((await w.send({type:'claim',stage:'otp',jobId},w.content)).granted,true);
  assert.ok(w.session.job.ignoredCodes.includes('654321'));
  assert.equal((await w.send({type:'code',jobId},w.content)).code,null);
  await w.send({type:'complete',jobId,email:'wrong@icloud.com'},{...w.content,url:'https://chatgpt.com/'});
  assert.equal(w.session.job.status,'paused');
  w.session.job.expiresAt=Date.now()-1;
  await w.send({type:'resume'});
  assert.equal(w.session.job.status,'stopped');
  assert.equal(w.session.job.baseline,undefined);
  assert.equal((await w.send({type:'code',jobId},w.content)).active,false);
});

test('obsolete content cannot type, consume OTPs or overwrite the current task', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const before=structuredClone(w.session.job);
  for(const version of [undefined,'0.2.3','0.2.4','0.3.0','0.3.1','0.3.2','0.3.3','0.3.4','0.3.5']) {
    for(const type of ['state','code','pause','claim']) {
      const result=await w.send({type,version,jobId:before.id,stage:'otp'},w.content);
      assert.equal(result.active,false);
      assert.match(result.message,/旧版/);
      assert.equal(result.password,undefined);
    }
  }
  assert.deepEqual(w.session.job,before);
  assert.equal((await w.send({type:'view'})).version,'0.3.7');
});

test('resume retries only the timed-out stage and keeps an overall submission limit', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  for(const stage of ['email','profile']) {
    const sender={...w.content,url:'https://auth.openai.com/'+stage};
    const message={type:'claim',stage,jobId};
    const key=stage+':/'+stage;
    assert.equal((await w.send(message,sender)).granted,true);
    await w.send({type:'pause',jobId,reason:'manual'},sender);
    await w.send({type:'resume'});
    assert.equal((await w.send({...message,prepare:true},sender)).granted,false);
    w.session.job.claims[key]=Date.now()-46000;
    await w.send({...message,prepare:true},sender);
    assert.equal(w.session.job.status,'paused');
    await w.send({type:'resume'});
    assert.equal((await w.send(message,sender)).granted,true);
    w.session.job.claims[key]=Date.now()-46000;
    await w.send({...message,prepare:true},sender);
    await w.send({type:'resume'});
    assert.equal((await w.send(message,sender)).granted,false);
    assert.equal(w.session.job.attempts[stage],2);
    await w.send({type:'resume'});
  }
});

test('slow mailbox read no longer blocks page state or pause; a stale result is dropped', async () => {
  let release;
  const gate=new Promise(resolve=>{release=resolve;});
  const w=await worker({poll:()=>gate});
  await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  const reading=w.send({type:'code',jobId},w.content);
  await new Promise(resolve=>setTimeout(resolve,20));
  // Before 0.5.2 these waited for Cloudflare (up to 25 s) behind the mailbox request.
  assert.equal((await w.send({type:'state'},w.content)).active,true);
  assert.equal((await w.send({type:'code',jobId},w.content)).code,null);
  await w.send({type:'pause',jobId,reason:'manual'},w.content);
  release('654321');
  assert.equal((await reading).code,null);
  assert.equal(w.session.job.pendingCode,undefined);
  await w.send({type:'resume'});
  w.session.job.lastPoll=0;
  assert.equal((await w.send({type:'code',jobId},w.content)).code,'654321');
  assert.equal(w.session.job.pendingCode,'654321');
});

test('waiting too long for a new OTP pauses with a resend hint and resume restarts the wait', async () => {
  const w=await worker({poll:async()=>null});
  await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  assert.equal((await w.send({type:'code',jobId},w.content)).code,null);
  assert.equal(w.session.job.status,'running');
  w.session.job.codeWaitSince=Date.now()-121000; w.session.job.lastPoll=0;
  assert.equal((await w.send({type:'code',jobId},w.content)).code,null);
  assert.equal(w.session.job.status,'paused');
  assert.match(w.session.job.message,/重新发送/);
  await w.send({type:'resume'});
  assert.equal(w.session.job.codeWaitSince,undefined);
  w.session.job.lastPoll=0;
  await w.send({type:'code',jobId},w.content);
  assert.equal(w.session.job.status,'running');
});

test('paused OTP stays available until an actual submit, and stop blocks all later edits', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  assert.equal((await w.send({type:'code',jobId},w.content)).code,'654321');
  await w.send({type:'pause',jobId,reason:'check form'},w.content);
  assert.equal((await w.send({type:'code',jobId},w.content)).active,false);
  await w.send({type:'resume',jobId},w.content);
  assert.equal((await w.send({type:'code',jobId},w.content)).code,'654321');
  await w.send({type:'stop'});
  assert.equal(w.session.job.pendingCode,undefined);
  assert.equal((await w.send({type:'claim',stage:'otp',jobId},w.content)).active,false);
});

test('manual modes never access mail, review cannot submit fields, and mode/profile validation precedes side effects', async () => {
  for (const mode of ['submit','manual']) {
    const w=await worker({failMail:true});
    assert.equal((await w.send({type:'start',email:'test@icloud.com',mode})).ok,true);
    const jobId=w.session.job.id;
    assert.equal(w.apiCalls.length,0);
    assert.equal((await w.send({type:'code',jobId},w.content)).code,null);
    assert.equal((await w.send({type:'state'},w.content)).mode,mode);
    assert.ok(w.session.job.expiresAt-w.session.job.startedAt>=1799000);
    if(mode==='manual') assert.equal((await w.send({type:'claim',stage:'email',jobId},w.content)).granted,false);
  }
  const w=await worker();
  for (const payload of [{mode:'invalid'},{profile:{name:'Test'}},{profile:{name:'Test',birthday:'2025-02-30'}}]) {
    assert.equal((await w.send({type:'start',email:'test@icloud.com',...payload})).ok,false);
  }
  assert.equal(w.apiCalls.length,0);assert.equal(w.createdTabs.length,0);
  await w.send({type:'start',email:'test@icloud.com',mode:'review',profile:{name:'Custom Name',birthday:'1980-02-29'}});
  assert.equal(w.session.job.profile.name,'Custom Name');
  const jobId=w.session.job.id;
  assert.equal((await w.send({type:'claim',stage:'email',prepare:true,jobId},w.content)).granted,true);
  assert.equal((await w.send({type:'claim',stage:'email',jobId},w.content)).granted,false);
  await w.send({type:'review-ready',stage:'email',jobId},w.content);
  const restored=await worker({session:w.session});
  assert.equal((await restored.send({type:'state'},restored.content)).reviewSteps['email:/email-verification'],true);
});

test('diagnostics whitelist fields, bound history, and record user-reported Codex outcome separately', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  const sender={...w.content,url:'https://auth.openai.com/email-verification?token=secret-token#secret-fragment'};
  await w.send({type:'event',event:'page',stage:'otp',jobId,email:'secret-email',password:'secret-password'},sender);
  for(let index=0;index<205;index++) await w.send({type:'event',event:'filled',stage:'otp',jobId,code:'654321'},sender);
  await w.send({type:'event',event:'secret-event',stage:'secret-stage',jobId},sender);
  await w.send({type:'complete',email:'test@icloud.com',jobId},sender);
  assert.equal(w.session.job.status,'running');
  const home={...w.content,url:'https://chatgpt.com/'};
  await w.send({type:'complete',email:'test@icloud.com',verified:false,jobId},home);
  assert.equal(w.session.job.status,'paused');
  await w.send({type:'resume'});
  await w.send({type:'complete',email:'test@icloud.com',verified:true,jobId},home);
  assert.equal(w.session.job.status,'done');
  await w.send({type:'set-result',result:'phone_required'});
  const {report}=await w.send({type:'diagnostics'});
  assert.equal(report.userReportedCodexResult,'phone_required');
  assert.equal(report.summary.otpPageSeen,true);
  assert.equal(report.summary.emailVerified,true);
  assert.equal(report.truncated,true);assert.equal(report.events.length,200);
  const serialized=JSON.stringify(report);
  for(const secret of ['test@icloud.com','654321',w.session.job.password,'secret-','fixture-secret','https://']) assert.ok(!serialized.includes(secret),secret);
  assert.equal((await w.send({type:'diagnostics',jobId},w.content)).report,undefined);
});

test('entry fallback is delayed, bound to the homepage, and persists its single-use guard', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com',mode:'review'});
  const jobId=w.session.job.id, home={...w.content,url:'https://chatgpt.com/'};
  const request={type:'entry-fallback',jobId};
  assert.equal((await w.send(request,home)).granted,false);
  w.session.job.startedAt-=16000;
  for(const sender of [w.content,{...home,url:'https://chatgpt.com/auth/login'},{...home,frameId:1},{...home,tab:{id:8,incognito:true}}]) {
    assert.notEqual((await w.send(request,sender)).granted,true);
  }
  const result=await w.send(request,home);
  assert.equal(result.granted,true);assert.equal(result.url,'https://chatgpt.com/auth/login');
  assert.equal((await w.send(request,home)).granted,false);
  const restored=await worker({session:w.session});
  assert.equal((await restored.send(request,home)).granted,false);
  assert.equal((await restored.send({type:'state'},home)).entryFallbackUsed,true);
  const report=(await restored.send({type:'diagnostics'})).report;
  assert.equal(report.events.filter(event=>event.event==='entry_fallback').length,1);
  assert.ok(!JSON.stringify(report).includes('https://'));
});

test('entry fallback never overrides a form already seen, manual modes, pause or stop', async () => {
  for(const scenario of ['manual','submit','email','password','otp','profile','paused','stopped','claimed']) {
    const w=await worker();await w.send({type:'start',email:'test@icloud.com',mode:['manual','submit'].includes(scenario)?scenario:'auto'});
    const jobId=w.session.job.id, home={...w.content,url:'https://chatgpt.com/'};
    w.session.job.startedAt-=16000;
    if(['email','password','otp','profile'].includes(scenario)) {
      await w.send({type:'event',event:'page',stage:scenario,jobId},home);
      assert.equal((await w.send({type:'state'},home)).entryFormSeen,true);
    }
    if(scenario==='paused') await w.send({type:'pause',stage:'email',jobId},home);
    if(scenario==='stopped') await w.send({type:'stop'});
    if(scenario==='claimed') await w.send({type:'claim',stage:'email',jobId},home);
    assert.notEqual((await w.send({type:'entry-fallback',jobId},home)).granted,true,scenario);
    if(scenario==='paused') assert.equal(w.session.job.events.at(-1).stage,'email');
    assert.ok(!w.session.job.entryFallbackUsed);
  }
});

test('unsent click releases its exact reservation without spending an attempt or OTP', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id, stage='otp';
  const code=(await w.send({type:'code',jobId},w.content)).code;
  const reservation=await w.send({type:'claim',stage,reserve:true,jobId},w.content);
  assert.equal(reservation.granted,true);assert.ok(reservation.token);
  assert.equal(w.session.job.pendingCode,code);
  assert.equal((await w.send({type:'claim',stage,reserve:true,jobId},w.content)).granted,false);
  const finish={type:'finish-click',stage,jobId,token:reservation.token,sent:false};
  for(const [message,sender] of [[{...finish,token:'wrong'},w.content],[{...finish,sent:'false'},w.content],
      [finish,{...w.content,url:'https://chatgpt.com/email-verification'}],[finish,{...w.content,tab:{id:99,incognito:true}}]]) {
    assert.notEqual((await w.send(message,sender)).accepted,true);
  }
  const restarted=await worker({session:w.session});
  assert.equal((await restarted.send(finish,w.content)).accepted,true);
  assert.equal(w.session.job.attempts.otp,0);
  assert.equal(Object.keys(w.session.job.claims).length,0);
  assert.equal(w.session.job.pendingCode,code);
  assert.ok(!w.session.job.ignoredCodes.includes(code));
  assert.equal(w.session.job.events.filter(e=>e.event==='submit_attempt').length,0);
  const next=await restarted.send({type:'claim',stage,reserve:true,jobId},w.content);
  assert.notEqual(next.token,reservation.token);
  assert.equal((await restarted.send(finish,w.content)).accepted,false);
  assert.equal((await restarted.send({...finish,token:next.token,sent:true},w.content)).accepted,true);
  assert.equal((await restarted.send({...finish,token:next.token,sent:true},w.content)).accepted,false);
  assert.equal(w.session.job.attempts.otp,1);
  assert.equal(w.session.job.pendingCode,undefined);
  assert.ok(w.session.job.ignoredCodes.includes(code));
  assert.equal(w.session.job.events.filter(e=>e.event==='submit_attempt').length,1);
});

test('cancel after pause or stop cannot resume a task; lost click acknowledgements remain guarded', async () => {
  for(const status of ['pause','stop']) {
    const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
    const jobId=w.session.job.id;
    const reserved=await w.send({type:'claim',stage:'email',reserve:true,jobId},w.content);
    await w.send({type:status,jobId},status==='pause'?w.content:undefined);
    assert.equal((await w.send({type:'finish-click',stage:'email',token:reserved.token,sent:false,jobId},w.content)).accepted,true);
    assert.equal(w.session.job.status,status==='pause'?'paused':'stopped');
    assert.equal((await w.send({type:'claim',stage:'email',reserve:true,jobId},w.content)).active,false);
  }
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  const code=(await w.send({type:'code',jobId},w.content)).code;
  await w.send({type:'claim',stage:'otp',reserve:true,jobId},w.content);
  w.session.job.claims['otp:/email-verification']-=46000;
  const restarted=await worker({session:w.session});
  assert.equal((await restarted.send({type:'claim',stage:'otp',prepare:true,jobId},w.content)).granted,false);
  await restarted.send({type:'resume'});
  assert.equal(w.session.job.attempts.otp,1);
  assert.equal(w.session.job.pendingCode,undefined);
  assert.ok(w.session.job.ignoredCodes.includes(code));
  assert.equal(Object.keys(w.session.job.clickReservations).length,0);
});

test('click response diagnostics export only allowed outcomes, never DOM messages', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id;
  for(const outcome of ['submitted','loading','advanced','validation','timeout','private error text']) {
    await w.send({type:'event',event:'click_response',stage:'email',jobId,outcome,text:'secret',url:'https://private/'},w.content);
  }
  const report=(await w.send({type:'diagnostics'})).report;
  const events=report.events.filter(e=>e.event==='click_response');
  assert.deepEqual(events.map(e=>e.outcome),['submitted','loading','advanced','validation','timeout',undefined]);
  assert.ok(!JSON.stringify(report).includes('private'));
  assert.ok(!JSON.stringify(report).includes('secret'));
});
test('Continue retries preserve fields/OTP lifecycle and have a persistent two-retry cap', async () => {
  for(const mode of ['auto','review','submit']) {
    const w=await worker();await w.send({type:'start',email:'test@icloud.com',mode});
    const jobId=w.session.job.id, stage='otp', key='otp:/email-verification';
    let token;
    if(mode==='review') {
      assert.equal((await w.send({type:'retry-continue',jobId,stage,token:'not-authorized'},w.content)).granted,false);
      token=(await w.send({type:'continue-observed',jobId,stage},w.content)).token;
    }else{
      if(mode==='auto')await w.send({type:'code',jobId},w.content);
      token=(await w.send({type:'claim',reserve:true,jobId,stage},w.content)).token;
      await w.send({type:'finish-click',sent:true,jobId,stage,token},w.content);
    }
    let request={type:'retry-continue',jobId,stage,token};
    assert.equal((await w.send(request,w.content)).granted,false);
    const polled=w.apiCalls.length;
    w.session.job.continueClicks[key].at=Date.now()-1000;
    const tooSoon=await w.send(request,w.content);
    assert.equal(tooSoon.granted,false);assert.equal(tooSoon.wait,true);
    assert.equal(w.session.job.continueRetries?.otp,undefined);
    for(let index=0;index<2;index++) {
      w.session.job.continueClicks[key].at-=2500;
      const reservation=await w.send(request,w.content);
      assert.equal(reservation.granted,true,mode);
      assert.equal((await w.send(request,w.content)).granted,false);
      assert.equal((await w.send({type:'finish-click',sent:true,jobId,stage,token:reservation.token},w.content)).accepted,true);
      request={...request,token:reservation.token};
    }
    w.session.job.continueClicks[key].at-=2500;
    const restored=await worker({session:w.session});
    assert.equal((await restored.send(request,w.content)).granted,false);
    assert.equal(w.session.job.continueRetries.otp,2);
    assert.equal(w.apiCalls.length,polled);
    assert.equal(w.session.job.events.filter(e=>e.event==='continue_retry').length,2);
    const observed=await restored.send({type:'continue-observed',jobId,stage},w.content);
    w.session.job.continueClicks[key].at-=2500;
    assert.equal((await restored.send({...request,token:observed.token},w.content)).granted,false);
  }
});

test('Continue retry is bound to the receipt and cancelled retries restore the prior claim', async () => {
  const w=await worker();await w.send({type:'start',email:'test@icloud.com'});
  const jobId=w.session.job.id, stage='profile', key='profile:/email-verification';
  const initial=await w.send({type:'claim',stage,jobId,reserve:true},w.content);
  await w.send({type:'finish-click',stage,jobId,token:initial.token,sent:true},w.content);
  const originalClaim=w.session.job.claims[key];
  w.session.job.continueClicks[key].at-=2500;
  const request={type:'retry-continue',stage,jobId,token:initial.token};
  for(const [message,sender] of [[{...request,stage:'email'},w.content],[{...request,token:'wrong'},w.content],
      [request,{...w.content,url:'https://chatgpt.com/email-verification'}],[request,{...w.content,tab:{id:8,incognito:true}}]]) {
    assert.notEqual((await w.send(message,sender)).granted,true);
  }
  const reserved=await w.send(request,w.content);
  assert.equal(reserved.granted,true);
  await w.send({type:'pause',jobId},w.content);
  await w.send({type:'finish-click',stage,jobId,token:reserved.token,sent:false},w.content);
  assert.equal(w.session.job.claims[key],originalClaim);
  assert.equal(w.session.job.attempts.profile,1);
  assert.equal(w.session.job.continueRetries.profile,0);
  assert.notEqual((await w.send(request,w.content)).granted,true);
});

test('loading, errors, page changes and manual-only mode prevent Continue retries', async () => {
  for(const outcome of ['submitted','loading','validation','advanced','page','manual']) {
    const w=await worker();await w.send({type:'start',email:'test@icloud.com',mode:outcome==='manual'?'manual':'review'});
    const jobId=w.session.job.id, stage='otp';
    const observed=await w.send({type:'continue-observed',jobId,stage},w.content);
    if(outcome==='manual') {assert.equal(observed.granted,false);continue;}
    w.session.job.continueClicks['otp:/email-verification'].at-=2500;
    await w.send({type:'event',jobId,event:outcome==='page'?'page':'click_response',stage:outcome==='page'?'profile':stage,outcome},w.content);
    assert.equal((await w.send({type:'retry-continue',jobId,stage,token:observed.token},w.content)).granted,false);
  }
});

test('MIME, HME, quoted-printable and base64 use the existing mailbox conventions', async () => {
  const {parseMessage,registrationMail,extractCode,unwrapItems}=await mail;
  const raw=['From: OpenAI <noreply@tm.openai.com>','To: inbox@example.com',
    'X-ICLOUD-HME: v=1; p=child@icloud.com', 'Subject: =?UTF-8?B?VmVyaWZpY2F0aW9uIGNvZGU=?=',
    'Content-Type: multipart/alternative; boundary=parts','', '--parts',
    'Content-Type: text/plain; charset=utf-8','Content-Transfer-Encoding: quoted-printable','',
    'Verification code: =36=35=34=33=32=31', '--parts','Content-Type: text/html; charset=utf-8',
    'Content-Transfer-Encoding: base64','',Buffer.from('<p>654321</p>').toString('base64'),'--parts--'].join('\r\n');
  const parsed=await parseMessage({raw});
  assert.equal(parsed.to,'child@icloud.com');
  assert.equal(registrationMail(parsed,'child@icloud.com'),true);
  assert.equal(extractCode(parsed.subject+'\n'+parsed.body),'654321');
  assert.equal(registrationMail(parsed,'prefixchild@icloud.com'),false);
  assert.equal(registrationMail({...parsed,from:'noreply@openai.com.evil.example'},'child@icloud.com'),false);
  assert.equal(unwrapItems({data:{results:[{raw},null,9]}}).length,1);
});

test('iCloud HME rewritten sender uses original sender and preserves leading-zero code', async () => {
  const {parseMessage,registrationMail,extractCode}=await mail;
  const raw=['From: ChatGPT <noreply_at_tm_openai_com_random@icloud.com>','To: inbox@example.com',
    'X-ICLOUD-HME: p=child@icloud.com; d=; f=inbox@example.com; r=to; s=noreply@tm.openai.com',
    'Subject: Your temporary ChatGPT verification code','Content-Type: text/html; charset=utf-8','',
    '<p>Enter this temporary verification code to continue:</p><div>005239</div>'].join('\r\n');
  const parsed=await parseMessage({raw});
  assert.equal(parsed.originalSender,'noreply@tm.openai.com');
  assert.equal(registrationMail(parsed,'child@icloud.com'),true);
  assert.equal(extractCode(parsed.subject+'\n'+parsed.body),'005239');
  assert.equal(registrationMail(parsed,'other@icloud.com'),false);
  for(const originalSender of ['', 'noreply@openai.com.evil.example', 'noreply@notopenai.com']) {
    assert.equal(registrationMail({...parsed,originalSender},'child@icloud.com'),false);
  }
  assert.equal(registrationMail({...parsed,hmeRecipient:''},'child@icloud.com'),false);
});

test('code extraction ignores CSS, scripts and invitation-only emails', async () => {
  const {extractCode}=await mail;
  assert.equal(extractCode('<style>.x{color:#123456}</style><script>987654</script>Verification code <b>654321</b>'),'654321');
  assert.equal(extractCode('Your verification code is 000000'),null);
  assert.equal(extractCode('<a href="https://chatgpt.com/invite/123456">Join workspace</a>'),null);
  assert.equal(extractCode('654321 is your security code'),'654321');
});

test('direct Cloudflare request and stale/other/used mail filtering', async () => {
  const {startMailbox,pollMailbox,fetchMessages}=await mail;
  const oldFetch=globalThis.fetch;
  let list=[{id:1,address:'child@icloud.com',source:'noreply@openai.com',subject:'Verification code: 123456'}];
  const requests=[];
  globalThis.fetch=async (url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({results:list})};};
  try {
    const baseline=await startMailbox(MAILBOX,'child@icloud.com');
    list=[...list,{id:2,address:'other@icloud.com',source:'noreply@openai.com',subject:'Verification code: 222222'},
      {id:3,address:'child@icloud.com',source:'noreply@openai.com',subject:'Verification code: 333333',created_at:'2000-01-01T00:00:00Z'},
      {id:4,address:'child@icloud.com',source:'noreply@openai.com',subject:'Verification code: 654321'}];
    assert.equal(await pollMailbox(MAILBOX,'child@icloud.com',baseline,[]),'654321');
    assert.equal(await pollMailbox(MAILBOX,'child@icloud.com',baseline,['654321']),null);
    const request=requests[0];
    assert.equal(new URL(request.url).pathname,'/admin/mails');
    assert.equal(new URL(request.url).searchParams.get('address'),MAILBOX.address);
    assert.equal(request.options.headers['x-admin-auth'],'fixture-secret');
    assert.equal(request.options.credentials,'omit');
    assert.equal(request.options.redirect,'error');
    await assert.rejects(()=>fetchMessages({...MAILBOX,baseUrl:'https://evil.example'},'child@icloud.com'));
  } finally {globalThis.fetch=oldFetch;}
});

const TEAM48 = {baseUrl:'https://48team.xiaozhudf2026.foo',token:'x'.repeat(32)};
const until = async (check, label, timeout=3000) => {
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline) { if(await check()) return; await new Promise(resolve=>setTimeout(resolve,5)); }
  throw new Error('timed out: '+label);
};
function team48Server({joinAfter=0, complete}={}) {
  const calls=[]; let syncs=0;
  const fetch=async (url,options)=>{
    const body=options.body ? JSON.parse(options.body) : null;
    calls.push({url,options,body});
    assert.equal(options.headers.Authorization,`Bearer ${TEAM48.token}`);
    assert.equal(options.credentials,'omit');
    const path=new URL(url).pathname;
    const reply=value=>({ok:true,status:200,json:async()=>value});
    if(path==='/api/ext/workspaces') return reply({ok:true,items:[{id:3,name:'Alpha'}]});
    if(path==='/api/ext/handoff' && !body.sync_operation_id) return reply({ok:true,state:'syncing',operation_id:`op-${++syncs}`});
    if(path==='/api/ext/handoff') {
      if(syncs<=joinAfter) return reply({ok:false,state:'not_joined',error_code:'member_not_joined',message:'invited'});
      return reply({ok:true,state:'authorize',account_id:11,ticket:'ticket-123',
        authorize_url:'https://auth.openai.com/oauth/authorize?state=abc&login_hint=test%40icloud.com',workspace:{id:3,name:'Alpha',names:['Alpha','Alpha Official']}});
    }
    if(path==='/api/ext/handoff/complete') return complete ? complete(body) : reply({ok:true,message:'test@icloud.com 授权已更新',
      followups:{sub2api:{ok:true,message:'Sub2API 推送完成'},switch_count:{ok:true,counted:true,message:'今日切换 +1，现为 2 次'}}});
    throw new Error('unexpected '+path);
  };
  return {fetch,calls};
}
async function signedUp(w) {
  await w.send({type:'start',email:'test@icloud.com',handoff:{workspaceId:3,workspaceName:'Alpha'}});
  const chatgpt={...w.content,url:'https://chatgpt.com/'};
  await w.send({type:'complete',jobId:w.session.job.id,email:'test@icloud.com'},chatgpt);
}
const CALLBACK='http://localhost:1455/auth/callback?code=c&state=abc';

test('handoff runs sync, link and OAuth in the signup tab, then submits the callback once', async () => {
  const server=team48Server();
  const w=await worker({team48:TEAM48,fetch:server.fetch});
  assert.equal((await w.send({type:'view'})).team48,true);
  assert.equal(JSON.stringify((await w.send({type:'workspaces'})).items),JSON.stringify([{id:3,name:'Alpha'}]));
  await signedUp(w);
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing');
  const job=w.session.job;
  assert.equal(job.phase,'oauth');
  assert.equal(job.status,'running');
  assert.equal(JSON.stringify(job.attempts),'{}');
  assert.equal(JSON.stringify(job.handoff.workspaceNames),'["Alpha","Alpha Official"]');
  assert.ok(w.updatedTabs.some(tab=>tab.id===7 && tab.url.startsWith('https://auth.openai.com/oauth/authorize')));
  assert.equal(server.calls.filter(call=>call.url.endsWith('/api/ext/handoff')).length,2);
  // The page learns the phase and which team to choose.
  const state=await w.send({type:'state',jobId:job.id},w.content);
  assert.equal(state.phase,'oauth');
  assert.equal(JSON.stringify(state.workspaceNames),'["Alpha","Alpha Official"]');
  // Callbacks from other tabs, hosts or paths are ignored.
  w.navigation.onBeforeNavigate.cb({tabId:9,frameId:0,url:CALLBACK});
  w.handlers.updated(7,{url:'http://localhost:1455/other?code=c&state=abc'});
  w.handlers.updated(7,{url:'https://evil.example/auth/callback?code=c&state=abc'});
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(w.session.job.handoff.status,'authorizing');
  // Several navigation events for one redirect submit it only once.
  w.navigation.onBeforeNavigate.cb({tabId:7,frameId:0,url:CALLBACK});
  w.navigation.onErrorOccurred.cb({tabId:7,frameId:0,url:CALLBACK});
  w.handlers.updated(7,{url:CALLBACK});
  await until(()=>w.session.job.handoff.status==='done','done');
  const completes=server.calls.filter(call=>call.url.endsWith('/complete'));
  assert.equal(completes.length,1);
  assert.deepStrictEqual(completes[0].body,{account_id:11,workspace_id:3,ticket:'ticket-123',callback_url:CALLBACK,push_sub2api:true,count_switch:true});
  const view=(await w.send({type:'view'})).job;
  assert.equal(view.handoff.followups.switchCount.message,'今日切换 +1，现为 2 次');
  assert.equal(view.codexResult,'no_phone');
  assert.ok(!JSON.stringify(w.session.job.handoff).includes('ticket-123'));
  assert.ok(!JSON.stringify(w.session.job.handoff).includes('code=c'));
  assert.ok(w.updatedTabs.some(tab=>tab.id===7 && tab.url.endsWith('popup.html')));
});

test('an invited member is rechecked until joined; without a token nothing is sent', async () => {
  const server=team48Server({joinAfter:1});
  const w=await worker({team48:TEAM48,fetch:server.fetch});
  await signedUp(w);
  await until(()=>w.session.job.handoff?.status==='waiting_join','waiting');
  assert.match(w.session.job.handoff.message,/接受邀请/);
  w.session.job.handoff.retryAt=0;
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing after join',5000);
  const off=await worker();
  assert.equal((await off.send({type:'view'})).team48,false);
  await off.send({type:'start',email:'test@icloud.com',handoff:{workspaceId:3}});
  assert.equal(off.session.job.autoHandoff,null);
  assert.equal((await off.send({type:'workspaces'})).ok,false);
});

test('a failed submission keeps the callback for an explicit retry; a consumed callback stops', async () => {
  let attempt=0;
  const server=team48Server({complete:()=>{
    attempt++;
    if(attempt===1) throw new Error('network');
    return {ok:true,status:200,json:async()=>({ok:false,error_code:'callback_consumed',message:'used'})};
  }});
  const w=await worker({team48:TEAM48,fetch:server.fetch});
  await signedUp(w);
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing');
  w.navigation.onBeforeNavigate.cb({tabId:7,frameId:0,url:'http://127.0.0.1:1455/auth/callback?code=c&state=abc'});
  await until(()=>w.session.job.handoff.status==='callback_retry','retry offered');
  assert.equal(w.session.job.handoff.callbackUrl,'http://127.0.0.1:1455/auth/callback?code=c&state=abc');
  await w.send({type:'handoff-retry'});
  await until(()=>w.session.job.handoff.status==='failed','consumed');
  assert.match(w.session.job.handoff.message,/回调已提交过一次/);
  assert.equal(w.session.job.handoff.callbackUrl,undefined);
  assert.equal(attempt,2);
});

test('closing the tab during authorization fails the handoff; a manual restart works', async () => {
  const server=team48Server();
  const w=await worker({team48:TEAM48,fetch:server.fetch});
  await signedUp(w);
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing');
  w.handlers.removed(7);
  await until(()=>w.session.job.handoff.status==='failed','closed');
  w.navigation.onBeforeNavigate.cb({tabId:7,frameId:0,url:CALLBACK});
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(server.calls.filter(call=>call.url.endsWith('/complete')).length,0);
  await w.send({type:'handoff-start',workspaceId:3,workspaceName:'Alpha'});
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing again');
});

test('manual completion is only offered after signup forms and starts the chosen handoff', async () => {
  const server=team48Server();
  const w=await worker({team48:TEAM48,fetch:server.fetch});
  await w.send({type:'start',email:'test@icloud.com',handoff:{workspaceId:3,workspaceName:'Alpha'}});
  const jobId=w.session.job.id;
  const early=await w.send({type:'mark-complete'});
  assert.equal(early.ok,false);
  assert.equal(w.session.job.status,'running');
  assert.equal((await w.send({type:'view'})).job.formSeen,false);
  await w.send({type:'event',event:'page',stage:'profile',jobId},{...w.content,url:'https://auth.openai.com/about-you'});
  await w.send({type:'event',event:'session_anonymous',stage:'home',jobId},{...w.content,url:'https://chatgpt.com/'});
  assert.equal((await w.send({type:'view'})).job.formSeen,true);
  w.session.job.status='paused';
  assert.equal((await w.send({type:'mark-complete'})).ok,true);
  assert.equal(w.session.job.status,'done');
  assert.equal(w.session.job.events.at(-1).event,'manual_complete');
  assert.ok(w.session.job.events.some(entry=>entry.event==='session_anonymous'));
  await until(()=>w.session.job.handoff?.status==='authorizing','authorizing after manual completion');
  assert.equal((await w.send({type:'mark-complete'})).ok,false);
});
