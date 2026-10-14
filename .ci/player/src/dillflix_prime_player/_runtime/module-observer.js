/* Observe the pinned Photon bundle's normal module initialization. */
(function (g, options) {
  'use strict';
  var moduleIds=options.profile.adapter.modules;
  if (g.__pvModules) return g.__pvModules.status();
  var ids=[moduleIds.focus,moduleIds.page,moduleIds.navigation,moduleIds.bridge,moduleIds.context,moduleIds.manager,moduleIds.plugin_command,moduleIds.rust_playback,moduleIds.input,moduleIds.native_input,moduleIds.platform,moduleIds.rpc,moduleIds.page_types,moduleIds.event_loop,moduleIds.store,moduleIds.metadata,moduleIds.history,moduleIds.router,moduleIds.content,moduleIds.resolver,moduleIds.transform,moduleIds.deeplink,moduleIds.playback_result,moduleIds.playback_metadata,moduleIds.metadata_client,moduleIds.metadata_model,moduleIds.remote_client,moduleIds.availability,moduleIds.identity,moduleIds.playback_policy,moduleIds.search_model,moduleIds.auth];
  ids=Array.from(new Set(ids.concat(Object.keys(moduleIds).filter(function(k){return k.indexOf('find_')===0;}).map(function(k){return moduleIds[k];}))));
  var captured={}, originals={}, wrappers={}, req=null, queued=false,initialized={},available={},revision=0,chunks=[],replacements=[],factoryRefs={},candidates={},availableCount=0,sourceBudget=1048576;
  var chunkArray=null,pushGet=null,pushSet=null,pushValue=null,pushAssigned=false,pushPrior=null,maps=[];
  var state={installed:true,webpackCaptured:false,failed:{},reason:null};
  var key='webpackChunkPhoton', prior=Object.getOwnPropertyDescriptor(g,key);
  var getter=null, setter=null;
  var factoryStack=[],defineProperty=Object.defineProperty,exportDefinitions=0,defineExports=null,defineExportsDescriptor=null,wrappedDefineExports=null;
  // Observe the value only when Prime invokes its own export getter. This also
  // handles re-export getters that are installed without Webpack's require.d.
  function observedDefine(object,key,descriptor){
    var frame=factoryStack[factoryStack.length-1],next=descriptor;
    if(frame&&object===frame.module.exports&&descriptor&&typeof descriptor.get==='function'&&g.__pvRoles){
      var originalGet=descriptor.get,lastValue,lastLoaded,observed=false;next={};Object.keys(descriptor).forEach(function(k){next[k]=descriptor[k];});
      next.get=function(){var value=originalGet.apply(this,arguments),loaded=frame.module.loaded===true;if(state.installed&&(!observed||lastValue!==value||lastLoaded!==loaded)){observed=true;lastValue=value;lastLoaded=loaded;try{g.__pvRoles.observe(frame.id,frame.module,String(key),value);}catch(_){}}return value;};
      try{g.__pvRoles.accessor(frame.id,frame.module,String(key),next.get,exportDefinitions?'webpack_export':'custom_accessor');}catch(_){}
    }
    return defineProperty(object,key,next);
  }
  if(g.__pvRoles)Object.defineProperty=observedDefine;
  function status(){
    var loaded=ids.filter(function(id){return captured[id]&&captured[id].loaded===true;});
    return {installed:state.installed,webpackCaptured:state.webpackCaptured,
      wrapped:ids.filter(function(id){return !!wrappers[id];}),loaded:loaded,
      pending:ids.filter(function(id){return loaded.indexOf(id)<0;}),
      failed:Object.assign({},state.failed),reason:state.reason,catalogRevision:revision,
      availableFactoryCount:Object.keys(available).length,initializedFactoryCount:Object.keys(initialized).length};
  }
  function catalog(){return {scope:key,revision:revision,available:Object.keys(available),initialized:Object.keys(initialized),
    sourceHintBudgetRemaining:sourceBudget,chunks:chunks.slice(),replacements:replacements.slice(),candidates:Object.keys(candidates).slice(0,256).map(function(k){var c=Object.assign({},candidates[k]),m=captured[k];c.initialized=!!(m&&m.loaded===true);if(c.initialized){var e=m.exports;try{c.exports=e&&(typeof e==='object'||typeof e==='function')?Object.getOwnPropertyNames(e).slice(0,80).map(function(n){var d=Object.getOwnPropertyDescriptor(e,n);return {name:n,type:d&&Object.prototype.hasOwnProperty.call(d,'value')?typeof d.value:'accessor_not_evaluated'};}):[];}catch(_){c.exports=[];c.reason='export_inventory_unavailable';}}return c;}),truncated:Object.keys(available).length>8192,retainedInterfaces:status().loaded,failed:Object.assign({},state.failed),
    limits:['Factory initialization observed in this JavaScript context only.','Absent modules may belong to other chunks or runtimes.','No module is force-required.']};}
  function capture(r){
    if(!state.installed||req||typeof r!=='function'||!r.m)return;
    req=r;state.webpackCaptured=true;
    var d=Object.getOwnPropertyDescriptor(r,'d');
    if(g.__pvRoles&&d&&Object.prototype.hasOwnProperty.call(d,'value')&&typeof d.value==='function'&&(d.writable||d.configurable)){
      defineExports=d.value;defineExportsDescriptor=d;wrappedDefineExports=function(){exportDefinitions++;try{return defineExports.apply(this,arguments);}finally{exportDefinitions--;}};
      defineProperty(r,'d',Object.assign({},d,{value:wrappedDefineExports}));
    }
    instrument(r.m);
  }
  function instrument(map){
    if(!state.installed||!map)return;
    if(maps.indexOf(map)<0){maps.push(map);if(maps.length>32)maps.shift();revision++;}
    Object.keys(map).forEach(function(id){
      if(!available[id]&&availableCount>=8192)return;
      var original=map[id];
      if(typeof original!=='function')return;
      if(original!==wrappers[id]&&factoryRefs[id]&&original!==factoryRefs[id]){
        var wasInitialized=!!initialized[id],affected=[];
        revision++;
        replacements.push(String(id));replacements=replacements.slice(-128);delete captured[id];delete initialized[id];
        if(g.__pvRoles)affected=g.__pvRoles.replaced(id);
        if(g.__pvCompat&&(!g.__pvRoles||wasInitialized||affected.length))g.__pvCompat.replaced(id);
      }
      if(original!==wrappers[id])factoryRefs[id]=original;
      if(!available[id]){available[id]=true;availableCount++;revision++;
        // Shape hints only. Never execute source or force-require candidate modules.
        if(!g.__pvRoles&&sourceBudget>0&&Object.keys(candidates).length<256){
          var sourceText='';try{sourceText=Function.prototype.toString.call(original);}catch(_){sourceBudget=0;}
          var src=sourceText.slice(0,Math.min(32768,sourceBudget)),hints=[];sourceBudget-=src.length;
          ['getFocusedTestID','getCurrentPage','getState','waitWithTimeout','isUserSignedIn','playbackMetadata','initialQuery'].forEach(function(k){if(src.indexOf(k)>=0)hints.push(k);});
          if(hints.length)candidates[id]={moduleId:String(id),hints:hints,sourceLength:src.length,semanticStatus:'unvalidated'};
        }
      }
      if(options&&options.lightweight&&!g.__pvRoles&&ids.indexOf(Number(id))<0&&!candidates[id])return;
      if(typeof original!=='function'||original===wrappers[id]||initialized[id])return;
      if(original===originals[id]&&wrappers[id]){map[id]=wrappers[id];return;}
      originals[id]=original;
      var wrapped=function(module){
        if(!state.installed)return original.apply(this,arguments);
        var result;
        factoryStack.push({id:id,module:module});
        try{result=original.apply(this,arguments);}
        catch(error){state.failed[id]=String(error).slice(0,300);revision++;throw error;}
        finally{factoryStack.pop();}
        // Webpack sets loaded=true AFTER this wrapper returns. Consumers must
        // wait for that bit, including when sampled inside a dependency call.
        if(!initialized[id]){initialized[id]=true;revision++;}
        if(ids.indexOf(Number(id))>=0||candidates[id])captured[id]=module;
        if(g.__pvRoles)try{g.__pvRoles.complete(id,module);}catch(_){state.reason='role_observation_incomplete';}
        if(req&&req.m[id]===wrapped)req.m[id]=original;
        return result;
      };
      wrappers[id]=wrapped;map[id]=wrapped;
    });
  }
  function watchPush(value){
    chunkArray=value;pushPrior=Object.getOwnPropertyDescriptor(value,'push');pushValue=value.push;
    if(pushPrior&&(!pushPrior.configurable||!Object.prototype.hasOwnProperty.call(pushPrior,'value'))){state.reason='chunk_push_not_wrappable';return;}
    function wrap(fn){return function(){
      if(state.installed)for(var i=0;i<arguments.length;i++){var d=arguments[i];if(Array.isArray(d)&&d[1]&&typeof d[1]==='object'){chunks.push({ids:Array.isArray(d[0])?d[0].slice(0,32).map(String):[],factoryCount:Object.keys(d[1]).length,observedAtMs:Date.now()});chunks=chunks.slice(-128);try{instrument(d[1]);}catch(_){state.reason='chunk_inventory_incomplete';}}}
      return fn.apply(this,arguments);
    };}
    var exposed=wrap(pushValue);
    pushGet=function(){return exposed;};pushSet=function(fn){pushAssigned=true;pushValue=fn;exposed=typeof fn==='function'?wrap(fn):fn;};
    Object.defineProperty(value,'push',{configurable:true,enumerable:pushPrior?pushPrior.enumerable:true,get:pushGet,set:pushSet});
  }
  function enqueue(value){
    if(queued||!Array.isArray(value)||typeof value.push!=='function')return;
    queued=true;
    // The pinned runtime processes queued chunk callbacks before its startup
    // requires. This adds no factories and never calls require(id).
    try{watchPush(value);value.push([['pv_module_observer_v2'],{},capture]);}
    catch(error){state.reason='chunk_callback_failed:'+String(error).slice(0,200);}
  }
  function restore(){
    state.installed=false;
    if(Object.defineProperty===observedDefine)Object.defineProperty=defineProperty;
    if(req&&req.d===wrappedDefineExports)defineProperty(req,'d',defineExportsDescriptor);
    maps.forEach(function(map){Object.keys(wrappers).forEach(function(id){if(map[id]===wrappers[id])map[id]=originals[id];});});
    var current=Object.getOwnPropertyDescriptor(g,key);
    if(current&&current.get===getter&&current.set===setter){
      if(prior)Object.defineProperty(g,key,prior);else delete g[key];
    }
    if(chunkArray){var d=Object.getOwnPropertyDescriptor(chunkArray,'push');if(d&&d.get===pushGet&&d.set===pushSet){
      if(pushAssigned)Object.defineProperty(chunkArray,'push',{value:pushValue,writable:true,configurable:true,enumerable:true});
      else if(pushPrior)Object.defineProperty(chunkArray,'push',pushPrior);else delete chunkArray.push;
    }}return status();
  }
  g.__pvModules={status:status,catalog:catalog,
    inventoryRevision:function(){return [revision,Object.keys(captured).filter(function(id){return captured[id].loaded===true;}).length,g.__pvRoles?g.__pvRoles.revision():0];},
    memory:function(){return {retainedChunkMaps:maps.length,factoryReferences:Object.keys(factoryRefs).length,capturedModules:Object.keys(captured).length,wrappedFactories:Object.keys(wrappers).length,catalogRevision:revision};},
    exports:function(id){
    var module=captured[id],raw=module&&module.loaded===true?module.exports:undefined;
    return g.__pvRoles?g.__pvRoles.exportsFor(id,raw):raw;
  },restore:restore};
  if(prior&&!(Object.prototype.hasOwnProperty.call(prior,'value')&&prior.value==null&&prior.configurable&&prior.writable)){
    if(Object.prototype.hasOwnProperty.call(prior,'value'))enqueue(prior.value);
    else state.reason='existing_chunk_accessor';
  }else{
    getter=function(){return prior?prior.value:undefined;};
    setter=function(value){
      Object.defineProperty(g,key,{value:value,writable:true,enumerable:prior?prior.enumerable:true,configurable:true});
      enqueue(value);
    };
    Object.defineProperty(g,key,{get:getter,set:setter,enumerable:prior?prior.enumerable:true,configurable:true});
  }
  return status();
})(globalThis, __PV_OPTIONS__);
