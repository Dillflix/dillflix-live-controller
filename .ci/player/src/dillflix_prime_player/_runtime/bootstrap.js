/* Resource identity is independent of the native engine and of module contracts. */
(function(g, options) {
  'use strict';
  if(g.__pvBootstrap)return g.__pvBootstrap.status();
  var original=JSON.parse, profile=options.profile, adapter=profile.adapter;
  var state={installed:true,matched:false,changed:false,rejected:null,mode:options.mode,
    generation:0,version:null,resources:[],javascript:false,nativeResource:false,behavior:false};
  var signature='',resourceSignature='',directSignature='',frozen=false,nativeVerified=false,nativeReason='binary_not_observed',schemaFailure=null,replaced={},unmappedReplacement=false,jsQuarantined=false,behaviorQuarantine={},nativeDescriptorResource=null,qualified={},contractFailures={};
  var lastTransportKey=null,nativeDescriptorSignature='',directDescriptorSignature='',nativeDescriptorRevision=0;
  function examine(value,direct){
    var c=value&&(value.config||value);
    if(!c||typeof c!=='object'||!Array.isArray(c.vendedResources)||!c.weblabs)return;
    // Signed URLs may rotate without changing resource identity. Refresh this
    // private descriptor independently of public compatibility generations.
    var nativeResources=c.vendedResources.filter(function(r){return r.name===profile.testRuntime.resourceName;});
    var test=nativeResources.length===1?nativeResources[0]:null,m=test&&test.metadata;
    var descriptor=test?{name:test.name,url:test.url,metadata:test.metadata,compression:test.compression}:null;
    var descriptorWire=JSON.stringify(descriptor);
    if(!direct||descriptorWire!==directDescriptorSignature){
      if(direct)directDescriptorSignature=descriptorWire;
      if(descriptorWire!==nativeDescriptorSignature){nativeDescriptorSignature=descriptorWire;nativeDescriptorResource=original(descriptorWire);nativeDescriptorRevision++;}
    }
    // Export only identities, never device IDs, signed URLs, signatures or account data.
    var resources=c.vendedResources.slice(0,512).map(function(r){var m=r.metadata||{},b=m.binaryMetadata||{};
      return {name:String(r.name||'').slice(0,256),sha256:m.dataIntegrity&&m.dataIntegrity.sha256||null,
        vmType:m.vmType||null,target:b.target_triple||null,engine:b.engine_version||null};
    }).sort(function(a,b){return a.name<b.name?-1:a.name>b.name?1:0;});
    var wire=JSON.stringify([c.version,resources]);
    if(direct){if(wire===directSignature)return;directSignature=wire;}
    if(wire===signature)return;
    var nextResourceSignature=JSON.stringify(resources);
    if(nextResourceSignature!==resourceSignature){schemaFailure=null;resourceSignature=nextResourceSignature;}
    signature=wire;state.generation++;qualified={};contractFailures={};state.version=String(c.version||'unknown').slice(0,128);
    state.resources=resources;state.truncated=c.vendedResources.length>512;
    function match(name,hash){var found=resources.filter(function(r){return r.name===name;});return found.length===1&&found[0].sha256===hash;}
    var appResources=resources.filter(function(r){return r.name===profile.appResource.name;});
    // Resource fingerprints identify the observed generation. Live export
    // contracts and operation results decide whether its capabilities work.
    state.javascript=!state.truncated&&appResources.length===1&&/^[a-f0-9]{64}$/i.test(appResources[0].sha256||'');
    state.javascriptKnown=state.javascript&&match(profile.appResource.name,profile.appResource.integritySha256);
    state.javascriptIdentity=state.javascript?appResources[0].sha256:null;
    state.nativeResource=!!(!state.truncated&&match(profile.testRuntime.resourceName,profile.testRuntime.compressedSha256)&&m&&m.featureFlagSelectors&&m.featureFlagSelectors.bootstrapRustClientTestUtils===true&&m.binaryMetadata&&m.binaryMetadata.target_triple==='armv7thumb'&&m.binaryMetadata.engine_version==='6.0.0');
    state.behaviors={};Object.keys(adapter.behaviorRequirements).forEach(function(cap){state.behaviors[cap]=!state.truncated&&adapter.behaviorRequirements[cap].every(function(k){return resources.filter(function(r){return r.name===k&&/^[a-f0-9]{64}$/i.test(r.sha256||'');}).length===1;});});
    state.behavior=Object.keys(state.behaviors).every(function(k){return state.behaviors[k];});
    state.nativeClient=Object.keys(adapter.nativeClientResources).every(function(k){return resources.filter(function(r){return r.name===k&&r.target==='armv7thumb'&&r.engine==='6.0.0'&&/^[a-f0-9]{64}$/i.test(r.sha256||'');}).length===1;});
    state.matched=state.javascript;state.rejected=null;
    // Only the initial bootstrap may opt into a verified inspection resource.
    // Later manifests are observations, never a request to reload/restart Prime.
    if(!frozen&&state.generation===1&&state.nativeResource&&options.mode==='test-utils'){
      state.originalFlag=c.weblabs.bootstrapRustClientTestUtils===true;
      c.weblabs.bootstrapRustClientTestUtils=true;state.changed=!state.originalFlag;
    }
    state.flag=c.weblabs.bootstrapRustClientTestUtils===true;
  }
  function status(){return JSON.parse(JSON.stringify(state));}
  function moduleContract(role){
    if(!state.javascript)return 'javascript_resource_unknown';
    if(unmappedReplacement)return 'runtime_restart_required';
    var id=adapter.modules[role];
    if(replaced[id])return 'module_replaced:'+role;
    var m;try{m=g.__pvModules&&g.__pvModules.exports(id);}catch(_){return 'export_read_failed:'+role;}
    if(g.__pvRoles){var binding=g.__pvRoles.status(role);if(binding.state==='missing'||binding.state==='initializing'||binding.state==='ambiguous'||binding.state==='incompatible')return 'module_'+binding.state+':'+role;}
    if(!m)return 'module_not_initialized:'+role;
    var values=adapter.valueContracts&&adapter.valueContracts[role]||[];
    for(var vi=0;vi<values.length;vi++){try{if(!m[values[vi]])return 'export_missing:'+role+'.'+values[vi];}catch(_){return 'export_read_failed:'+role;}}
    var paths=adapter.contracts[role]||[];
    for(var i=0;i<paths.length;i++){
      var v=m,parts=paths[i].split('.');
      try{for(var j=0;j<parts.length;j++)v=v&&v[parts[j]];}catch(_){return 'export_read_failed:'+role;}
      if(typeof v!=='function')return 'export_contract_failed:'+role+'.'+paths[i];
    }
    return null;
  }
  function group(name){var roles=adapter.groups[name]||[],errors=[];roles.forEach(function(r){var e=moduleContract(r);if(e)errors.push(e);});return errors;}
  function decision(errors){return {available:errors.length===0,reasons:errors};}
  function snapshot(includeInventory){
    var core=group('core'),nativeErrors=group('native');
    if(!state.nativeResource)nativeErrors.push('native_resource_unknown');
    if(!state.nativeClient)nativeErrors.push('native_client_resource_unknown');
    if(schemaFailure)nativeErrors.push(schemaFailure);
    if(!nativeVerified)nativeErrors.push(nativeReason);
    var behavior=state.behavior?[]:['required_resources_missing'];
    function behaviorFor(c){return state.behaviors&&state.behaviors[c]?[]:[c+':required_resources_missing'];}
    var caps={core:decision(core),javascript_navigation:decision(core.concat(group('navigation'),behaviorFor('navigation'))),
      resolve:decision(core.concat(group('resolver'),behaviorFor('play'))),
      play:decision(core.concat(group('resolver'),behaviorFor('play'))),
      native_inspection:decision(core.concat(nativeErrors)),
      search:decision(core.concat(group('catalog_search'),behaviorFor('catalog_search'))),
      broadcasts:decision(core.concat(group('broadcasts'),behaviorFor('broadcasts'))),
      renderer_search:decision(core.concat(group('search'),nativeErrors,behaviorFor('search'))),
      playback_status:decision(core.concat(nativeErrors)),
      javascript_focus:decision(core)};
    Object.keys(caps).forEach(function(k){if(contractFailures[k]){caps[k].available=false;caps[k].reasons.push(contractFailures[k]);}var nativeSchemaFailed=schemaFailure&&['native_inspection','renderer_search','playback_status'].indexOf(k)>=0;caps[k].qualification=contractFailures[k]||nativeSchemaFailed?'contract_failed':qualified[k]||'probeable_contract';});
    var adaptation=g.__pvRoles?(includeInventory===false?g.__pvRoles.summary():g.__pvRoles.snapshot()):{restartRequired:unmappedReplacement};
    adaptation.restartRequired=adaptation.restartRequired||unmappedReplacement;
    adaptation.contractFailures=Object.assign({},contractFailures);
    adaptation.nativeResponseFailure=schemaFailure;
    return {schema:'prime-compatibility/1',adapter:adapter.id,generation:state.generation,adaptation:adaptation,
      runtimeVersion:state.version,manifestScope:'advertised resources; not proof all resources are loaded',
      resources:includeInventory===false?undefined:state.resources,truncated:!!state.truncated,quarantine:{javascript:jsQuarantined,behavior:behaviorQuarantine,recovery:'automatic controlled reinitialization after loaded binding replacement'},
      layers:{javascript:decision(state.javascript?[]:['javascript_resource_unknown']),
        native_inspection:decision(nativeErrors),behavior:decision(behavior)},capabilities:caps,
      modules:includeInventory===false?undefined:g.__pvModules?g.__pvModules.catalog():null,policy:'Initialized contract discovery; independently validated operations; loaded replacements require controlled restart'};
  }
  function poll(){
    // Recheck live contracts, including mutations of existing exports. Only the
    // diagnostic inventory is conditional; authorization is never cached.
    var value=snapshot(false),revision=g.__pvModules&&g.__pvModules.inventoryRevision?g.__pvModules.inventoryRevision():null;
    var key=JSON.stringify([value,revision]);
    if(key===lastTransportKey)return null;
    value.resources=state.resources;value.modules=g.__pvModules?g.__pvModules.catalog():null;
    if(g.__pvRoles){value.adaptation=g.__pvRoles.snapshot();value.adaptation.restartRequired=value.adaptation.restartRequired||unmappedReplacement;value.adaptation.contractFailures=Object.assign({},contractFailures);value.adaptation.nativeResponseFailure=schemaFailure;}
    lastTransportKey=key;return value;
  }
  var operations={'broadcasts-data':'broadcasts','search-data':'search','search':'renderer_search','watch-search':'renderer_search','search-results':'renderer_search',
    'inspect-native':'native_inspection','inspect-selection':'native_inspection','inspect-player':'native_inspection',
    'watch-page':'core','playback-status':'core','play-content':'play','play-live':'play','resolve-content':'resolve','resolve-live':'resolve',
    'stop-playback':'javascript_navigation','navigate':'javascript_navigation','back':'javascript_navigation','focus':'javascript_focus','snapshot':'javascript_focus',
    'trace-js':'javascript_focus','mark':'core','inspect-state':'core'};
  function assertCommand(c){
    var cap=operations[c.op],s=snapshot(false),d=cap&&s.capabilities[cap];
    if(c.compatibilityGeneration!==undefined&&c.compatibilityGeneration!==s.generation)throw new Error('compatibility:generation_changed; command not invoked');
    if(!d||!d.available)throw new Error('compatibility:'+(cap||'unknown_operation')+':'+(d?d.reasons.join(','):'unsupported'));
    return s.generation;
  }
  function patched(){var v=original.apply(this,arguments);try{examine(v);}catch(_){state.javascript=false;state.nativeResource=false;state.behavior=false;state.matched=false;state.error='manifest_inspection_failed';}return v;}
  JSON.parse=patched;
  g.__pvCompat={snapshot:snapshot,poll:poll,assertCommand:assertCommand,allows:function(c){if(c==='core')return group('core').length===0;return snapshot(false).capabilities[c].available;},
    native:function(ok,reason){nativeVerified=!!ok;nativeReason=reason||'binary_not_observed';},
    replaced:function(id){replaced[id]=true;unmappedReplacement=true;qualified={};state.generation++;},
    bindingChanged:function(){qualified={};state.generation++;if(g.__pvAppProbe)unmappedReplacement=true;},
    qualified:function(capability){qualified[capability]='operation_validated';},
    contractFailure:function(capability,reason){contractFailures[capability]='response_contract_failed:'+String(reason).slice(0,80);},
    schemaFailure:function(reason){schemaFailure='response_schema_failed:'+reason;},
    freeze:function(){frozen=true;},generation:function(){return state.generation;}};
  g.__pvBootstrap={status:status,knownJavascript:function(){return state.javascriptKnown;},javascriptIdentity:function(){return state.javascriptIdentity;},nativeDescriptor:function(){return nativeDescriptorResource;},nativeDescriptorRevision:function(){return nativeDescriptorRevision;},refresh:function(includeStatus){if(typeof g.config==='object')examine(g.config,true);if(includeStatus!==false)return status();},
    restore:function(){if(JSON.parse===patched)JSON.parse=original;state.installed=false;return status();}};
  try{if(typeof g.config==='object')examine(g.config,true);}catch(_){}
  return status();
})(globalThis, __PV_OPTIONS__);

