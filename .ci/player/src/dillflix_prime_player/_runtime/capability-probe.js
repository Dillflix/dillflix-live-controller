/* Pinned runtime interfaces: passive coverage and fresh live or explicitly enabled on-demand resolution. */
(function(g,options,api){
  'use strict';
  var moduleIds=options.profile.adapter.modules;
  var stopped=false,lastState=0,lastCoverage='',lastCatalog=-1,callId=0,liveOp=null,hookStatus={};
  var renderer=(__PV_RENDERER_FACTORY__)(api.bounded),lastRendererTime=0,lastRendererRevision=-1,aggregated={};
  var interfaceIds=[moduleIds.store,moduleIds.metadata,moduleIds.history,moduleIds.router,moduleIds.content,moduleIds.resolver,moduleIds.transform,moduleIds.deeplink,moduleIds.playback_result,moduleIds.playback_metadata,moduleIds.metadata_client,moduleIds.metadata_model,moduleIds.remote_client,moduleIds.availability,moduleIds.identity,moduleIds.playback_policy,moduleIds.search_model,moduleIds.auth];
  function mod(id){return g.__pvModules.exports(id);}
  function emit(k,d){if(!stopped)api.emit(k,d);}
  function attempt(fn,fallback){try{return fn();}catch(_){return fallback;}}
  function primitive(x){return x===null||typeof x==='boolean'||typeof x==='number'||typeof x==='string';}
  function semantic(root){
    var out=[],seen=[],budget=2500,hit=/^(?:id|gti|titleId|contentId|title|titleText|text|label|type|target|pageType|pageId|actionType|metadataActionType|position|videoMaterialType|playbackTitle|buttonType|entitlementType|expiryTime|name)$/;
    function visit(x,path,depth){
      if(!x||typeof x!=='object'||depth<0||budget--<=0||out.length>=256||seen.indexOf(x)>=0)return;
      seen.push(x);var keys=Object.keys(x),summary={},children=[];
      keys.slice(0,128).forEach(function(k){
        if(/auth|cookie|token|password|customer|deviceId|profile|advertising|playbackEnvelope|correlationId/i.test(k))return;
        var d=Object.getOwnPropertyDescriptor(x,k);if(!d||!Object.prototype.hasOwnProperty.call(d,'value'))return;
        var v=d.value;if(hit.test(k)&&primitive(v))summary[k]=typeof v==='string'?v.slice(0,500):v;
        if(v&&typeof v==='object')children.push([v,path+'.'+k]);
      });
      if(Object.keys(summary).length)out.push({path:path,fields:summary});
      children.forEach(function(c){visit(c[0],c[1],depth-1);});seen.pop();
    }
    visit(root,'$',6);return {entries:out,truncated:budget<=0||out.length>=256,coverage:'Selected own data fields; accessors and credentials excluded. Loaded state is not proof of visibility.'};
  }
  function trace(obj,key,label,accept,tap){
    if(!hookStatus[label])hookStatus[label]={installed:false,observedCalls:0};
    var installed=api.hook(obj,key,function(args,receiver){
      hookStatus[label].observedCalls++;
      if(accept&&!accept(args,receiver)){aggregated[label]=(aggregated[label]||0)+1;return null;}
      var id=++callId;emit('capability_call',{callId:id,target:label,phase:'enter',page:api.page(),transformName:receiver&&typeof receiver.transformName==='string'?receiver.transformName.slice(0,200):undefined,arguments:api.bounded(Array.prototype.slice.call(args))});return id;
    },function(args,result,error,thrown,id){
      if(!thrown&&tap)attempt(function(){tap(args,result);});
      if(id===null||id===undefined)return;
      emit('capability_call',{callId:id,target:label,phase:'return',thrown:thrown,error:thrown?String(error).slice(0,300):undefined,
        result:result&&typeof result.then==='function'?{kind:'promise'}:api.bounded(result)});
      if(!thrown&&result&&typeof result.then==='function')attempt(function(){result.then(function(value){
        attempt(function(){emit('capability_call',{callId:id,target:label,phase:'resolved',value:api.bounded(value),semantic:semantic(value)});});
      },function(reason){attempt(function(){emit('capability_call',{callId:id,target:label,phase:'rejected',error:String(reason).slice(0,300)});});});});
    });
    if(installed)hookStatus[label].installed=true;
  }
  function install(){
    if(options.lightweight)return;
    var router=mod(moduleIds.router),client=mod(moduleIds.transform),result=mod(moduleIds.playback_result),base=mod(moduleIds.remote_client),input=mod(moduleIds.native_input);
    if(router&&router.commandRouter){
      trace(router.commandRouter,'invokeCommand','CommandRouter.invokeCommand');
      trace(router.commandRouter,'routeCommand','CommandRouter.routeCommand');
    }
    if(client&&client.YY)trace(client.YY.prototype,'call','DeepLinkTitleTransform.call');
    if(result&&result.G)trace(result.G.prototype,'navigate','PlaybackNavigationResult.navigate');
    if(base&&base.t)trace(base.t.prototype,'call','RemoteAPIClient.call');
    if(input&&input.WasmInputHandler)trace(input.WasmInputHandler.prototype,'handleEventCommand','WasmInputHandler.handleEventCommand',function(args){
      var value=args[0]||{};
      // Metrics and memory labels are high-volume telemetry, not input gestures.
      return !(value.command||value.source&&value.labels);
    });
    var manager=api.Manager&&api.Manager.instance;
    if(manager)trace(manager,'sendCommand','WasmCommunicationManager.sendCommand',function(args){
      var command=args[0],plugin=command&&command.plugin;
      if(command&&/renderable|sceneCommandFlush|Animation/i.test(command.commandType||''))return false;
      return !(plugin&&/^app-(focus|view-inspection)-plugin$/.test(plugin.pluginName));
    },function(args){renderer.command(args[0]);});
    var coverage={interfaces:interfaceIds.map(function(id){var m=mod(id);return {moduleId:id,loaded:!!m,exports:m?Object.keys(m).slice(0,80):[]};}),
      resolverAvailable:!!(mod(moduleIds.resolver)&&typeof mod(moduleIds.resolver).ZW==='function'&&result&&typeof result.G==='function'),
      signInCheckAvailable:!!(mod(moduleIds.auth)&&mod(moduleIds.auth).ZP&&typeof mod(moduleIds.auth).ZP.isUserSignedIn==='function'),
      activeLayer:api.page().activeLayer,hooks:hookStatus,aggregatedCalls:aggregated,inspectionScopes:['javascript_modules','javascript_state_store','javascript_renderer_graph','cross_language_commands','native_test_queries','android_artifacts'],
      limits:['JavaScript presence does not imply ownership of visible tiles.','Native internal Rust handlers are not intercepted by JavaScript wrappers.','Module absence is reported, never repaired by forcing initialization.']};
    var wire=JSON.stringify(coverage);if(wire!==lastCoverage){lastCoverage=wire;emit('capability_coverage',coverage);}
  }
  function state(){
    var m=mod(moduleIds.store),store=m&&m.default,model=null;
    if(store&&typeof store.getState==='function'){
      var raw=attempt(function(){return store.getState();},null),selected={};
      if(raw&&typeof raw==='object')Object.keys(raw).forEach(function(k){if(/browse|details|find|search|playback|title|widget|collection|page|modal|navigation|settings|category|management|accessibility/i.test(k)&&!/profile|auth/i.test(k)){
        var d=Object.getOwnPropertyDescriptor(raw,k);if(d&&Object.prototype.hasOwnProperty.call(d,'value'))selected[k]=d.value;
      }});
      if(options.metadataDiscovery)Object.keys(selected).forEach(function(k){emit('javascript_store_section',{page:api.page(),root:k,metadata:api.bounded(selected[k])});});
      model={rootKeys:raw?Object.keys(raw).slice(0,256):[],selectedRoots:Object.keys(selected),content:semantic(selected)};
    }
    var history=mod(moduleIds.history),router=mod(moduleIds.router),registry=router&&router.commandRouter&&router.commandRouter.registeredCommandHandlers;
    emit('javascript_state_inventory',{page:api.page(),store:model,
      history:history&&history.ZP&&typeof history.ZP.getNavStack==='function'?api.bounded(attempt(function(){return history.ZP.getNavStack();},null)):null,
      registeredCommands:registry&&typeof registry.keys==='function'?attempt(function(){return Array.from(registry.keys()).slice(0,128);},[]):[],
      authority:api.page().activeLayer==='JavaScript'?'active_javascript_ui':'host_services_while_native_ui_active'});
  }
  function tick(){
    if(stopped||options.lightweight)return;
    try{install();}catch(error){emit('capability_error',{phase:'install',message:String(error).slice(0,300)});}
    var c=g.__pvModules.catalog&&g.__pvModules.catalog();
    if(c&&c.revision!==lastCatalog){lastCatalog=c.revision;emit('javascript_module_catalog',c);}
    if(Date.now()-lastRendererTime>=5000&&renderer.revision()!==lastRendererRevision){lastRendererTime=Date.now();lastRendererRevision=renderer.revision();emit('javascript_renderer_snapshot',{page:api.page(),graph:renderer.snapshot()});}
    if(Date.now()-lastState>=10000){lastState=Date.now();try{state();}catch(error){emit('capability_error',{phase:'state',message:String(error).slice(0,300)});}}
  }
  function identityFields(root){
    var fields=[],seen=[],budget=2000,truncated=false;
    var secret=/token|cookie|credential|authorization|envelope|url|uri|license|drm|customer|profile|session|password|signature/i;
    var wanted=/^(?:title|titleName|displayTitle|displayName|name|label|eventName|subtitle|subTitle|gti|eventGti|titleId|titleIdentifier|contentId|itemContentId|titleType|videoMaterialType|sport|league|competition|homeTeam|awayTeam|teamName|startTime|endTime|startDate|eventDate|date|season|episodeNumber|duration|durationSeconds|isLive|isReplay)$/i;
    function walk(v,path,depth){
      if(!v||typeof v!=='object'||seen.indexOf(v)>=0)return;
      if(depth>8||budget<=0||fields.length>=96){truncated=true;return;}
      seen.push(v);
      var keys=Object.getOwnPropertyNames(v);
      for(var k=0;k<keys.length;k++){
        if(--budget<0){truncated=true;break;}
        var key=keys[k];if(secret.test(key))continue;
        var d=Object.getOwnPropertyDescriptor(v,key);if(!d||!Object.prototype.hasOwnProperty.call(d,'value'))continue;
        var x=d.value,at=path+'.'+key;
        if(wanted.test(key)&&(typeof x==='string'||typeof x==='number'||typeof x==='boolean')){
          if(typeof x!=='string'||(x.length<=512&&!/^https?:/i.test(x)))fields.push({path:at,key:key,value:x});
        }else if(x&&typeof x==='object')walk(x,at,depth+1);
        if(fields.length>=96){truncated=true;break;}
      }
    }
    walk(root,'$',0);
    return {fields:fields,truncated:truncated,method:'bounded_own_data_properties_no_getters',replayIdentityVerified:false};
  }
  function restoreIdentity(op){
    if(op.identityHook&&op.identityHook.target.call===op.identityHook.wrapper)op.identityHook.target.call=op.identityHook.original;
    op.identityHook=null;
  }
  function observeIdentity(op){
    var m=mod(moduleIds.transform),target=m&&m.YY&&m.YY.prototype;
    if(!target||typeof target.call!=='function'){op.identityCoverage='raw_resolver_unavailable';return;}
    var original=target.call;
    function wrapped(request){
      var result=original.apply(this,arguments);
      if(!op.cancelled&&request&&request.titleIdentifier===op.command.titleId&&result&&typeof result.then==='function'){
        result.then(function(value){
          if(op.cancelled)return;
          try{op.rawIdentity=identityFields(value);op.identityCoverage='matching_raw_resolver_response';
            if(options.metadataDiscovery)emit('content_metadata',{id:op.command.id,requestedTitleId:op.command.titleId,source:'matching_raw_resolver_response',metadata:api.bounded(value)});
            emit('content_identity',{id:op.command.id,requestedTitleId:op.command.titleId,source:'71336.YY.call matching titleIdentifier',identity:op.rawIdentity});
          }catch(e){op.identityCoverage='identity_extraction_failed';}
        },function(){});
      }
      return result;
    }
    target.call=wrapped;op.identityHook={target:target,original:original,wrapper:wrapped};op.identityCoverage='raw_resolver_observer_installed';
  }
  function summary(op,result){
    var meta=result&&result.playbackMetadata,experience=meta&&meta.playbackExperienceMetadata;
    var playbackClass=meta&&meta.videoMaterialType==='Feature'?'on_demand':meta&&meta.videoMaterialType==='LiveStreaming'&&meta.position==='LIVE_STREAM_WATCH_NOW'?'live_watch_now':'unsupported';
    return {identity:{resolverResponse:op.rawIdentity||null,navigationResult:identityFields(result),coverage:op.identityCoverage||'unavailable'},contentPolicy:/(?:-content)$/.test(op.command.op)?'live_or_on_demand':'live_only',playbackClass:playbackClass,replayIdentityVerified:false,requestedTitleId:op.command.titleId,resolvedTitleId:result&&result.gti,titleType:result&&result.titleType,playbackTitleIdentifier:meta&&meta.playbackTitle,
      videoMaterialType:meta&&meta.videoMaterialType,position:meta&&meta.position,
      entitlementType:meta&&meta.userEntitlementMetadata&&meta.userEntitlementMetadata.entitlementType,
      metadataExpiresAtMs:experience&&experience.expiryTime,hasPlaybackEnvelope:!!(experience&&experience.playbackEnvelope),
      resolver:'adapter.resolver -> adapter.transform -> deeplink/deepLinkTitleTransform',signedInConfirmed:!!op.signedIn,freshRequestStartedMs:op.started};
  }
  function begin(c){
    if(g.__pvOwnershipCheck)g.__pvOwnershipCheck(c);
    if(g.__pvCompat)c.compatibilityGeneration=g.__pvCompat.assertCommand(c);
    if(liveOp)throw new Error('resolver already active');
    if(!/^(?:resolve-live|play-live|resolve-content|play-content)$/.test(c.op)||typeof c.titleId!=='string'||!/^[A-Za-z0-9._:-]{1,256}$/.test(c.titleId))throw new Error('invalid content-resolution command');
    var handler=mod(moduleIds.resolver),Result=mod(moduleIds.playback_result),auth=mod(moduleIds.auth);
    if(!handler||typeof handler.ZW!=='function'||!Result||typeof Result.G!=='function')throw new Error('normal title resolver is not initialized; no cold require performed');
    if(!auth||!auth.ZP||typeof auth.ZP.isUserSignedIn!=='function')throw new Error('application sign-in check is unavailable; resolver not called');
    var op={command:c,started:Date.now(),deadline:Date.now()+20000,page:JSON.stringify(api.page()),initialPlayback:api.playbackSnapshot(),ready:false,cancelled:false,Result:Result.G};liveOp=op;
    emit(/-content$/.test(op.command.op)?'content_resolution':'live_resolution',{id:c.id,phase:'requested',operation:c.op,titleId:c.titleId,source:'normal_application_resolver'});
    function failed(error){if(!op.cancelled){op.error=String(error);op.ready=true;}}
    try{
      // This normal voice path skips its own sign-in check. Preserve that check explicitly.
      var signedIn=auth.ZP.isUserSignedIn();
      if(!signedIn||typeof signedIn.then!=='function')throw new Error('sign-in check did not return a promise');
      signedIn.then(function(value){
        if(op.cancelled||Date.now()>op.deadline)return;
        try{if(g.__pvOwnershipCheck)g.__pvOwnershipCheck(c);if(g.__pvCompat)g.__pvCompat.assertCommand(c);}catch(e){failed(e);return;}
        if(value!==true){failed('application reports user is not signed in; resolver not called');return;}
        op.signedIn=true;
        try{
          observeIdentity(op);
          var promise=handler.ZW({isVoiceControlled:true,gti:c.titleId});
          if(!promise||typeof promise.then!=='function')throw new Error('resolver did not return a promise');
          promise.then(function(value){if(!op.cancelled){op.result=value;op.ready=true;}},failed);
        }catch(error){failed(error);}
      },failed);
    }catch(error){failed(error);}
    return op;
  }
  function cancel(op,reason){if(op){restoreIdentity(op);op.cancelled=true;op.result=null;if(liveOp===op)liveOp=null;emit(/-content$/.test(op.command.op)?'content_resolution':'live_resolution',{id:op.command.id,phase:'cancelled',reason:reason});}}
  function poll(op,allowNavigation){
    try{if(g.__pvOwnershipCheck)g.__pvOwnershipCheck(op.command);if(g.__pvCompat)g.__pvCompat.assertCommand(op.command);}catch(e){cancel(op,String(e));return {done:true,success:false,error:String(e)};}
    if(op.cancelled)return {done:true,success:false,error:'resolution cancelled'};
    if(Date.now()>op.deadline){cancel(op,'deadline');return {done:true,success:false,error:'fresh title resolution exceeded 20 seconds; late result cannot launch'};}
    if(!op.ready)return {done:false};
    if((op.command.op==='play-live'||op.command.op==='play-content')&&!allowNavigation)return {done:false};
    var r=op.result,s=summary(op,r),error=op.error;
    if(options.metadataDiscovery&&!op.metadataEmitted){emit('content_metadata',{id:op.command.id,requestedTitleId:op.command.titleId,source:'navigation_result_before_policy_check',metadata:api.bounded(r)});op.metadataEmitted=true;}
    if(!error&&!(r instanceof op.Result))error='resolver returned a non-playback result (for example details, entitlement or error page)';
    var allowOnDemand=/(?:-content)$/.test(op.command.op);
    var live=s.playbackClass==='live_watch_now';
    var onDemand=s.playbackClass==='on_demand' && !(typeof s.position==='string' && s.position.indexOf('LIVE_STREAM_')===0);
    if(!error&&(!s.resolvedTitleId||(!live&&!(allowOnDemand&&onDemand))))error=allowOnDemand?
      'resolver did not return supported live Watch Now or on-demand Feature metadata; no mode was substituted':
      'resolver did not explicitly return live Watch Now; no mode was substituted';
    var meta=r&&r.playbackMetadata,experience=meta&&meta.playbackExperienceMetadata;
    if(!error&&s.hasPlaybackEnvelope&&(typeof s.metadataExpiresAtMs!=='number'||s.metadataExpiresAtMs<Date.now()+5000))error='playback metadata expiry missing, expired or too close';
    if(!error&&(op.command.op==='play-live'||op.command.op==='play-content')&&!op.playbackTransition&&JSON.stringify(api.page())!==op.page)error='page changed while resolving; launch cancelled';
    // Retain the actual rejected navigation result without invoking accessors or
    // relaxing the live-only policy. Diagnostic failure cannot change control.
    if(error){try{emit('resolver_diagnostic',{id:op.command.id,requestedTitleId:op.command.titleId,error:error,isPlaybackResult:r instanceof op.Result,result:api.bounded(r)});}catch(diagnosticError){try{emit('resolver_diagnostic',{id:op.command.id,captureError:String(diagnosticError)});}catch(_){}}}
    if(error){emit(/-content$/.test(op.command.op)?'content_resolution':'live_resolution',{id:op.command.id,phase:'refused',error:error,summary:s});cancel(op,'refused');return {done:true,success:false,error:error,evidence:s,launchDisposition:op.playbackTransition?undefined:'not_invoked'};}
    if(op.command.expectedPlaybackClass&&s.playbackClass!==op.command.expectedPlaybackClass){cancel(op,'requested playback class differs');return {done:true,success:false,error:'requested playback class differs; no navigation invoked',launchDisposition:op.playbackTransition?undefined:'not_invoked'};}
    var launched=false;
    if((op.command.op==='play-live'||op.command.op==='play-content')){
      // Resolve and validate first, then stop any existing stream. Page exit
      // alone is insufficient: wait for a fresh unload/stop from its epoch.
      var prepared=api.preparePlayback(op);
      if(!prepared.ready)return {done:false};
      s.playbackTransition=prepared.evidence;
      // Navigation can emit arrival synchronously. Establish the attempt's
      // evidence boundary before invoking it, while still requiring success.
      emit(/-content$/.test(op.command.op)?'content_resolution':'live_resolution',{id:op.command.id,phase:'launch_invoking'});
      try{r.navigate();launched=true;}catch(e){error=String(e);}
    }
    emit(/-content$/.test(op.command.op)?'content_resolution':'live_resolution',{id:op.command.id,phase:launched?'launch_invoked':'resolved',summary:s,error:error,
      note:'Fresh resolution and normal navigation invocation; confirm page and Playing separately.'});
    restoreIdentity(op);op.cancelled=true;op.result=null;liveOp=null;
    return {done:true,success:!error,error:error,result:{resolved:true,launchInvoked:launched,summary:s}};
  }
  return {tick:tick,state:state,begin:begin,poll:poll,cancel:cancel,stop:function(){cancel(liveOp,'probe stopped');stopped=true;}};
})
