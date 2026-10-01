// Regenerate only against the original, unchanged uploaded collector. Node is
// needed for fixture generation, never for the Python executor or its tests.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(process.argv[2]);
const {FocusCollector} = require(path.join(root, 'accessibility.js'));
const {SUPPORTED_VERSION} = require(path.join(root, 'prime-focus-burst.js'));
const input='TYPE_VIEW_FOCUSED', access='TYPE_VIEW_ACCESSIBILITY_FOCUSED';
const window='TYPE_WINDOW_STATE_CHANGED', clear='TYPE_VIEW_ACCESSIBILITY_FOCUS_CLEARED';
const event=(at,type,text='',o={})=>`EventType: ${type}; EventTime: ${at}; PackageName: ${o.pkg??'com.amazon.firebat'}; ContentChangeTypes: [${o.content??''}]; WindowChangeTypes: [${o.windows??''}] [ ClassName: ${o.klass??'button'}; Text: [${text}]; ContentDescription: ${o.description??'null'}; Enabled: ${o.enabled??true}; FullScreen: ${o.fullScreen??false}`;
const emit=(at,type,text,o)=>({op:'event',line:event(at,type,text,o)});
const pair=(title='Any occupant',o={})=>[emit(101,access,title,o),emit(102,input,title,o)];
const echo=(title='Any occupant',o={})=>emit(103,window,title,o);
const cleanup=(at=104,o={})=>emit(at,window,'',{klass:'android.view.View',enabled:false,...o});
const closure=(at=105)=>emit(at,'TYPE_WINDOW_CONTENT_CHANGED','',{klass:'android.view.View',content:'CONTENT_CHANGE_TYPE_SUBTREE'});
const cases=[];
function add(name,steps,version=SUPPORTED_VERSION){
 let now=0; const collector=new FocusCollector({now:()=>now,appVersion:version});
 const all=[{op:'action',action:'RIGHT',device_time:100},...steps];
 try{
  for(const step of all){
   if(step.op==='action') collector.beginAction(step.action,step.device_time);
   else if(step.op==='event') collector.ingest(step.line);
   else if(step.op==='time') now=step.now;
   else if(step.op==='invalidate') collector.invalidate();
   step.expected=JSON.parse(JSON.stringify(collector.snapshot()));
  }
  cases.push({name,version,steps:all});
 }finally{collector.stop();}
}
for(const title of ['Unseen TV show','42','未見の作品','Filter value'])for(const klass of ['button','','custom.Control'])
 add(`complete burst: ${title}/${klass}`,[...pair(title,{klass}),echo(title,{klass}),cleanup(104),cleanup(105),closure(106)]);
for(const version of [null,'future-version'])add(`unknown version: ${version}`,[...pair(),echo(),cleanup(),closure()],version);
for(const content of ['CONTENT_CHANGE_TYPE_PANE_APPEARED','CONTENT_CHANGE_TYPE_PANE_DISAPPEARED','CONTENT_CHANGE_TYPE_PANE_TITLE'])
 add(content,[...pair(),echo(),cleanup(104,{content}),closure()]);
for(const windows of ['WINDOWS_CHANGE_ADDED','WINDOWS_CHANGE_REMOVED','WINDOWS_CHANGE_ACTIVE','WINDOWS_CHANGE_FOCUSED'])
 add(windows,[...pair(),echo(),emit(104,'TYPE_WINDOWS_CHANGED','',{pkg:'null',windows}),cleanup(106),closure(107)]);
for(const [name,step] of Object.entries({
 enabled:cleanup(104,{enabled:true}),fullscreen:cleanup(104,{fullScreen:true}),
 wrong_class:cleanup(104,{klass:'other.View'}),window_flag:cleanup(104,{windows:'WINDOWS_CHANGE_ACTIVE'}),
 text:emit(104,window,'Dialog',{klass:'android.view.View',enabled:false}),
 missing_flags:{op:'event',line:event(104,window,'',{klass:'android.view.View',enabled:false}).replace('ContentChangeTypes: []; ','')},
}))add(`malformed cleanup: ${name}`,[...pair(),echo(),step,closure()]);
add('cleanup without echo',[...pair(),cleanup(),closure()]);
add('single channel cannot recognize burst',[emit(101,input,'Any occupant'),echo(),cleanup(),closure()]);
add('wrong echo subject',[...pair(),echo('Another subject'),cleanup(),closure()]);
add('cleanup after closure',[...pair(),echo(),closure(),cleanup(106)]);
add('out of order closure',[...pair(),echo(),cleanup(110),closure(104)]);
add('out of order cleanup',[...pair(),echo(),cleanup(110),cleanup(104)]);
add('new subject after burst',[...pair(),echo(),cleanup(),closure(),emit(120,access,'New'),emit(121,input,'New')]);
add('expired receipt anchor',[...pair(),echo(),{op:'time',now:121},cleanup()]);
add('expired device anchor',[...pair(),echo(),...[110,150,200,222].map(n=>cleanup(n))]);
add('clear inside burst',[...pair(),echo(),emit(104,clear,'Any occupant'),closure()]);
add('input cancels burst',[...pair(),echo(),{op:'action',action:'DOWN',device_time:105},cleanup(106)]);
add('disconnect cancels burst',[...pair(),echo(),{op:'invalidate'},cleanup(106)]);
add('same-millisecond hard boundary',[...pair(),emit(102,window,'Actual pane',{fullScreen:true})]);
add('channel disagreement and clear',[emit(101,input,'Personal Shopper'),emit(102,access,'Customers also watched'),emit(103,clear,'Customers also watched')]);
add('clear ordering',[emit(101,access,'A'),emit(102,clear,'A'),emit(101,access,'A'),emit(103,access,'B'),emit(104,clear,'A')]);
add('independent channel watermarks',[emit(102,access,'Row'),emit(101,input,'Tile')]);
add('matching descriptions preserved',[emit(101,access,'A',{description:'[Main menu] A'}),emit(101,input,'A'),emit(101,input,'A')]);
add('same label different descriptions',[emit(101,input,'A',{description:'[One] A'}),emit(102,access,'A',{description:'[Two] A'})]);
add('ambiguous and unlabeled focus',[emit(101,input,'A'),emit(101,input,'B'),emit(102,input,'C'),emit(103,input,'',{klass:''}),emit(104,access,'A'),emit(105,input,'A')]);
add('content changes and generic startup',[emit(101,input,'A'),emit(102,'TYPE_WINDOW_CONTENT_CHANGED'),emit(103,window),emit(104,input,'B'),emit(105,input,'',{klass:'android.view.View'}),emit(106,input,'',{klass:'IgniteView'})]);
add('expired labels',[emit(101,input,'A'),{op:'time',now:60001}]);
add('delayed old event',[{op:'time',now:60002},emit(101,input,'A')]);
add('negative host age',[emit(101,input,'A'),{op:'time',now:-1}]);
add('wrong package',[emit(101,input,'A'),emit(102,input,'Home',{pkg:'com.amazon.tv.launcher'})]);
add('clock unavailable',[emit(101,input,'A'),{op:'action',action:'DOWN'},emit(10000,input,'B')]);
add('strict boundary',[emit(99,input,'Before'),emit(100,input,'At'),emit(101,input,'After')]);
add('unlabeled diagnostics survive hard boundary',[emit(101,input,'A'),emit(102,input,'',{klass:''}),emit(103,window,'Pane'),cleanup(104),{op:'action',action:'UP',device_time:110}]);
const sources=['accessibility.js','prime-focus-burst.js','semantic-evidence.js'].map(file=>({file,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(root,file))).digest('hex')}));
fs.writeFileSync(process.argv[3],JSON.stringify({schema:'archived-prime-collector-conformance-v1',sources,cases},null,2)+'\n');
console.log(`${cases.length} scenarios; ${cases.reduce((n,c)=>n+c.steps.length,0)} reference snapshots`);
