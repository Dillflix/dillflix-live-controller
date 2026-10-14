/* Control and passive tracing on the matched app VM's owning thread. */
(function(g,options,api){
  'use strict';
  var moduleIds=options.profile.adapter.modules;
  var ownerEpoch=0,lastPlaybackAttempt=null,pageWatch=null;
  function checkOwner(c){if(options.ownership&&c.ownerEpoch!==ownerEpoch)throw new Error('ownership:stale command epoch');}
  g.__pvOwnershipCheck=checkOwner;
  function fence(epoch){
    if(!Number.isSafeInteger(epoch)||epoch<ownerEpoch)throw new Error('ownership:stale fence');
    ownerEpoch=epoch;pageWatch=null;expireCommands('ownership:cancelled before handoff acknowledgement');
    return {epoch:ownerEpoch,acknowledged:true};
  }
  var Focus=api.Focus,Nav=api.Navigation,Page=api.Page,Manager=api.Manager;
  var hooks=[],hooked=[],callId=0,inputId=0,epoch=0,stopped=false,lastTree=0,lastCoverage='';
  var inputStack=[],strategyBusy=false,treeBusy=false,scheduled={},lastPageKey='',pageSince=Date.now(),lastControlState='';
  var dispatchHook=null,dispatchQueue=null,dispatchBusy=false,probeBusy=false,boundaryCount=0,lastBoundary=0,completions=[],capabilities=null,inflight=null;
  var traceUntil=0,lastCheckpoint=0,lastInstall=0,lastControl=null,lastMarker=null;
  var playbackEpoch=0,lastPlaybackEvent=null,recentPlaybackEvents=[];
  var playbackEventSequence=0,lastPlaybackIdle=null,lastPlaybackStartEpoch=0;
  function playbackEvent(e){
    if(!e||e.type!=='AppEvent')return;
    if(e.name==='NAVIGATE_TO_PAGE'||e.name==='PLAYBACK_LOAD'||e.name==='PLAYBACK_CONTENT_SWITCH'){
      playbackEpoch++;lastPlaybackEvent=null;
    }
    var p=api.page(),v=e.params||{};
    if(e.name==='PLAYBACK_LOAD'||e.name==='PLAYBACK_CONTENT_SWITCH'||(e.name==='NAVIGATE_TO_PAGE'&&p.pageType==='RUST_PLAYBACK'))lastPlaybackStartEpoch=playbackEpoch;
    if(/^PLAYBACK_/.test(e.name)||e.name==='NAVIGATE_TO_PAGE')playbackEventSequence++;
    if(e.name==='PLAYBACK_UNLOAD'||(e.name==='PLAYBACK_STATE_CHANGE'&&(v.state==='Stopped'||v.state==='Ended'))){
      lastPlaybackIdle={epoch:playbackEpoch,playbackStartEpoch:lastPlaybackStartEpoch,sequence:playbackEventSequence,name:e.name,state:v.state||null,timeMs:Date.now()};
    }
    if(e.name==='PLAYBACK_STATE_CHANGE'&&(v.state==='Playing'||v.state==='Buffering'))lastPlaybackIdle=null;
    if(/^PLAYBACK_/.test(e.name)||e.name==='NAVIGATE_TO_PAGE'){
      recentPlaybackEvents.push({name:e.name,rawState:typeof v.state==='string'?v.state.slice(0,128):null,timeMs:Date.now(),pageId:p.pageId||null,epoch:playbackEpoch});
      if(recentPlaybackEvents.length>24)recentPlaybackEvents.shift();
    }
    if(p.pageType!=='RUST_PLAYBACK')return;
    if(e.name==='PLAYBACK_STATE_CHANGE'||/^PLAYBACK_FATAL_/.test(e.name)||e.name==='PLAYBACK_EXPECTED_FATAL'){
      lastPlaybackEvent={name:e.name,rawState:typeof v.state==='string'?v.state:null,
        timeMs:Date.now(),pageId:p.pageId||null,epoch:playbackEpoch};
    }
  }
  function playbackSnapshot(){return {epoch:playbackEpoch,page:api.page()};}
  function preparePlayback(op){
    var p=api.page(),t=op.playbackTransition;
    if(!p||typeof p.pageType!=='string'||!p.pageType)throw new Error('current playback page is unknown; launch cancelled');
    if(!t){
      if(playbackEpoch!==op.initialPlayback.epoch||JSON.stringify(p)!==op.page)throw new Error('playback changed while resolving; launch cancelled');
      if(p.pageType!=='RUST_PLAYBACK')return {ready:true,evidence:{state:'already_idle'}};
      if(typeof p.pageId!=='string'||!p.pageId)throw new Error('current playback identity unavailable; launch cancelled');
      // The replacement owns this operation, but still fences the exact page and
      // playback epoch on the app thread. Do not issue repeated Back commands.
      if(g.__pvCompat)g.__pvCompat.assertCommand({op:'stop-playback',compatibilityGeneration:op.command.compatibilityGeneration});
      t=op.playbackTransition={state:'stop_requested',priorTitleId:p.pageId,epoch:playbackEpoch,playbackStartEpoch:lastPlaybackStartEpoch,sequence:playbackEventSequence,
        requestedAtMs:Date.now(),deadlineMs:Math.min(op.deadline,Date.now()+15000)};
      emit('playback_transition',{id:op.command.id,attemptId:op.command.attemptId,transition:bounded(t)});
      Nav.switchToPreviousPage();
      return {ready:false};
    }
    if(lastPlaybackStartEpoch>t.epoch||(p.pageType==='RUST_PLAYBACK'&&(p.pageId!==t.priorTitleId||playbackEpoch!==t.epoch)))throw new Error('playback changed during stop; replacement launch cancelled');
    if(Date.now()>=t.deadlineMs){
      emit('playback_transition',{id:op.command.id,attemptId:op.command.attemptId,phase:'idle_timeout',transition:bounded(t),page:bounded(p),lastIdle:bounded(lastPlaybackIdle),playbackEpoch:playbackEpoch,playbackStartEpoch:lastPlaybackStartEpoch});
      throw new Error('playback stop/idle was not confirmed; replacement launch cancelled');
    }
    // Navigation to the browsing page advances playbackEpoch too. Match the
    // last playback start so either ordering of page exit and unload is valid.
    // A newer playback start is still rejected above, even for the same title.
    if(p.pageType!=='RUST_PLAYBACK'&&p.pageType!=='UNLOADED'&&lastPlaybackIdle&&lastPlaybackIdle.playbackStartEpoch===t.playbackStartEpoch&&lastPlaybackIdle.sequence>t.sequence){
      t.state='idle_confirmed';t.confirmedAtMs=Date.now();t.idleEvent=lastPlaybackIdle.name;
      emit('playback_transition',{id:op.command.id,attemptId:op.command.attemptId,transition:bounded(t)});
      return {ready:true,evidence:t};
    }
    return {ready:false};
  }
  var broadcasts=(__PV_BROADCASTS_FACTORY__)(g,options,{emit:emit,bounded:bounded,check:checkOwner});
  var catalog=(__PV_CATALOG_FACTORY__)(g,options,{emit:emit,bounded:bounded,check:checkOwner});
  var search=(__PV_SEARCH_FACTORY__)(g,options,{page:api.page,emit:emit,bounded:bounded});
  function pageImpression(payload){
    search.impression(payload);
    var w=pageWatch,p=api.page(),now=Date.now();
    if(!w||now>w.until||w.count>=512||p.pageId!==w.pageId)return;
    var event=payload&&payload.ImpressionEntryEvent;
    if(!event||typeof event.eventTimestamp!=='number'||event.eventTimestamp<w.started||event.eventTimestamp>now)return;
    w.count++;emit('page_tile_metadata',{browseId:w.id,page:p,startedTimeMs:w.started,receivedTimeMs:now,metadata:bounded(payload)});
  }
  function tracing(){return !options.lightweight||Date.now()<traceUntil;}
  var optionalIds=[moduleIds.input,moduleIds.native_input,moduleIds.platform,moduleIds.rpc,moduleIds.page_types,moduleIds.event_loop];
  function emit(k,d){api.emit(k,d);}
  function attempt(fn,fallback){try{return fn();}catch(_){return fallback;}}
  function call(o,k){return attempt(function(){return o&&typeof o[k]==='function'?o[k]():undefined;},undefined);}
  function bounded(v){
    var seen=[],losses=[],budget=4000;
    function copy(x,depth,path){
      if(--budget<0){losses.push(path+':budget');return {omitted:'budget'};}
      if(x===null||typeof x==='boolean'||typeof x==='number')return x;
      if(typeof x==='string'){
        if(/^(Atna\||Atnr\||Bearer\s)/i.test(x)){losses.push(path+':redacted');return {omitted:'sensitive_value'};}
        if(/^https?:/i.test(x)&&/[?&](?:token|signature|policy|credential|key-pair-id|x-amz-)/i.test(x)){losses.push(path+':signed_url');return {omitted:'signed_url'};}
        if(x.length>16384){losses.push(path+':string');return {omitted:'string',chars:x.length};}
        if(depth>0&&/^[\s]*[\[{]/.test(x)){
          try{var parsed=JSON.parse(x),before=losses.length,safe=copy(parsed,depth-1,path+'.json');if(losses.length!==before)return JSON.stringify(safe);}catch(_){}
        }
        return x;
      }
      if(x===undefined){losses.push(path+':undefined');return {omitted:'undefined'};}
      if(typeof x!=='object'){losses.push(path+':'+typeof x);return {omitted:typeof x};}
      if(depth<=0||seen.indexOf(x)>=0){losses.push(path+':depth_or_cycle');return {omitted:'depth_or_cycle'};}
      seen.push(x);var out=Array.isArray(x)?[]:{},keys=Object.keys(x);
      keys.slice(0,256).forEach(function(k){
        if(/authorization|cookie|password|token|credential|signature|manifest|license|streamUrl|playbackUrl|sessionId|playbackEnvelope|correlationId|directedId|customerId|deviceId|^pin$/i.test(k)){out[k]={omitted:'sensitive_field'};losses.push(path+'.'+k+':redacted');return;}
        var d=Object.getOwnPropertyDescriptor(x,k);
        if(!d||!Object.prototype.hasOwnProperty.call(d,'value')){out[k]={omitted:'accessor'};losses.push(path+'.'+k+':accessor');}
        else out[k]=copy(d.value,depth-1,path+'.'+k);
      });
      if(keys.length>256)losses.push(path+':keys');seen.pop();return out;
    }
    var value=copy(v,10,'$'),wire=JSON.stringify(value);
    if(wire.length>65536)return {value:{omitted:'trace_size',chars:wire.length},lossless:false,losses:['trace_size']};
    return {value:value,lossless:losses.length===0,losses:losses.slice(0,32)};
  }
  function node(n){if(!n)return null;return {path:call(n,'getFocusPath'),testId:call(n,'getTestID'),focusKey:call(n,'getFocusKey'),isFocused:call(n,'isFocused')};}
  function state(){return {page:api.page(),javascriptFocus:{authoritativeForActiveLayer:Nav.activeLayer==='JavaScript',focusedPath:call(Focus,'getFocusedFocusablePath'),navigablePath:call(Focus,'getFocusedNavigablePath'),focused:node(call(Focus,'getFocusedFocusable'))},inputEpoch:epoch};}
  function hook(obj,key,before,after){
    if(hooks.length>=1024||!obj||typeof obj[key]!=='function'||hooked.some(function(h){return h.obj===obj&&h.key===key;}))return false;
    var own=Object.getOwnPropertyDescriptor(obj,key),original=obj[key];
    if(own&&(!Object.prototype.hasOwnProperty.call(own,'value')||(!own.writable&&!own.configurable)))return false;
    var wrapped=function(){
      if(g.__pvCompat&&!g.__pvCompat.allows('core'))return original.apply(this,arguments);
      var args=arguments,ctx;attempt(function(){ctx=before&&before(args,this);}.bind(this));
      var result,thrown=false,error;
      try{result=original.apply(this,args);return result;}catch(e){thrown=true;error=e;throw e;}
      finally{attempt(function(){if(after)after(args,result,error,thrown,ctx,this);}.bind(this));}
    };
    try{Object.defineProperty(obj,key,{value:wrapped,writable:true,configurable:true,enumerable:own?own.enumerable:false});}
    catch(_){return false;}
    var h={obj:obj,key:key,own:own,original:original,wrapped:wrapped};hooks.push(h);hooked.push(h);return true;
  }
  function traceCall(obj,key,label){return hook(obj,key,function(args){
    var id=++callId;emit('control_call',{callId:id,target:label,phase:'enter',state:state(),arguments:bounded(Array.prototype.slice.call(args))});return id;
  },function(args,result,error,thrown,id){emit('control_call',{callId:id,target:label,phase:'return',state:state(),thrown:thrown,error:thrown?String(error):undefined,result:result&&typeof result.then==='function'?{kind:'promise_unobserved'}:bounded(result)});});}
  function inputEvent(stage,event){
    var Input=g.__pvModules.exports(moduleIds.input),e=event||{};
    return {stage:stage,event:bounded(e),applicationKey:e.key,keyName:Input&&Input.UL&&Input.UL[e.key],state:state()};
  }
  function tapEmitter(emitter,label,press){
    return hook(emitter,'emit',function(args){
      if(press)epoch++;
      if(tracing())emit('input_event',inputEvent(label,args[0]));
    });
  }
  function strategyHook(strategy,ownerPath){
    if(!strategy||typeof strategy.getNextFocused!=='function'||strategyBusy)return;
    hook(strategy,'getNextFocused',function(args){
      if(treeBusy||!tracing())return null;var id=++callId;
      var info={callId:id,ownerPathAtInstallation:ownerPath,inputId:inputStack.length?inputStack[inputStack.length-1]:null,key:args[1]&&args[1].key,before:state(),children:Array.isArray(args[0])?args[0].slice(0,128).map(node):[]};return info;
    },function(args,result,error,thrown,info){if(info)emit('navigation_strategy_result',Object.assign(info,{result:node(result),returnedNull:result===null,returnedUndefined:result===undefined,thrown:thrown,error:thrown?String(error):undefined,coverage:'Actual application invocation; a strategy candidate is not necessarily the final focus destination.'}));});
  }
  function richTree(){
    if(treeBusy)return;treeBusy=true;
    try{
      var dict=Focus.focusPathDict,keys=dict&&typeof dict==='object'?Object.keys(dict):[];
      var result=[],strategies=0;
      keys.slice(0,512).forEach(function(key){
        var n=dict[key],h=n&&n.hostNode,props=h&&h.props;
        var strat=call(n,'getNavStrategy');
        // Hooks observe future real calls; they never ask a live strategy to simulate a move.
        if(tracing())strategyHook(strat,key);if(strat)strategies++;
        var handlers={};['onPress','onPressIn','onPressOut','onFocus','onBlur','onBubbleUpEvent'].forEach(function(k){handlers[k]=!!(props&&typeof props[k]==='function');});
        result.push(Object.assign({dictionaryKey:key,parent:node(n&&n.parent),children:n&&Array.isArray(n.children)?n.children.slice(0,128).map(node):[],activeChildKey:call(n,'getActiveChildKey'),focusable:call(n,'isFocusableComponent'),virtualRoot:call(n,'isVirtualRoot'),handlers:handlers,strategy:strat?{className:attempt(function(){return strat.constructor.name;},null),hasGetNextFocused:typeof strat.getNextFocused==='function',supportsEvent:typeof strat.supportsEvent==='function'}:null},node(n)||{}));
      });
      emit('javascript_page_structure',{state:state(),nodes:result,totalDictionaryEntries:keys.length,truncated:keys.length>512,strategyCount:strategies,coverage:Nav.activeLayer==='JavaScript'?'Active JavaScript focus domain; strategy edges require actual calls.':'JavaScript host focus domain while native UI is active; not authoritative native focus.'});
    }finally{treeBusy=false;}
  }
  function install(){
    var loop=g.__pvModules.exports(moduleIds.event_loop),eq=loop&&loop.eventQueue;
    if(!dispatchHook&&eq&&typeof eq.waitWithTimeout==='function'){
      var oldWait=eq.waitWithTimeout;
      if(hook(eq,'waitWithTimeout',function(){if(!probeBusy){if(options.lightweight&&api.onBoundary)api.onBoundary();dispatchAtBoundary();}},null)){dispatchHook=eq.waitWithTimeout;dispatchQueue=eq;}
      else if(eq.waitWithTimeout!==oldWait)dispatchHook=null;
    }
    traceCall(Nav,'switchPage','Navigation.switchPage');traceCall(Nav,'switchToPreviousPage','Navigation.switchToPreviousPage');traceCall(Nav,'rpcCallToWasm','Navigation.rpcCallToWasm');
    hook(Focus,'processInputEvent',function(args){if(!tracing())return null;var id=++inputId;inputStack.push(id);return {inputId:id,before:state(),event:bounded(args[0]),marker:lastMarker};},function(args,result,error,thrown,info){if(info)inputStack.pop();if(info)emit('javascript_input_edge',Object.assign(info,{after:state(),returnValue:result,metrics:bounded(args[0]&&args[0].metrics),thrown:thrown,error:thrown?String(error):undefined,authoritativeForActiveLayer:info.before.page.activeLayer==='JavaScript'&&Nav.activeLayer==='JavaScript'}));});
    traceCall(Focus,'setFocus','Focus.setFocus');
    // This dispatch method obtains the CURRENT strategy from its owner for each
    // real input, including containers created after our initial tree snapshot.
    hook(Focus,'getNextFocusedItemWithPathAndInputEvent',function(args){
      if(!tracing()||treeBusy)return null;
      return {callId:++callId,mechanism:'focus_manager_strategy_dispatch',owner:node(args[0]),
        inputId:inputStack.length?inputStack[inputStack.length-1]:null,event:bounded(args[1]),
        marker:lastMarker,before:state()};
    },function(args,result,error,thrown,info){if(info)emit('navigation_strategy_result',Object.assign(info,{
      result:node(result),returnedNull:result===null,returnedUndefined:result===undefined,thrown:thrown,
      error:thrown?String(error):undefined,coverage:'Actual strategy dispatch; final settled focus is recorded separately.'}));});
    var Input=g.__pvModules.exports(moduleIds.input),NativeInput=g.__pvModules.exports(moduleIds.native_input),Platform=g.__pvModules.exports(moduleIds.platform),RPC=g.__pvModules.exports(moduleIds.rpc);
    if(Input){
      [['mi','pre_button_down',true],['QQ','post_button_down',false],['xn','post_button_up',false],['pR','press_gesture',false],['uO','long_press_gesture',false],['Rc','long_press_finished',false]].forEach(function(x){tapEmitter(Input[x[0]],x[1],x[2]);});
    }
    if(NativeInput&&NativeInput.IgniteInputEmitter)hook(NativeInput.IgniteInputEmitter,'emit',function(args){if(tracing()&&args[0]==='onNativeInput')emit('native_input_event',{event:bounded(args[1]),state:state()});});
    if(!options.lightweight&&RPC&&RPC.Z&&RPC.Z.instance)hook(RPC.Z.instance,'makeCall',function(args){
      var c=args[0],name=c&&c.wasmFuncName||'';
      if(/auth|synchronized_state_store_update|acm_store_update/i.test(name))return null;
      var id=++callId;emit('control_call',{callId:id,target:'RPCManager.makeCall',phase:'enter',state:state(),arguments:bounded(Array.prototype.slice.call(args))});return id;
    },function(args,result,error,thrown,id){if(id!==null&&id!==undefined)emit('control_call',{callId:id,target:'RPCManager.makeCall',phase:'return',state:state(),thrown:thrown,error:thrown?String(error):undefined,result:result&&typeof result.then==='function'?{kind:'promise_unobserved'}:bounded(result)});});
    Manager=(g.__pvModules.exports(moduleIds.manager)||{}).WasmCommunicationManager||Manager;
    var manager=Manager&&Manager.instance;
    if(!options.lightweight&&manager)hook(manager,'handleResponseEvent',function(args){var c=args[0];if(c&&c.jsFuncName)emit('rpc_incoming',{name:c.jsFuncName,callId:c.callId,argsJson:bounded(c.argsJson),state:state()});});
    var moduleCoverage=optionalIds.map(function(id){return {id:id,loaded:!!g.__pvModules.exports(id)};});
    var coverage={modules:moduleCoverage,hooks:hooks.length,activeLayer:Nav.activeLayer,dispatcher:dispatchHook?'event_queue_wait_boundary':'unavailable'};var wire=JSON.stringify(coverage);
    if(wire!==lastCoverage){lastCoverage=wire;emit('control_coverage',coverage);}
    if(Input&&!install.mapsReported){install.mapsReported=true;emit('input_mapping',{applicationKeys:bounded(Input.UL),applicationEventTypes:bounded(Input.GM),processingStates:bounded(Input.qO),nativeAvpkKeys:bounded(Platform&&Platform.AVPK&&Platform.AVPK.KeyCode),note:'Android key codes, AVPK codes and application codes are separate namespaces.'});}
  }
  function executeCommand(c){
    checkOwner(c);
    if(g.__pvCompat)g.__pvCompat.assertCommand(c);
    if(stopped)throw new Error('control stopped');
    if(!c||typeof c!=='object'||typeof c.id!=='string')throw new Error('invalid control command');
    var before=state(),result;
    if(c.op!=='snapshot'&&(before.page.pageType==='UNLOADED'||!before.page.pageType||JSON.stringify(before.page)!==lastPageKey||Date.now()-pageSince<3000))throw new Error('page is initializing or changed within 3 seconds; wait for control_state ready');
    emit('control_action_started',{id:c.id,command:bounded(c),before:before});
    if(c.op==='mark'){epoch++;lastMarker={id:c.id,key:c.key,inputEpoch:epoch,timeMs:Date.now()};result={marked:true,inputEpoch:epoch};}
    else if(c.op==='snapshot'){richTree();emit('focus_checkpoint',{state:state(),requested:true});result={snapshotRequested:true};}
    else if(c.op==='trace-js'){
      if(c.seconds!==0&&(!Number.isInteger(c.seconds)||c.seconds<5||c.seconds>120))throw new Error('trace seconds must be 0 or 5..120');
      if(c.seconds&&Nav.activeLayer!=='JavaScript')throw new Error('JavaScript is not the active input owner');
      traceUntil=c.seconds?Date.now()+c.seconds*1000:0;if(c.seconds)richTree();
      result={tracing:!!c.seconds,untilMs:traceUntil};emit('javascript_trace_window',result);
    }
    else if(c.op==='broadcasts-data'){result=broadcasts.command(c);}
    else if(c.op==='search-data'){result=catalog.command(c);}
    else if(c.op==='search'||c.op==='watch-search'){
      result=search.begin(c.query,c.id);
      if(c.op==='search'){
        var en=g.__pvModules.exports(moduleIds.page_types);if(!en||!en.Z||en.Z.FIND_PAGE!=='FIND_PAGE')throw new Error('pinned FIND_PAGE enum unavailable');
        Nav.switchPage(en.Z.FIND_PAGE,{initialQuery:c.query,isAlexaQuery:true,voiceQueryId:Date.now().toString()});
      }
    }
    else if(c.op==='search-results'){result=search.snapshot(true);}
    else if(c.op==='inspect-native'||c.op==='inspect-selection'||c.op==='inspect-player'){if(!api.requestNative)throw new Error('native inspection unavailable');result=api.requestNative(c.op==='inspect-player'?'player':c.op==='inspect-selection',c.id);}
    else if(c.op==='watch-page'){
      if(typeof c.pageId!=='string'||api.page().pageId!==c.pageId)throw new Error('page watch destination mismatch');
      pageWatch={id:c.id,pageId:c.pageId,started:Date.now(),until:Date.now()+120000,count:0};
      result={browseId:c.id,pageId:c.pageId,startedTimeMs:pageWatch.started};
    }
    else if(c.op==='playback-status'){result={page:api.page(),epoch:playbackEpoch,event:lastPlaybackEvent,recentEvents:recentPlaybackEvents.slice(),timeMs:Date.now()};}
    else if(c.op==='inspect-state'){if(!capabilities)throw new Error('capability probe unavailable');capabilities.state();result={requested:true};}
    else if(c.op==='resolve-live'||c.op==='play-live'||c.op==='resolve-content'||c.op==='play-content'){
      if(!capabilities)throw new Error('capability probe is not enabled');
      return {deferredResolution:capabilities.begin(c)};
    }
    else if(c.op==='navigate'){
      var enums=g.__pvModules.exports(moduleIds.page_types),valid=enums&&enums.Z&&Object.keys(enums.Z).some(function(k){return enums.Z[k]===c.destination;});
      if(!valid)throw new Error('destination not in loaded PageType enum');
      if(!c.params||typeof c.params!=='object'||Array.isArray(c.params))throw new Error('params must be an object');
      pageWatch=null;
      result=Nav.switchPage(c.destination,c.params);
    }else if(c.op==='stop-playback'){
      var p=api.page();
      if(!lastPlaybackAttempt||lastPlaybackAttempt!==c.attemptId||p.pageType!=='RUST_PLAYBACK'||p.pageId!==c.expectedTitleId||playbackEpoch!==c.expectedPlaybackEpoch)throw new Error('ownership:playback no longer belongs to this attempt');
      result=Nav.switchToPreviousPage();
    }else if(c.op==='back'){result=Nav.switchToPreviousPage();}
    else if(c.op==='focus'){
      if(Nav.activeLayer!=='JavaScript')throw new Error('Focus.setFocus is only enabled for the active JavaScript focus domain');
      if(typeof c.path!=='string'||!call(Focus,'getFocusedFocusable'))throw new Error('JavaScript focus is not ready');
      var n=Focus.getByFocusPath(c.path);if(!n)throw new Error('exact focus path not present');
      result=Focus.setFocus(c.path);
    }else throw new Error('unsupported control operation');
    var out={id:c.id,op:c.op,invoked:true,before:before,after:state(),result:c.op==='search-results'?{value:result,lossless:true,losses:[]}:bounded(result),note:'Invocation result; destination rendering/playback must be verified separately.'};emit('control_action_finished',out);return c.op==='playback-status'||c.op==='watch-page'?result:out;
  }
  function command(c){
    checkOwner(c);
    if(g.__pvCompat)c.compatibilityGeneration=g.__pvCompat.assertCommand(c);
    if(stopped)throw new Error('control stopped');
    if(!c||typeof c.id!=='string')throw new Error('invalid command');
    if(Object.keys(scheduled).length||inflight)throw new Error('one command already scheduled or resolving');
    if(!dispatchHook||dispatchQueue.waitWithTimeout!==dispatchHook||!boundaryCount||Date.now()-lastBoundary>5000)throw new Error('application event-loop boundary unavailable or stale');
    scheduled[c.id]={command:c,queuedAt:Date.now(),deadline:Date.now()+10000};
    return {id:c.id,scheduled:true,mechanism:'application_event_queue_wait_boundary',deadlineMs:scheduled[c.id].deadline};
  }
  function complete(p,success,data,launchDisposition){
    var out={id:p.command.id,success:success,queuedAtMs:p.queuedAt,completedAtMs:Date.now(),queueDelayMs:(p.started||Date.now())-p.queuedAt,executionDurationMs:p.started?Date.now()-p.started:null,mechanism:'application_event_queue_wait_boundary'};
    if(success)out.result=data;else out.error=String(data);
    if(!success&&launchDisposition==='not_invoked')out.launchDisposition=launchDisposition;
    completions.push(out);emit('control_completion',out);
  }
  function finishResolution(allowNavigation,reason){
    if(!inflight)return;
    var entry=inflight,out;
    if(reason){capabilities.cancel(entry.operation,reason);out={done:true,success:false,error:reason};}
    else try{out=capabilities.poll(entry.operation,allowNavigation);}catch(error){capabilities.cancel(entry.operation,'poll failed');out={done:true,success:false,error:String(error)};}
    if(out.done){if(out.success&&out.result&&out.result.launchInvoked)lastPlaybackAttempt=entry.pending.command.attemptId||null;inflight=null;complete(entry.pending,out.success,out.success?out.result:out.error,out.launchDisposition);}
  }
  function expireCommands(reason){Object.keys(scheduled).forEach(function(id){var p=scheduled[id];if(reason||Date.now()>p.deadline){delete scheduled[id];complete(p,false,reason||'command expired before event-loop boundary; not invoked');}});finishResolution(false,reason);}
  function dispatchAtBoundary(){
    if(g.__pvCompat&&!g.__pvCompat.allows('core')){expireCommands('compatibility:core_unavailable');return;}
    if(stopped||dispatchBusy)return;boundaryCount++;lastBoundary=Date.now();
    expireCommands();
    if(inflight){dispatchBusy=true;try{finishResolution(true);}finally{dispatchBusy=false;}return;}
    var ids=Object.keys(scheduled);if(!ids.length)return;
    var p=scheduled[ids[0]];delete scheduled[ids[0]];p.started=Date.now();dispatchBusy=true;
    try{var result=executeCommand(p.command);if(result&&result.deferredResolution)inflight={pending:p,operation:result.deferredResolution};else complete(p,true,result);}catch(e){complete(p,false,e);}finally{dispatchBusy=false;}
  }
  function stop(){stopped=true;catalog.stop();broadcasts.stop();expireCommands('control stopped before invocation');if(capabilities)capabilities.stop();hooks.slice().reverse().forEach(function(h){if(h.obj[h.key]===h.wrapped){if(h.own)Object.defineProperty(h.obj,h.key,h.own);else delete h.obj[h.key];}});hooks=[];return {stopped:true};}
  function tick(){if(stopped)return;catalog.tick();broadcasts.tick();if(g.__pvCompat&&!g.__pvCompat.allows('core')){expireCommands('compatibility:core_unavailable');return;}probeBusy=true;try{if(!options.lightweight||Date.now()-lastInstall>=3000){lastInstall=Date.now();install();}expireCommands();if(capabilities)capabilities.tick();var p=api.page(),key=JSON.stringify(p);if(key!==lastPageKey){lastPageKey=key;pageSince=Date.now();}var recent=!!dispatchHook&&dispatchQueue.waitWithTimeout===dispatchHook&&boundaryCount>0&&Date.now()-lastBoundary<=5000;var cs={page:p,ready:!!p.pageType&&p.pageType!=='UNLOADED'&&Date.now()-pageSince>=3000&&recent&&!inflight,dispatcherReady:recent,dispatcher:'event_queue_wait_boundary',resolving:!!inflight};lastControl=cs;var cw=JSON.stringify(cs);if(cw!==lastControlState){lastControlState=cw;emit('control_state',cs);}if(!options.lightweight&&Date.now()-lastTree>=5000){lastTree=Date.now();richTree();}if(options.lightweight&&tracing()&&Date.now()-lastCheckpoint>=500){lastCheckpoint=Date.now();emit('focus_checkpoint',{state:state(),requested:false});}}finally{probeBusy=false;}}
  if(options.capabilities)capabilities=(__PV_CAPABILITY_FACTORY__)(g,options,{hook:hook,bounded:bounded,page:api.page,emit:emit,Manager:Manager,playbackSnapshot:playbackSnapshot,preparePlayback:preparePlayback});
  install();emit('control_ready',{runtimeVersion:options.profile.runtimeVersion,commands:['navigate','back','focus','snapshot','mark'],coverage:'JavaScript and native routing traced; native-only internal handlers may not cross these boundaries.'});
  return {fence:fence,tick:tick,stop:stop,command:command,health:function(){return {dispatcherReady:!!dispatchHook&&dispatchQueue.waitWithTimeout===dispatchHook&&boundaryCount>0&&Date.now()-lastBoundary<=5000,lastBoundaryMs:lastBoundary,boundaryCount:boundaryCount,tracingUntilMs:traceUntil,state:lastControl};},searchSnapshot:function(){return search.snapshot(false);},impression:pageImpression,epoch:function(){return epoch;},takeCompletions:function(){var out=completions;completions=[];return out;},event:function(e){if(e&&e.type==='AppEvent'&&e.name==='NAVIGATE_TO_PAGE')pageWatch=null;playbackEvent(e);search.event(e);if(e&&e.type==='AppEvent'&&/nav|play|focus|input|key|page|action|launch|select|find|search/i.test(e.name||''))emit('control_app_event',{name:e.name,params:bounded(e.params),state:state(),playbackEpoch:playbackEpoch});}};
})

