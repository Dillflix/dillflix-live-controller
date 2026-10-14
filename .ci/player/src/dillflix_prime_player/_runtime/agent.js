/* Frida 17+; all application evaluation occurs on an intercepted owning thread.
 * The payload is supplied by run_probe.py. No native callbacks are stored in the app.
 */
'use strict';
const payload=__PV_PAYLOAD__, options=payload.options, profile=options.profile;
let active=false, fatal=false, matchedConfig=false, testLoaded=false, stopping=false;
let testBase=null,nativeImageEpoch=0;
let contexts=new Map(), threads=new Map(), hooks=[], moduleObserver=null;
let controlPending=null,controlContext=null,ownerEpoch=0;
let engineModule=null,light=null,stableGate=null,nativeFailure=null,lastMemorySample=null;
const adaptationManifests=new Map(),adaptationDescriptorRevisions=new Map();
function captureAdaptation(ctx,revision){
  const key=ctx.toString();if(adaptationDescriptorRevisions.get(key)===revision)return;
  try{const descriptor=JSON.parse(evaluate(ctx,'JSON.stringify(globalThis.__pvBootstrap.nativeDescriptor?globalThis.__pvBootstrap.nativeDescriptor():null)'));
    if(descriptor)adaptationManifests.set(key,{engineBuildId:profile.engine.buildId,descriptor:descriptor});
    else adaptationManifests.delete(key);
    adaptationDescriptorRevisions.set(key,revision);
  }catch(_){adaptationManifests.delete(key);adaptationDescriptorRevisions.delete(key);}
}
function compatibility(ctx){
  metrics.compatibilityChecks++;
  if(testLoaded&&testBase){try{verifyNativeImage(testBase);}catch(_){testLoaded=false;testBase=null;nativeImageEpoch++;nativeFailure="native_image_unloaded";}}
  const source="JSON.stringify((function(){if(!globalThis.__pvCompat)return null;var b=globalThis.__pvBootstrap;b.refresh(false);globalThis.__pvCompat.native("+testLoaded+","+JSON.stringify(nativeFailure||'binary_not_observed')+");return {snapshot:globalThis.__pvCompat.poll(),descriptorRevision:b.nativeDescriptorRevision?b.nativeDescriptorRevision():null};})())";
  const envelope=JSON.parse(evaluate(ctx,source)),value=envelope&&envelope.snapshot;
  // Capture before public snapshot deduplication: URL-only changes intentionally
  // produce no compatibility report and must not fence active commands.
  if(envelope)captureAdaptation(ctx,envelope.descriptorRevision);
  if(value){metrics.compatibilityReports++;value.layers.engine={available:active&&!fatal,reasons:active&&!fatal?[]:['engine_unverified'],buildId:profile.engine.buildId,architecture:Process.arch,pointerSize:Process.pointerSize};const key=ctx.toString();report('compatibility',{context:key,authoritative:controlContext===key,snapshot:value});}
  return value;
}
function memorySample(ctx){
  const now=Date.now();if(lastMemorySample!==null&&now-lastMemorySample<30000)return;
  lastMemorySample=now;
  try{
    const app=JSON.parse(evaluate(ctx,'JSON.stringify(globalThis.__pvAppProbe.memory())'));
    report('runtime_memory',{timeMs:now,appPid:Process.id,fridaHeapBytes:Frida.heapSize,
      evaluations:metrics.evaluations,compatibilityChecks:metrics.compatibilityChecks,
      compatibilityReports:metrics.compatibilityReports,nativeTestRuntimeVerified:testLoaded,app:app});
  }catch(error){report('runtime_memory_error',{timeMs:now,message:String(error).slice(0,300)});}
}
let api=null, metrics={boundaries:0,evaluations:0,contexts:0,compatibilityChecks:0,compatibilityReports:0};
function report(kind,data){send({schema:'prime-frida-experiment/1',timeMs:Date.now(),kind:kind,data:data});}
function fail(error){fatal=true;report('fatal',{message:String(error)});}
function hex(pointer,length){return Array.from(new Uint8Array(pointer.readByteArray(length)),x=>x.toString(16).padStart(2,'0')).join('');}
function verify(module,checks){for(const c of checks)if(hex(module.base.add(c.offset),c.hex.length/2)!==c.hex)throw new Error('Fingerprint mismatch: '+module.name+' '+c.name);}
function verifyNativeImage(base){
  // Both linker notifications and anonymous-image discovery require identical
  // mapped-byte evidence. Checking the entire span prevents cross-range reads.
  const entries=profile.testRuntime.checks.concat(profile.testRuntime.executableAnchors);
  for(const c of entries){
    const at=base.add(c.offset),range=Process.findRangeByAddress(at),executable=profile.testRuntime.executableAnchors.indexOf(c)>=0;
    if(!range||range.protection.indexOf('r')<0||(executable&&range.protection.indexOf('x')<0)||at.add(c.hex.length/2).compare(range.base.add(range.size))>0||hex(at,c.hex.length/2)!==c.hex)
      throw new Error('Native image fingerprint mismatch');
  }
}
function acceptNativeImage(base,mechanism){
  verifyNativeImage(base);testBase=base;testLoaded=true;nativeFailure=null;
  report('test_runtime_verified',{buildId:profile.testRuntime.buildId,mechanism:mechanism,anchorCount:profile.testRuntime.executableAnchors.length});
}
function tag(v){return v.shr(32).toNumber()>>>0;}
function release(ctx,value){
  if(tag(value)>=0xfffffff5){const p=ptr(value.and('0xffffffff').toString());const count=p.readS32();if(count<1)throw new Error('Invalid QuickJS reference count');p.writeS32(count-1);if(count===1)api.freeValue(ctx,value);}
}
function stringValue(ctx,value){
  const len=Memory.alloc(4);len.writeU32(0);let s=api.toString(ctx,len,value,0);
  if(s.isNull())throw new Error('JS_ToCStringLen2 failed');
  try{const n=len.readU32();if(n>2200000)throw new Error('Evaluation result exceeds transport limit');return s.readUtf8String(n);}finally{api.freeString(ctx,s);}
}
function evaluate(ctx,source){
  metrics.evaluations++;
  const buffer=Memory.allocUtf8String(source), filename=Memory.allocUtf8String('pv-runtime-experiment.js');
  // Payloads are ASCII (the Python packer uses ensure_ascii), so length is byte length.
  const value=api.eval(ctx,buffer,source.length,filename,0);
  if(tag(value)===6){const error=api.exception(ctx);let description;try{description=stringValue(ctx,error);}finally{release(ctx,error);}throw new Error(description);}
  try{return stringValue(ctx,value);}finally{release(ctx,value);}
}
function stateFor(ctx){const key=ctx.toString();let s=contexts.get(key);if(!s){s={ctx:ctx,bootstrap:false,app:false,lastPump:0,lastBootstrap:'',errors:0};contexts.set(key,s);metrics.contexts=contexts.size;}return s;}
function bootstrap(s){
  if(s.bootstrap&&evaluate(s.ctx,'String(!!globalThis.__pvBootstrap)')==='true')return;
  s.app=false;
  // An initial scalar evaluation tests the inferred ABI before allocating app objects.
  if(evaluate(s.ctx,"'pv-abi-ok'")!=='pv-abi-ok')throw new Error('QuickJS ABI smoke check failed');
  if(payload.roles)evaluate(s.ctx,'JSON.stringify('+payload.roles+')');
  const modules=JSON.parse(evaluate(s.ctx,'JSON.stringify('+payload.modules+')'));
  report('module_observer_hook',{context:s.ctx.toString(),status:modules});
  if(options.capabilities&&!options.lightweight){
    const runtime=JSON.parse(evaluate(s.ctx,'JSON.stringify('+payload.runtimes+')'));
    report('runtime_observer_installed',{context:s.ctx.toString(),status:runtime});
  }
  const r=JSON.parse(evaluate(s.ctx,'JSON.stringify('+payload.bootstrap+')'));
  s.bootstrap=true;report('bootstrap_hook',{context:s.ctx.toString(),status:r});
}
function pump(s,force){
  const now=Date.now();if(now-s.lastPump<1000)return;s.lastPump=now;s.pumps=(s.pumps||0)+1;
  if(!s.bootstrap)bootstrap(s);
  let status=JSON.parse(evaluate(s.ctx,'JSON.stringify(globalThis.__pvBootstrap?globalThis.__pvBootstrap.status():null)'));
  if(!status){s.bootstrap=false;s.app=false;bootstrap(s);status=JSON.parse(evaluate(s.ctx,'JSON.stringify(globalThis.__pvBootstrap.status())'));}
  const wire=JSON.stringify(status);if(wire!==s.lastBootstrap){s.lastBootstrap=wire;report('bootstrap_status',{context:s.ctx.toString(),status:status});}
  if(status.matched)matchedConfig=true;
  s.matched=!!status.matched;
  compatibility(s.ctx);
  if(options.capabilities&&!options.lightweight){
    const batch=JSON.parse(evaluate(s.ctx,'JSON.stringify(globalThis.__pvRuntimeObserver?globalThis.__pvRuntimeObserver.tick():{records:[],dropped:0})'));
    if(batch.records.length||batch.dropped)report('runtime_observations',{context:s.ctx.toString(),runtimeMatched:!!status.matched,batch:batch});
  }
  if(options.capabilities&&!options.lightweight&&now-(s.lastCoverageInventory||0)>=5000){
    s.lastCoverageInventory=now;
    const source="JSON.stringify((function(){var g=globalThis,names=Object.getOwnPropertyNames(g),spaces={};['Native','DirectNative','VmManager','AVPK','Ignite','Java','WebAssembly','ATVUnfPlayerBundle'].forEach(function(k){var d=Object.getOwnPropertyDescriptor(g,k);if(d&&Object.prototype.hasOwnProperty.call(d,'value')&&d.value&&(typeof d.value==='object'||typeof d.value==='function'))spaces[k]=Object.getOwnPropertyNames(d.value).slice(0,300);});return {globalNames:names.slice(0,600),globalNameCount:names.length,namespaces:spaces,webpackGlobals:names.filter(function(k){return /webpack|photon|chunk/i.test(k);}),moduleStatus:g.__pvModules?g.__pvModules.status():null,moduleCatalog:g.__pvModules&&g.__pvModules.catalog?g.__pvModules.catalog():null};})())";
    const inventory=JSON.parse(evaluate(s.ctx,source)),wire=JSON.stringify({inventory:inventory,runtimeMatched:!!status.matched});
    if(wire!==s.lastCoverageWire){s.lastCoverageWire=wire;report('context_coverage',{context:s.ctx.toString(),runtimeMatched:!!status.matched,inventory:inventory,scope:'Read-only namespace inventory; this is not a claim that every runtime or handler was intercepted.'});}
  }
  if(stopping){
    const result=evaluate(s.ctx,'JSON.stringify({bootstrap:globalThis.__pvBootstrap.restore(),probe:globalThis.__pvAppProbe?globalThis.__pvAppProbe.stop():null,runtime:globalThis.__pvRuntimeObserver?globalThis.__pvRuntimeObserver.stop():null,modules:globalThis.__pvModules?globalThis.__pvModules.restore():null})');
    report('context_stopped',{context:s.ctx.toString(),result:JSON.parse(result)});s.stopped=true;return;
  }
  if(!status.matched){
    if(now-(s.lastInventory||0)>=5000){
      s.lastInventory=now;
      const source="JSON.stringify({hasPhoton:typeof globalThis.webpackChunkPhoton!=='undefined',configType:typeof globalThis.config,globalNames:Object.getOwnPropertyNames(globalThis).slice(0,256),nativeNames:typeof Native==='object'&&Native?Object.getOwnPropertyNames(Native).slice(0,160):[],vmManagerNames:typeof VmManager==='object'&&VmManager?Object.getOwnPropertyNames(VmManager).slice(0,80):[]})";
      const inventory=JSON.parse(evaluate(s.ctx,source)), wire=JSON.stringify(inventory);
      if(wire!==s.lastInventoryWire){s.lastInventoryWire=wire;report('runtime_inventory',{context:s.ctx.toString(),inventory:inventory,note:'This context has not matched its runtime manifest; application module queries remain blocked.'});}
    }
    return;
  }
  if(!s.app&&now-(s.lastInstall||0)>=1000){s.lastInstall=now;
    const result=JSON.parse(evaluate(s.ctx,'JSON.stringify('+payload.probe+')'));s.app=!!result.installed;
    if(s.app){if(controlContext===null)controlContext=s.ctx.toString();s.waiting=null;report('app_vm_found',{context:s.ctx.toString()});}
    else{const wire=JSON.stringify(result);s.waiting=result;if(wire!==s.lastWaiting){s.lastWaiting=wire;report('app_probe_waiting',{context:s.ctx.toString(),status:result});}}
  }
  if(s.app&&controlPending&&s.ctx.toString()===controlContext){
    const c=controlPending;controlPending=null;
    if(Date.now()>c.expiresAt)report('control_result',{id:c.command.id,success:false,error:'command expired before owning-thread execution'});
    else{try{const source='JSON.stringify(globalThis.__pvAppProbe.command('+JSON.stringify(c.command).replace(/[\u007f-\uffff]/g,x=>'\\u'+x.charCodeAt(0).toString(16).padStart(4,'0'))+'))';
      const result=JSON.parse(evaluate(s.ctx,source));report('control_scheduled',{id:c.command.id,result:result});
    }catch(error){report('control_result',{id:c.command.id,success:false,error:String(error)});}}
  }
  if(s.app){
    const result=JSON.parse(evaluate(s.ctx,'JSON.stringify(globalThis.__pvAppProbe.tick({nativeVerified:'+testLoaded+'}))'));
    forwardBatch(s.ctx,result);
    if(options.lightweight&&light&&!light.ready()){
      const health=JSON.parse(evaluate(s.ctx,'JSON.stringify(globalThis.__pvAppProbe.health())'));
      light.handoff(s,health);
      if(light.ready())evaluate(s.ctx,'String(globalThis.__pvCompat.freeze())');
    }
  }
}
function forwardBatch(ctx,result){
  if(Array.isArray(result.completions)){for(const c of result.completions)report('control_result',c);}
  else for(const r of result.records||[])if(r.kind==='control_completion')report('control_result',r.data);
  if((result.records||[]).length||result.dropped)report('observations',{context:ctx.toString(),batch:result});
}
function lightExchange(ctx,command){
  if(fatal||!active)throw new Error('engine unavailable; no evaluation performed');
  if(command&&command._ownershipFence!==undefined){
    const result=JSON.parse(evaluate(ctx,'JSON.stringify(globalThis.__pvAppProbe.fence('+command._ownershipFence+'))'));
    report('ownership_fence',result);return result;
  }
  if(command&&options.ownership&&command.ownerEpoch!==ownerEpoch)throw new Error('ownership:stale command epoch');
  compatibility(ctx);
  if(command){
    const encoded=JSON.stringify(command).replace(/[\u007f-\uffff]/g,x=>'\\u'+x.charCodeAt(0).toString(16).padStart(4,'0'));
    const scheduled=JSON.parse(evaluate(ctx,'JSON.stringify(globalThis.__pvAppProbe.command('+encoded+'))'));
    report('control_scheduled',{id:command.id,result:scheduled});
  }
  const result=JSON.parse(evaluate(ctx,'JSON.stringify(globalThis.__pvAppProbe.transport({nativeVerified:'+testLoaded+'}))'));
  forwardBatch(ctx,result.batch);memorySample(ctx);return {health:result.health,commandSubmitted:!!command};
}
function boundary(address,isEval){
  hooks.push(Interceptor.attach(address,{
    onEnter(args){
      this.eligible=false;if(fatal||(light&&light.ready()))return;
      const tid=this.threadId;let t=threads.get(tid);if(!t){t={depth:0,maxDepth:0,returns:0,exceptionReturns:0,busy:false};threads.set(tid,t);}if(t.busy)return;
      this.eligible=true;this.t=t;this.ctx=args[0];t.depth++;t.maxDepth=Math.max(t.maxDepth,t.depth);metrics.boundaries++;
      if(options.mode==='discover'&&!contexts.has(this.ctx.toString())){stateFor(this.ctx);report('context_seen',{context:this.ctx.toString(),thread:tid});}
      if(isEval&&options.mode!=='discover'){
        t.busy=true;try{bootstrap(stateFor(this.ctx));}catch(error){fail(error);}finally{t.busy=false;}
      }
    },
    onLeave(retval){
      if(!this.eligible)return;const t=this.t;t.depth--;t.returns++;
      // Do not disturb an exception being returned by the application.
      if(fatal||t.busy||options.mode==='discover'||(light&&light.ready()))return;
      if(this.context.r1.toUInt32()===6){t.exceptionReturns++;return;}
      // Both early and late VMs may retain long-lived outer calls. Sample a completed
      // inner call on its owning thread; busy prevents recursive probe entry.
      const s=stateFor(this.ctx);s.threadId=this.threadId;if(s.stopped)return;
      if(!s.boundaryReported){s.boundaryReported=true;report('pump_boundary',{context:this.ctx.toString(),thread:this.threadId,remainingDepth:t.depth,isEval:isEval});}
      t.busy=true;try{pump(s,isEval);}catch(error){s.errors++;report('probe_error',{context:this.ctx.toString(),message:String(error),count:s.errors});if(s.errors>=3)fail('Repeated probe errors; stopping evaluation');}finally{t.busy=false;}
    }
  }));
}
function onModule(module){
  if(fatal)return;
  try{
    if(options.capabilities&&!options.lightweight)report('native_module_inventory',{name:module.name,path:module.path,size:module.size});
    if(module.name.indexOf('libamzn_av_living_room_rust_client')>=0){
      report('native_runtime_loaded',{name:module.name,path:module.path});
      if(module.name.indexOf('_test_utils-')>=0){nativeImageEpoch++;try{acceptNativeImage(module.base,'module_anchors_and_build_id');}catch(e){testLoaded=false;testBase=null;nativeFailure='binary_fingerprint_mismatch';report('native_compatibility_unavailable',{reason:nativeFailure});}}
    }
    if(module.name!==profile.engine.name||active)return;
    if(Process.arch!=='arm'||Process.pointerSize!==4)throw new Error('Expected ARM32 process');
    verify(module,profile.engine.checks);active=true;engineModule=module;
    const fn=(name,ret,args)=>new NativeFunction(module.base.add(profile.engine.functions[name]+1),ret,args,{scheduling:'exclusive',exceptions:'propagate'});
    api={eval:fn('JS_Eval','uint64',['pointer','pointer','uint32','pointer','int']),toString:fn('JS_ToCStringLen2','pointer',['pointer','pointer','uint64','int']),freeString:fn('JS_FreeCString','void',['pointer','pointer']),freeValue:fn('__JS_FreeValue','void',['pointer','uint64']),exception:fn('JS_GetException','uint64',['pointer'])};
    if(options.lightweight){
      stableGate=(__PV_STABLE_GATE_FACTORY__)({fail:fail,startup:function(ctx,tid,isEval,phase,tag){
        if(fatal||stopping)return;
        const s=stateFor(ctx);s.threadId=tid;
        try{
          if(phase===0){bootstrap(s);return;}
          metrics.boundaries++;
          if(tag!==6)pump(s,isEval);
        }catch(error){s.errors++;report('probe_error',{context:ctx.toString(),message:String(error),count:s.errors});if(s.errors>=3)fail('Repeated startup probe errors');}
      }});
      light=(__PV_LIGHT_TRANSPORT_FACTORY__)({report:report,
        handoff:function(ctx,tid){stableGate.handoff(ctx,tid);},
        arm:function(callback){return stableGate.request(callback);},
        close:function(){stableGate.close();},exchange:lightExchange});
      stableGate.install(module.base.add(profile.engine.functions.JS_EvalFunction+1),true);
      stableGate.install(module.base.add(profile.engine.functions.JS_CallInternal+1),false);
    }else{
      boundary(module.base.add(profile.engine.functions.JS_EvalFunction+1),true);
      if(options.mode!=='discover'||options.lateAttach)boundary(module.base.add(profile.engine.functions.JS_CallInternal+1),false);
    }
    report('engine_verified',{name:module.name,buildId:profile.engine.buildId,mode:options.mode});
  }catch(error){if(!active){report('engine_unavailable',{reason:String(error)});report('compatibility_degraded',{reason:'engine_adapter_unverified',note:'No engine offsets invoked; Prime remains running.'});}else fail(error);}
}
rpc.exports={
  adaptationmanifest(){
    // Return already-observed data only; never evaluate QuickJS on the RPC thread.
    // Resource URLs remain in this private channel, outside health/diagnostics.
    if(!active||fatal)return null;
    if(controlContext&&adaptationManifests.has(controlContext))return adaptationManifests.get(controlContext);
    const values=Array.from(adaptationManifests.values());
    if(!values.length)return null;
    const first=JSON.stringify(values[0]);
    return values.every(v=>JSON.stringify(v)===first)?values[0]:null;
  },
  async fence(epoch){
    if(!Number.isSafeInteger(epoch)||epoch<ownerEpoch)throw new Error('ownership:stale fence');
    ownerEpoch=epoch;controlPending=null;
    if(!light||!light.ready())throw new Error('ownership:owning-thread cancellation transport unavailable');
    const end=Date.now()+12000;
    while(light.status().transferArmed){if(Date.now()>end)throw new Error('ownership:transfer did not drain');await new Promise(resolve=>setTimeout(resolve,25));}
    return await light.request({_ownershipFence:epoch});
  },
  submitcontrol:function(command){
    if(options.ownership&&command.ownerEpoch!==ownerEpoch)throw new Error('ownership:stale command epoch');
    if(!options.control||fatal||stopping||!controlContext)throw new Error('control VM is not ready');
    if(controlPending)throw new Error('one command is already pending');
    if(!command||typeof command.id!=='string'||!/^[a-f0-9]{32}$/.test(command.id)||JSON.stringify(command).length>32768)throw new Error('invalid command envelope');
    if(options.lightweight&&light&&light.ready()){
      light.request(command).catch(error=>report('control_result',{id:command.id,success:false,error:String(error),note:'Not retried; inspect control_scheduled for any submission.'}));
      return {queued:true,id:command.id,mechanism:'bounded_owning_thread_transfer'};
    }
    controlPending={command:command,expiresAt:Date.now()+10000};return {queued:true,id:command.id};
  },poll(){if(!options.lightweight||!light||!light.ready())return {ready:false};return light.request(null);},status(){return {lightweight:light?light.status():null,active:active,fatal:fatal,matchedConfig:matchedConfig,testLoaded:testLoaded,metrics:metrics,threads:Array.from(threads,([id,t])=>({id:id,depth:t.depth,maxDepth:t.maxDepth,returns:t.returns,exceptionReturns:t.exceptionReturns})),contexts:Array.from(contexts.values(),s=>({context:s.ctx.toString(),pumps:s.pumps||0,matched:!!s.matched,app:s.app,errors:s.errors,waiting:s.waiting||null}))};},stop(){stopping=true;if(light)light.close();hooks.splice(0).forEach(h=>h.detach());return {requested:true};}};
moduleObserver=Process.attachModuleObserver({onAdded:onModule,onRemoved:function(m){if(engineModule&&m.base.equals(engineModule.base)){active=false;report('engine_unavailable',{reason:'engine_unloaded'});}if(m.name.indexOf('_test_utils-')>=0){nativeImageEpoch++;if(testBase&&m.base.equals(testBase)){testLoaded=false;testBase=null;nativeFailure='native_image_unloaded';}}}});
report('agent_ready',{mode:options.mode,enginePresentAtInjection:active,note:'Gated runtime experiment with nested sampling and passive module initialization observation.'});

// Ignite-loaded images may be absent from the Android linker module list.
// Read-only scan: require three executable anchors AND the pinned GNU build-id.
let scanBusy=false, scanAttempts=0;
async function scanNativeImage(){
  if(scanBusy||fatal||stopping||testLoaded||!matchedConfig||options.mode!=='test-utils')return;
  scanBusy=true;scanAttempts++;const scanEpoch=nativeImageEpoch;
  try{
    const anchors=profile.testRuntime.executableAnchors;
    let ranges=Process.enumerateRanges({protection:'r-x',coalesce:false}),bytes=0;
    for(const range of ranges){
      if(testLoaded||stopping||fatal)break;
      if(range.size<64)continue;
      bytes+=range.size;if(bytes>268435456){report('native_image_scan_limit',{attempt:scanAttempts});break;}
      await new Promise(resolve=>Memory.scan(range.base,range.size,anchors[0].hex.match(/../g).join(' '),{
        onMatch(address){
          try{
            const base=address.sub(anchors[0].offset);
            if(scanEpoch!==nativeImageEpoch||stopping||fatal)return 'stop';
            acceptNativeImage(base,'executable_anchors_and_build_id');
            return 'stop';
          }catch(_){}
        },onError(message){report('native_image_scan_error',{message:String(message),base:range.base.toString()});},onComplete(){resolve();}
      }));
    }
    report('native_image_scan_status',{attempt:scanAttempts,bytesScanned:bytes,verified:testLoaded});
  }catch(error){report('native_image_scan_error',{message:String(error)});}finally{scanBusy=false;}
}
if(options.mode==='test-utils')setInterval(scanNativeImage,5000);
if(options.lightweight)setTimeout(function(){
  if(!stopping&&!fatal&&(!light||!light.ready())){
    if(stableGate)stableGate.close();report('compatibility_degraded',{reason:'dispatcher_not_validated_within_90_seconds',note:'Startup callbacks disabled; Prime remains running. No automatic restart.'});
  }
},90000);
