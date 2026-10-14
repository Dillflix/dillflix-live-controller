/* Frida-side, bounded requests through an installed native gate. No runtime patch cycling. */
(function(bridge) {
  'use strict';
  var owner=null,pulse=null,closed=false,lastHealth=null;
  var stats={ready:false,startupCallbacksDisabled:false,pulses:0,completed:0,timeouts:0,
    owningThreadReturns:0,totalArmedMs:0,maxArmedMs:0,handoffTimeMs:null};
  function handoff(s,health){
    if(closed||owner||!health||!health.dispatcherReady)return false;
    if(!Number.isInteger(s.threadId))throw new Error('owning thread was not recorded');
    owner={ctx:s.ctx,threadId:s.threadId};lastHealth=health;
    bridge.handoff(s.ctx,s.threadId);stats.ready=true;stats.startupCallbacksDisabled=true;stats.handoffTimeMs=Date.now();
    bridge.report('lightweight_handoff',{context:s.ctx.toString(),threadId:s.threadId,continuousJavaScriptExecutionHooks:0,installedNativeGates:2,hotPatchCyclesAfterInstall:0,
      dispatcher:'application_event_queue_wait_boundary',note:'Native gates remain installed; idle calls do not enter Frida JavaScript. Requests change data, not executable code.'});
    return true;
  }
  function finish(p,result,error){
    if(pulse!==p)return;
    if(p.timer!==null)clearTimeout(p.timer);
    if(p.hook)p.hook.cancel();
    var duration=Date.now()-p.started;
    stats.totalArmedMs+=duration;stats.maxArmedMs=Math.max(stats.maxArmedMs,duration);
    pulse=null;
    if(error)p.reject(error);else p.resolve(result);
  }
  function request(command){
    if(closed||!owner)return Promise.reject(new Error('lightweight transport not ready'));
    if(pulse){if(command)return Promise.reject(new Error('transport transfer already in flight'));return pulse.promise;}
    var p={started:Date.now(),executing:false,command:command||null,hook:null,timer:null};
    p.promise=new Promise(function(resolve,reject){p.resolve=resolve;p.reject=reject;});pulse=p;stats.pulses++;
    try{
      p.hook=bridge.arm(function(ctx){
        if(pulse!==p||p.executing)return;
        stats.owningThreadReturns++;p.executing=true;
        try{
          var result=bridge.exchange(ctx,p.command);
          if(result&&result.health)lastHealth=result.health;
          stats.completed++;finish(p,result);
        }catch(error){finish(p,null,error);}
      });
      p.timer=setTimeout(function(){
        if(pulse!==p)return;
        stats.timeouts++;
        finish(p,null,new Error('no eligible owning-thread return within 1200 ms; this request was not submitted'));
      },1200);
    }catch(error){finish(p,null,error);}
    return p.promise;
  }
  function status(){return Object.assign({},stats,{continuousJavaScriptExecutionHooks:owner?0:null,installedNativeGates:2,hotPatchCyclesAfterInstall:0,
    transferArmed:!!pulse,currentArmedMs:pulse?Date.now()-pulse.started:0,health:lastHealth});}
  function close(){closed=true;bridge.close();if(pulse)finish(pulse,null,new Error('controller stopped before transfer'));}
  return {handoff:handoff,request:request,status:status,close:close,ready:function(){return !!owner&&!closed;}};
})
