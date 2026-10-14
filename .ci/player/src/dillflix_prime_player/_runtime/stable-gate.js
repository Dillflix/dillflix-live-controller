/* Frida adapter. Keep all native objects alive until the restarted Prime exits. */
(function(bridge) {
  'use strict';
  const storage=Memory.alloc(128),listeners=[];
  let pending=null,generation=0;
  const startup=new NativeCallback(function(ctx,tid,isEval,phase,tag){
    try{bridge.startup(ctx,tid,!!isEval,phase,tag);}catch(e){bridge.fail(e);}
  },'void',['pointer','uint','uint','uint','uint']);
  const transfer=new NativeCallback(function(ctx,token){
    const p=pending;if(!p||p.token!==token)return;
    pending=null;
    try{p.callback(ctx);}catch(e){bridge.fail(e);}
  },'void',['pointer','uint']);
  const cm=new CModule(__PV_NATIVE_GATE_SOURCE__,{state:storage,startup_callback:startup,transfer_callback:transfer});
  const fn=(name,ret,args)=>new NativeFunction(cm[name],ret,args,{scheduling:'exclusive'});
  const size=fn('state_size','uint',[])();if(size>128)throw new Error('native gate storage too small');
  const init=fn('init_gate','void',[]),handoff=fn('handoff','void',['pointer','uint']);
  const arm=fn('arm','void',['uint']),disarm=fn('disarm','void',[]),close=fn('close_gate','void',[]);
  init();
  function install(address,isEval){listeners.push(Interceptor.attach(address,{onEnter:cm.on_enter,onLeave:cm.on_leave},ptr(isEval?1:0)));}
  function request(callback){
    if(pending)throw new Error('native gate already armed');
    const p={token:++generation,callback:callback};pending=p;arm(p.token);
    return {cancel:function(){if(pending===p){disarm();pending=null;}}};
  }
  return {install:install,handoff:handoff,request:request,
    close:function(){close();pending=null;},
    keepAlive:{cm:cm,storage:storage,startup:startup,transfer:transfer,listeners:listeners},
    status:function(){return {installedNativeGates:listeners.length,hotPatchCyclesAfterInstall:0};}};
})
