/* Read-only liveDetailsPageBTF request, derived from validated module 76442.
 * Preserve the response before defining a normalized broadcast selection API.
 * No page mount, cold module require, resolver, or playback invocation.
 */
(function(g,options,api){
  'use strict';
  var active=null,stopped=false;
  var ids=options.profile.adapter.modules;
  var required={schemes:ids.find_broadcast_schemes,timezone:ids.find_timezone,device:ids.find_device,features:ids.find_features,
    branding:ids.find_branding,headerParams:ids.find_broadcast_headers,headers:ids.find_headers,url:ids.find_url,edge:ids.find_edge,
    http:ids.find_http,method:ids.find_method,presentation:ids.find_presentation};
  function valid(r){try{api.check(r.command);g.__pvCompat.assertCommand(r.command);return !stopped&&!r.expired;}catch(_){return false;}}
  function done(r,status){api.emit('broadcasts_done',{id:r.id,status:status,pieces:r.pieces,
    warnings:r.redacted?['Sensitive transport fields were redacted.']:[]});}
  function response(r,value){
    if(!value||typeof value!=='object'||Array.isArray(value)||!Object.prototype.hasOwnProperty.call(value,'containerList'))throw Error('schema');
    var pieces=[],bytes=0,nodes=0,seen=[];
    function add(path,body,branch){
      var piece={id:r.id,index:pieces.length,path:path,body:body,branch:branch||null};
      bytes+=JSON.stringify(piece).length;
      if(pieces.length>=192||bytes>1500000)throw Error('limit');
      pieces.push(piece);
    }
    function split(v,path){
      if(++nodes>20000||path.length>32)throw Error('limit');
      // Wrap each subtree in its original key so bounded() still redacts
      // sensitive fields even when a large response has to be partitioned.
      var key=path.length?String(path[path.length-1]):'resource',wrapper={};
      Object.defineProperty(wrapper,key,{value:v,enumerable:true});
      var safe=api.bounded(wrapper);
      if(safe.losses.some(function(x){return /:(redacted|signed_url)$/.test(x);}))r.redacted=true;
      if(safe.lossless||(safe.losses.length<32&&safe.losses.every(function(x){return /:(redacted|signed_url)$/.test(x);}))) {
        if(JSON.stringify(safe.value).length<24000){add(path,safe.value[key]);return;}
      }
      if(!v||typeof v!=='object'||seen.indexOf(v)>=0)throw Error('structural_loss');
      var keys=Object.keys(v);if(keys.length>2048)throw Error('limit');
      seen.push(v);add(path,null,Array.isArray(v)?'array':'object');
      keys.forEach(function(k){
        if(k==='__proto__'||k==='constructor'||k==='prototype')throw Error('unsafe_key');
        var d=Object.getOwnPropertyDescriptor(v,k);
        if(!d||!Object.prototype.hasOwnProperty.call(d,'value'))throw Error('accessor');
        split(d.value,path.concat(Array.isArray(v)?Number(k):k));
      });seen.pop();
    }
    split(value,[]);
    pieces.forEach(function(p){api.emit('broadcasts_piece',p);});r.pieces=pieces.length;
  }
  function command(c){
    if(stopped)throw Error('broadcasts adapter stopped');
    if(active){done({id:c.id,pieces:0},'busy');return {submitted:false};}
    if(typeof c.contentId!=='string'||!/^amzn1\.dv\.gti\.[A-Za-z0-9-]+$/.test(c.contentId))throw Error('invalid event ID');
    if(!Number.isInteger(c.requestTimeoutMs)||c.requestTimeoutMs<1||c.requestTimeoutMs>120000)throw Error('invalid deadline');
    var m={};Object.keys(required).forEach(function(k){m[k]=g.__pvModules.exports(required[k]);if(!m[k])throw Error('broadcast dependency not initialized');});
    var pairs=[['itemId',c.contentId]],locale=m.device.LK.getInstance().osLocale;
    if(locale)pairs.push(['osLocale',locale]);
    pairs.push(['newMaturityRatings','true'],['widgetScheme',m.schemes.YF.LRC_LIVE_DETAIL_BTF_V1_4],['dynamicFeatures',m.schemes.BY()]);
    var tz=m.timezone.R();if(tz)pairs.push(['timeZoneId',tz]);
    pairs.push(['presentationScheme',m.presentation.A()]);
    if(m.branding._k())pairs.push(['isBrandingEnabled','true']);
    var features=m.features.z(m.features.X.LiveEventDetailsPageBTF);if(features)pairs.push(['clientFeatures',features]);
    if(pairs.some(function(p){return p[1]===undefined||p[1]===null;}))throw Error('broadcast request configuration unavailable');
    var url=new m.url.Z('',pairs).buildQueryParamsOnly();
    var headers=new m.headers.G([],m.headerParams.ZE(c.contentId));
    var client=new m.edge.P(m.http.Z.deviceProxyClient(),'detailsPage/liveDetailsPageBTF');
    var r={id:c.id,command:c,deadline:Date.now()+c.requestTimeoutMs,pieces:0,redacted:false,expired:false};active=r;
    function settle(value,error){
      if(active!==r)return;
      if(!valid(r)){if(!r.expired)done(r,'cancelled');active=null;return;}
      if(Date.now()>r.deadline){done(r,'timeout');active=null;return;}
      var status='resolved';
      try{if(error)status='request_failed';else{response(r,value);if(g.__pvCompat.qualified)g.__pvCompat.qualified('broadcasts');}}catch(error){if(error&&error.message==='schema'&&g.__pvCompat.contractFailure)g.__pvCompat.contractFailure('broadcasts','broadcast_response_schema');status='response_invalid_or_too_large';}
      done(r,status);active=null;
    }
    try{Promise.resolve(client.fetch(url,{method:m.method.Z.GET,headers:headers,avlrcTimeout:15000})).then(function(v){settle(v,false);},function(){settle(null,true);});}
    catch(_){settle(null,true);}
    return {submitted:true};
  }
  return {command:command,stop:function(){stopped=true;if(active&&!active.expired){done(active,'cancelled');active.expired=true;}},tick:function(){
    if(active&&!active.expired&&(!valid(active)||Date.now()>active.deadline)){done(active,valid(active)?'timeout':'cancelled');active.expired=true;}
  }};
})
