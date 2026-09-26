const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../app/web/static/js', name), 'utf8');
const window = {Team48Runtime: {subscribe() {}}};
vm.runInNewContext(source('task-progress.js'), {window, Date, Map, Set});
const {model, duration, summarizeOperationResult: summary} = window.Team48TaskProgress;
const plan = [{code:'checking',label:'检查成员',stages:['checking']},{code:'hme',label:'领取邮箱',stages:['hme']},
  {code:'inviting',label:'发送邀请',stages:['inviting','skip_invite']},{code:'browser',label:'注册与入组',stages:['browser','sms_otp','browser_failed']}];

test('browser substage shares its parent segment; observed is not success', () => {
  const view = model({state:'running',stage_code:'sms_otp',stage_label:'等待短信验证码',stage_plan:plan,observed_stages:['checking','inviting','sms_otp']});
  assert.equal(view.ordinal, '第 4/4 步');
  assert.equal(view.rows[3].sublabel, '等待短信验证码');
  assert.deepEqual([...view.rows].map(row => row.state), ['visited','pending','visited','current']);
});
test('legacy logs reconstruct interrupted stage without fabricating skipped steps', () => {
  const view = model({state:'failed',current_step:'failed',stage_plan:plan,log:[{stage:'checking'},{stage:'browser_failed'}]});
  assert.equal(view.index, 3); assert.equal(view.rows[3].state, 'failed');
  assert.equal(view.rows[1].state, 'pending');
});
test('only explicit skip or completed step becomes skipped or done', () => {
  const view = model({state:'running',stage_code:'browser',stage_plan:plan,log:[{stage:'skip_invite'}],
    steps:[{step_name:'checking',step_label:'检查成员',state:'success'}]});
  assert.equal(view.rows[0].state, 'done'); assert.equal(view.rows[2].state, 'skipped');
  const generic = model({state:'failed',current_step:'probe',steps:[{step_name:'probe',step_label:'检测',state:'failed',error_message:'失败'}]});
  assert.equal(generic.rows[0].label, '检测'); assert.equal(generic.rows[0].state, 'failed');
});
test('unknown stages stay indeterminate, and finished duration freezes', () => {
  assert.equal(model({state:'running',stage_code:'new-stage',stage_plan:plan}).index, -1);
  assert.equal(duration({started:'2026-09-01T00:00:00Z',finished:'2026-09-01T00:02:10Z'}, Date.parse('2026-09-02')), '2 分 10 秒');
});
test('summary uses confirmed effects without inventing authorization, seats or pushes', () => {
  const joined = summary('onboard', {success:true,status:'active',child:{email:'new@example.com'},pushed:false});
  assert.match(joined, /已入组/); assert.match(joined, /未推送/); assert.doesNotMatch(joined, /授权已完成|席位/);
  assert.match(summary('kick_member', {success:false,partial:true,child:{email:'a@example.com'},pause_result:{ok:false}}), /暂停未确认/);
  assert.match(summary('rotate', {rotated:true,partial:true,success:false,error:'补位闸门未通过'}), /旧号已移出，补位未完成/);
  assert.equal(summary('quota_probe', {success:true}), null);
});

function runtimeEnv() {
  let config, factories = 0, refreshes = 0;
  const window = {Team48Polling:{createPoller(options) {factories++; config=options; return {refresh:async()=>{refreshes++;},destroy(){}};}},
    setInterval:()=>1,clearInterval(){},addEventListener(){}};
  const document = {visibilityState:'visible',addEventListener(){}};
  const fetch = async () => ({ok:true,json:async()=>({id:'op',state:'success',operation:'onboard',finished:'2026-09-01T00:00:02Z'})});
  const context = {window,document,fetch,console,Map,Set,Date};
  vm.runInNewContext(source('runtime-store.js'),context);
  return {runtime:window.Team48Runtime,config:()=>config,context,factories:()=>factories,refreshes:()=>refreshes};
}
test('runtime is a singleton and all subscribers share snapshots and stale state', () => {
  const e=runtimeEnv(), seen=[];
  e.runtime.subscribe((data,stale)=>seen.push([data,stale]));
  e.runtime.subscribe(()=>{});
  vm.runInNewContext(source('runtime-store.js'),e.context);
  assert.equal(e.factories(),1);
  const data={counts:{running:1},active_operations:[{id:'op',state:'running',kind:'onboard'}]};
  e.config().onData(data); e.config().onError();
  assert.equal(seen.at(-1)[0],data); assert.equal(seen.at(-1)[1],true);
  assert.equal(e.runtime.get('op').state,'running');
  assert.equal(e.config().delay({counts:{waiting_user:1}}),2000);
  assert.equal(e.config().delay({counts:{}}),15000);
});
test('synchronous command is tracked across form closure and stale runtime cannot revive completion', async () => {
  const e=runtimeEnv(); let resolve;
  const result=e.runtime.track(7,'onboard',()=>new Promise(r=>{resolve=r;}));
  assert.equal(e.runtime.all().length,1);
  e.config().onData({active_operations:[{id:'op',workspace_id:7,kind:'onboard',state:'running'}],recent_operations:[]});
  assert.equal(e.runtime.all().length,1);
  resolve({operation_id:'op',success:true,status:'active'}); await result;
  e.config().onData({active_operations:[{id:'op',workspace_id:7,kind:'onboard',state:'running'}]});
  assert.equal(e.runtime.get('op').state,'success');
});
test('conflict response cannot turn someone else’s running operation into a failure', async () => {
  const e=runtimeEnv();
  e.config().onData({active_operations:[{id:'existing',workspace_id:7,kind:'rotate',state:'running'}]});
  await e.runtime.track(7,'rotate',async()=>({success:false,error_code:'operation_conflict',operation_id:'existing'}));
  assert.equal(e.runtime.get('existing').state,'running');
  assert.equal(e.runtime.all().length,1);
});
test('network loss preserves uncertainty instead of claiming the operation failed', async () => {
  const e=runtimeEnv();
  await assert.rejects(e.runtime.track(7,'kick_member',async()=>{throw new Error('offline');}));
  assert.equal(e.runtime.all()[0].state,'manual_required');
  assert.match(e.runtime.all()[0].stage_label,/未确认/);
});


test('OAuth browser substages stay under authorization after official join', () => {
  const expanded = [...plan, {code:'authorizing',label:'自动授权',stages:['authorizing','auth_failed']}];
  const view = model({state:'running',stage_code:'sms_otp',stage_plan:expanded,observed_stages:['browser','authorizing','sms_otp']});
  assert.equal(view.index,4);
  assert.equal(view.label,'自动授权');
  assert.match(summary('onboard',{joined:true,authorized:false,partial:true,pushed:false}), /已入组.*授权未完成/);
});
test('cancel acknowledgement is monotonic across a late runtime read', () => {
  const e=runtimeEnv();
  e.runtime.put({id:'op',kind:'onboard',state:'running',can_cancel:false,cancel_requested:true});
  e.config().onData({active_operations:[{id:'op',kind:'onboard',state:'running',can_cancel:true,cancel_requested:false}]});
  assert.equal(e.runtime.get('op').cancel_requested,true);
  assert.equal(e.runtime.get('op').can_cancel,false);
});
test('an uncertain local request is reconciled when its server operation appears later', async () => {
  const e=runtimeEnv();
  await assert.rejects(e.runtime.track(7,'onboard',async()=>{throw new Error('offline');}));
  e.config().onData({active_operations:[{id:'server-op',workspace_id:7,kind:'onboard',state:'running'}]});
  assert.equal(e.runtime.all().length,1);
  assert.equal(e.runtime.all()[0].id,'server-op');
});
