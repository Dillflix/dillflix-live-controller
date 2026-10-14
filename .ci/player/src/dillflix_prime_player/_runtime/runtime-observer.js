/* Generic passive boundaries, independent of the Photon app-module gate. */
(function(g){
  'use strict';
  if(g.__pvRuntimeObserver)return {installed:true};
  var hooks=[],queue=[],dropped=0,stopped=false,lastCoverage='',sequence=0;
  function emit(kind,data){if(stopped)return;if(queue.length>=128){queue.shift();dropped++;}queue.push({kind:kind,timeMs:Date.now(),sequence:++sequence,data:data});}
  function ownValue(obj,key){var d=Object.getOwnPropertyDescriptor(obj,key);return d&&Object.prototype.hasOwnProperty.call(d,'value')?d.value:undefined;}
  function input(value){
    var result={};if(!value||typeof value!=='object')return result;
    ['input_type','event_type','key_code','modifier_flags','button_code','pointer_id','last_known_input_event_check'].forEach(function(k){
      var v=ownValue(value,k);if(typeof v==='number'||typeof v==='string'&&v.length<=100)result[k]=v;
    });return result;
  }
  function argumentsSummary(args){return Array.prototype.slice.call(args,0,10).map(function(x){
    var v={type:typeof x};if(typeof x==='string'){v.length=x.length;if(x.length<=512&&/\.(?:js|bin|wasm|so|br)(?:$|\?)/.test(x)&&!/https?:|token|auth/i.test(x))v.resource=x;}
    else if(typeof x==='number'||typeof x==='boolean')v.value=x;return v;
  });}
  function hook(obj,key,namespace,isInput){
    if(hooks.some(function(h){return h.obj===obj&&h.key===key;}))return true;
    var d=Object.getOwnPropertyDescriptor(obj,key),original=d&&d.value;
    if(!d||typeof original!=='function'||!d.writable&&!d.configurable)return false;
    var wrapped=function(){
      var result,failed=false,error;
      try{result=original.apply(this,arguments);return result;}catch(e){failed=true;error=e;throw e;}
      finally{try{
        if(isInput){if(result&&typeof result==='object')emit('platform_input_return',{namespace:namespace,method:key,event:input(result),eventKeys:Object.keys(result).slice(0,40),codeNamespace:'platform_native_not_android'});}
        else emit('runtime_boundary',{namespace:namespace,method:key,arguments:argumentsSummary(arguments),thrown:failed,error:failed?String(error).slice(0,200):undefined,resultType:typeof result});
      }catch(_){}}
    };
    try{Object.defineProperty(obj,key,{value:wrapped,configurable:d.configurable,enumerable:d.enumerable,writable:d.writable});}
    catch(_){return false;}
    hooks.push({obj:obj,key:key,descriptor:d,wrapped:wrapped});return true;
  }
  function install(){
    var coverage=[];
    ['Native','DirectNative','VmManager'].forEach(function(name){
      var obj=ownValue(g,name);if(!obj||typeof obj!=='object'&&typeof obj!=='function')return;
      Object.getOwnPropertyNames(obj).forEach(function(k){
        var selected=k==='FetchOneInputEvent'||/^(?:Create.*VmWith|CreateCoroutineVm|OpenNamedBackgroundVm|Load.*InCoroutine|WebAssembly_CreateModuleInstance)/.test(k);
        if(selected)coverage.push({namespace:name,method:k,installed:hook(obj,k,name,k==='FetchOneInputEvent')});
      });
    });
    var wire=JSON.stringify(coverage);if(wire!==lastCoverage){lastCoverage=wire;emit('runtime_hook_coverage',{hooks:coverage,limits:['Only application calls through these JavaScript-visible wrappers are observed.','Calls entirely within native code bypass these wrappers.','Script bodies, bytecode and credentials are not recorded.']});}
  }
  function tick(){if(!stopped)install();var records=queue;queue=[];return {records:records,dropped:dropped};}
  function stop(){hooks.slice().reverse().forEach(function(h){if(ownValue(h.obj,h.key)===h.wrapped)Object.defineProperty(h.obj,h.key,h.descriptor);});stopped=true;return tick();}
  g.__pvRuntimeObserver={tick:tick,stop:stop};install();return {installed:true};
})(globalThis);
