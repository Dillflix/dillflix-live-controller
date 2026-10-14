/* Minimal authenticated Find adapter. No page mount, resolver or research hooks.
 * Request derived from lrc-1.0.116634.0 module 3986; dependency exports are
 * retained only after normal app initialization. Live role contracts and
 * response validation govern support independently of runtime resource hashes.
 */
(function(g,options,api){
  'use strict';
  var ids=options.profile.adapter.modules;
  var required={schemes:ids.find_schemes,timezone:ids.find_timezone,device:ids.find_device,
    features:ids.find_features,branding:ids.find_branding,headerParams:ids.find_header_params,headers:ids.find_headers,url:ids.find_url,
    edge:ids.find_edge,http:ids.find_http,method:ids.find_method,capabilities:ids.find_capabilities,
    capabilityNames:ids.find_capability_names,flags:ids.find_flags,flagNames:ids.find_flag_names};
  var active=null,stopped=false;
  function valid(r){
    if(stopped||r.expired)return false;
    try{api.check(r.command);g.__pvCompat.assertCommand(r.command);return true;}
    catch(_){return false;}
  }
  function done(r,status){
    api.emit('catalog_search_done',{id:r.id,status:status,pieces:r.pieces,
      warnings:r.redacted?['Sensitive transport fields were redacted.']:[]});
  }
  function clean(r,value){
    var safe=api.bounded(value);
    // Redacting credentials/cursors is expected. Any structural loss fails
    // the request instead of silently returning an incomplete item.
    if(!safe.lossless){
      if(!safe.losses.length||safe.losses.length>=32||safe.losses.some(function(x){return !/:redacted$/.test(x);}))throw new Error('response_limit');
      r.redacted=true;
    }
    return safe.value;
  }
  function without(value,key){var out={};Object.keys(value).forEach(function(k){if(k!==key)Object.defineProperty(out,k,Object.getOwnPropertyDescriptor(value,k));});return out;}
  function response(r,value){
    if(!value||!Array.isArray(value.containers)||value.containers.length>64)throw new Error('response_schema');
    var pieces=[];
    function add(path,body){
      var data={id:r.id,index:pieces.length,path:path,body:clean(r,body)};
      r.bytes+=JSON.stringify(data).length;
      if(pieces.length>=192||r.bytes>1500000)throw new Error('response_limit');
      pieces.push(data);
    }
    add([],without(value,'containers'));
    value.containers.forEach(function(c,i){
      if(!c||!Array.isArray(c.items)||c.items.length>256)throw new Error('response_schema');
      add(['containers',i],without(c,'items'));
      c.items.forEach(function(item,j){
        if(!item||typeof item!=='object'||Array.isArray(item))throw new Error('response_schema');
        add(['containers',i,'items',j],item);
      });
    });
    // Validate and bound the whole response before emitting any pieces.
    pieces.forEach(function(p){api.emit('catalog_search_piece',p);});r.pieces=pieces.length;
  }
  function command(c){
    if(stopped)throw new Error("catalogue adapter stopped");
    if(active){done({id:c.id,pieces:0},'busy');return {submitted:false};}
    if(typeof c.query!=='string'||!c.query.trim()||c.query.length>512)throw new Error('invalid Find query');
    if(!Number.isInteger(c.requestTimeoutMs)||c.requestTimeoutMs<1||c.requestTimeoutMs>120000)throw new Error('invalid Find deadline');
    var modules={};Object.keys(required).forEach(function(k){
      var m=g.__pvModules.exports(required[k]);if(!m)throw new Error('Find dependency not initialized');modules[k]=m;
    });
    var m=modules;
    // These are the exact bindings used by the pinned Find client. Reading them
    // evaluates only Webpack's known export getters, not arbitrary accessors.
    var schemes=m.schemes,features=m.features,capabilities=m.capabilities.s.getInstance();
    var channelBundles=m.flags.b.getInstance().isFeatureEnabled(m.flagNames.L.ENABLE_CHANNEL_BUNDLES);
    var params={phrase:c.query,pageType:'search',pageId:'flex',isFlexEnabled:'true',
      shouldEnrichThroughCaspr:!capabilities.getCapability(m.capabilityNames.W.SUPPORTS_NATIVE_KEYBOARD)&&
        channelBundles?'true':'false'};
    // Inline the Find-only branch of 20439.I and the one-line 69563.x wrapper.
    // This avoids requiring a shared UI chunk just to read these settings.
    var widget=channelBundles===true?schemes.YF.LRC_SEARCH_V5:schemes.YF.LRC_SEARCH_V4_1;
    var pairs=[['featureScheme',schemes.Fd()],['dynamicFeatures',schemes.fe()],
      ['decorationScheme',schemes.Vu()],['widgetScheme',widget],
      ['isBrandingEnabled',m.branding._k().toString()]];
    var timezone=m.timezone.R(),locale=m.device.LK.getInstance().osLocale,clientFeatures=features.z(features.X.FindPage);
    if(timezone)pairs.push(['timeZoneId',timezone]);if(locale)pairs.push(['osLocale',locale]);
    if(clientFeatures)pairs.push(['clientFeatures',clientFeatures]);pairs.push(['disableFindFilter','true']);
    Object.keys(params).forEach(function(k){if(params[k])pairs.push([k,params[k]]);});
    var url=new m.url.Z('',pairs).buildQueryParamsOnly(true);
    var headers=new m.headers.G([],m.headerParams.sI());
    var client=new m.edge.P(m.http.Z.deviceProxyClient(),'find/findPageInitial',{queryParamRedactList:['phrase']});
    var r={id:c.id,command:c,deadline:Date.now()+c.requestTimeoutMs,pieces:0,bytes:0,redacted:false,expired:false};
    active=r;
    function settle(value,error){
      if(active!==r)return;
      if(!valid(r)){if(!r.expired)done(r,'cancelled');active=null;return;}
      if(Date.now()>r.deadline){done(r,'timeout');active=null;return;}
      var status='resolved';
      try{if(error)status='request_failed';else{response(r,value);if(g.__pvCompat.qualified)g.__pvCompat.qualified('search');}}
      catch(error){if(error&&error.message==='response_schema'&&g.__pvCompat.contractFailure)g.__pvCompat.contractFailure('search','find_response_schema');status='response_invalid_or_too_large';}
      done(r,status);active=null;
    }
    try{Promise.resolve(client.fetch(url,{method:m.method.Z.GET,headers:headers,avlrcTimeout:15000}))
      .then(function(v){settle(v,false);},function(){settle(null,true);});}
    catch(_){settle(null,true);}
    return {submitted:true};
  }
  return {command:command,stop:function(){stopped=true;if(active&&!active.expired){done(active,"cancelled");active.expired=true;}},tick:function(){
    if(!active||active.expired)return;
    if(!valid(active)||Date.now()>active.deadline){
      done(active,valid(active)?'timeout':'cancelled');active.expired=true;
      // The app client has no verified cancellation interface. Keep the slot
      // occupied until it settles; late data is discarded, not published.
    }
  }};
})
