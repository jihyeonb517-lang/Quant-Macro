const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');

// Exercise the exact refresh block shipped in the dashboard with a delayed
// fetch and mocked browser lifecycle. No external packages or network needed.
const html=readFileSync(path.join(__dirname,'../index.html'),'utf8');
const code=html.split('// Live snapshot refresh:')[1].split('// End live snapshot refresh.')[0];
function snapshot(day,value){
 return {generatedAt:`2026-10-${day}T00:00:00Z`,
  metrics:[{id:'tga',date:`2026-10-${day}`,value,points:[[day,value]],sources:[]}],
  regimes:{us:[[day,'expansion']],jp:[]}};
}
function setup(){
 let clock=Date.parse('2026-10-09T00:00:00Z');
 const data=snapshot('05',883.335),metrics=data.metrics,regimes=data.regimes;
 const events={},stamp={},details={open:true};
 let renders=0,requests=0,warnings=0,interval;
 const state={selected:'tga',years:5,zoomRange:[1,2]};
 const context=vm.createContext({
  DATA:data,REGIMES:regimes,
  Date:class extends Date {static now(){return clock;}},
  AbortSignal,
  document:{visibilityState:'visible',querySelector:()=>details,
   addEventListener:(event,fn)=>{events[event]=fn;}},
  window:{setInterval:(fn,ms)=>{interval={fn,ms};},
   addEventListener:(event,fn)=>{events[event]=fn;}},
  fetch:async(_url,options)=>{requests++;assert.equal(options.cache,'no-cache');
   assert.ok(options.signal);return {ok:true,json:async()=>snapshot('07',885.783)};},
  console:{warn:()=>warnings++},$:()=>stamp,QMI18N:{locale:()=> 'en-US'},
  render:()=>{renders++;},renderFooterNav:()=>{},state,
 });
 vm.runInContext('// Live snapshot refresh:'+code,context);
 return {context,data,metrics,regimes,events,details,state,stamp,
  advance:()=>{clock+=61000;},refresh:()=>context.refreshDashboardData(),
  stats:()=>({renders,requests,warnings,interval})};
}
test('a newer snapshot updates TGA, comparison array and regimes without resetting the view',async()=>{
 const h=setup();h.advance();await h.refresh();
 assert.equal(h.data.metrics[0].date,'2026-10-07');
 assert.equal(h.data.metrics[0].value,885.783);
 assert.equal(h.data.metrics,h.metrics);
 assert.equal(h.regimes.us[0][0],'07');
 assert.match(h.stamp.textContent,/10\/7\/2026/);
 assert.deepEqual(h.state,{selected:'tga',years:5,zoomRange:[1,2]});
 assert.equal(h.details.open,true);
 assert.equal(h.stats().renders,1);
});
test('visible tabs poll every five minutes and returning tabs revalidate',async()=>{
 const h=setup();assert.equal(h.stats().interval.ms,300000);
 h.advance();await h.stats().interval.fn();assert.equal(h.stats().requests,1);
 h.advance();await h.events.focus();assert.equal(h.stats().requests,2);
 h.advance();h.events.visibilitychange();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(h.stats().requests,3);
 h.advance();h.events.pageshow({persisted:true});await new Promise(resolve=>setImmediate(resolve));
 assert.equal(h.stats().requests,4);
});
test('hidden tabs and repeated focus events do not issue redundant requests',async()=>{
 const h=setup();h.advance();h.context.document.visibilityState='hidden';await h.refresh();
 assert.equal(h.stats().requests,0);
 h.context.document.visibilityState='visible';await h.refresh();await h.events.focus();
 assert.equal(h.stats().requests,1);
});
test('HTTP failures, invalid JSON and older cached snapshots preserve the displayed data',async()=>{
 for(const result of [{ok:false,status:503},{ok:true,json:async()=>{throw Error('Bad JSON');}},
  {ok:true,json:async()=>({metrics:[]})},
  {ok:true,json:async()=>snapshot('04',1)}]){
  const h=setup();h.context.fetch=async()=>result;h.advance();await h.refresh();
  assert.equal(h.data.metrics[0].value,883.335);
  assert.equal(h.stats().renders,0);
 }
});
test('in-flight requests cannot overlap and a failed request can be retried',async()=>{
 const h=setup();let reject;
 h.context.fetch=()=>new Promise((_resolve,fail)=>{reject=fail;});
 h.advance();const pending=h.refresh();h.advance();await h.refresh();
 reject(Error('timeout'));await pending;
 assert.equal(h.stats().warnings,1);
 h.context.fetch=async()=>({ok:true,json:async()=>snapshot('07',885.783)});
 await h.refresh();assert.equal(h.data.metrics[0].value,885.783);
});
