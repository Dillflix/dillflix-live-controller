/* Observe initialized exports, never initialize a module or invoke an accessor. */
(function(g,options){
  'use strict';
  if(g.__pvRoles)return g.__pvRoles.snapshot();
  var adapter=options.profile.adapter,roles=Object.keys(adapter.modules),candidates={},bindings={},rejected={},accessors=[],getterKinds=new WeakMap(),prefixCache={},rolesById={},revision=0,restartRequired=false,limited=false;
  roles.forEach(function(r){var id=String(adapter.modules[r]);(rolesById[id]||(rolesById[id]=[])).push(r);});
  var extra={find_flags:['prototype.isFeatureEnabled','prototype.getFeatureTreatment'],find_capabilities:['prototype.getCapability','prototype.getCapabilities'],playback_result:['prototype.navigate'],plugin_command:['prototype.typeTag','prototype.getCommandId']};
  var namedDiscovery={focus:true,page:true,navigation:true,event_loop:true,auth:true,find_http:true,bridge:true};
  // Command/result base methods occur on unrelated classes throughout Prime.
  // They validate the expected slot, but cannot identify a relocated class.
  var slotOnly={plugin_command:true,playback_result:true};
  var discoveryRoles=roles.filter(function(r){return !slotOnly[r]&&(namedDiscovery[r]||extra[r]);}),observationRoles={},pathCache={},contractCache={};
  function own(o,k){if(!o||(typeof o!=='object'&&typeof o!=='function'))return undefined;var d;try{d=Object.getOwnPropertyDescriptor(o,k);}catch(_){return undefined;}return d&&Object.prototype.hasOwnProperty.call(d,'value')?d.value:undefined;}
  function member(o,k){for(var depth=0;o&&depth<5;depth++){var d;try{d=Object.getOwnPropertyDescriptor(o,k);}catch(_){return undefined;}if(d)return Object.prototype.hasOwnProperty.call(d,'value')?d.value:undefined;try{o=Object.getPrototypeOf(o);}catch(_){return undefined;}}}
  function path(o,p){var keys=pathCache[p]||(pathCache[p]=p.split('.'));for(var i=0;i<keys.length&&o!=null;i++)o=member(o,keys[i]);return o;}
  function prefixes(role){if(prefixCache[role])return prefixCache[role];var out={};(adapter.contracts[role]||[]).forEach(function(p){out[p.split('.')[0]]=true;});(adapter.valueContracts&&adapter.valueContracts[role]||[]).forEach(function(p){out[p.split('.')[0]]=true;});prefixCache[role]=Object.keys(out);return prefixCache[role];}
  function hintsCurrent(){return typeof adapter.roleHintResourceSha256==='string'&&g.__pvBootstrap&&g.__pvBootstrap.javascriptIdentity&&g.__pvBootstrap.javascriptIdentity()===adapter.roleHintResourceSha256;}
  function hinted(role,prefix,id,key){return hintsCurrent()&&(adapter.roleHints||[]).some(function(h){return h.role===role&&(h.bindings||[]).some(function(b){return String(b.moduleId)===String(id)&&b.observedExport===key&&b.export===prefix;});});}
  function contract(role,prefix,value,relocated){
    if(relocated&&slotOnly[role])return false;
    // The barrel's custom getter may never be read on an unfamiliar resource.
    // Identify the naturally initialized defining export without reading it or
    // constructing its singleton. The channel tag and methods distinguish it.
    if(role==='bridge')return typeof value==='function'&&own(value,'CROSS_APP_EVENT_CHANNEL')==='CROSS_APP_EVENT_CHANNEL'&&
      ['getInstance','prototype.subscribe','prototype.sendMessage','prototype.installHandler'].every(function(p){return typeof path(value,p)==='function';});
    var cacheKey=role+':'+prefix,paths=contractCache[cacheKey];
    if(!paths)paths=contractCache[cacheKey]=(adapter.contracts[role]||[]).filter(function(p){return p.split('.')[0]===prefix;}).map(function(p){return p.split('.').slice(1).join('.');});
    if(!paths.length)return value!==undefined&&value!==null&&!relocated;
    if(!paths.every(function(p){return typeof(p?path(value,p):value)==='function';}))return false;
    var traits=extra[role]||[];
    if(traits.length&&!traits.every(function(p){return typeof path(value,p)==='function';}))return false;
    // Bare minified functions and generic singleton accessors are not discoverable
    // by shape. Their existing slot is a probe candidate, not semantic evidence.
    if(relocated&&!traits.length&&!namedDiscovery[role])return false;
    return true;
  }
  function notify(role,old,next){
    revision++;
    if(old&&(old.id!==next.id||old.module!==next.module||old.key!==next.key||old.value!==next.value)){
      restartRequired=true;
      if(g.__pvCompat&&g.__pvCompat.bindingChanged)g.__pvCompat.bindingChanged(role);
    }
  }
  function fresh(c,role){
    if(c.invalidated)return false;
    var d;try{d=Object.getOwnPropertyDescriptor(c.module.exports,c.key);}catch(_){d=null;}
    var valid=c.module.exports===c.exports&&d&&c.descriptor&&
      (Object.prototype.hasOwnProperty.call(c.descriptor,'value')?
        Object.prototype.hasOwnProperty.call(d,'value')&&d.value===c.value:
        !Object.prototype.hasOwnProperty.call(d,'value')&&d.get===c.descriptor.get);
    if(valid&&!Object.prototype.hasOwnProperty.call(d,'value')&&
       (getterKinds.get(d.get)==='webpack_export'||g.__pvBootstrap&&g.__pvBootstrap.knownJavascript&&g.__pvBootstrap.knownJavascript())){
      // Export getters installed by Webpack expose a live binding. Re-read this
      // selected export, not a module inventory, before using the retained value.
      var previous=c.value;try{valid=d.get.call(c.exports)===previous;}catch(_){valid=false;}
    }
    if(!valid){c.invalidated=true;restartRequired=true;revision++;if(g.__pvCompat&&g.__pvCompat.bindingChanged)g.__pvCompat.bindingChanged(role);}
    return !!valid;
  }
  function observe(id,module,key,value){
    if(!module||typeof key!=='string')return;
    if((!value||typeof value!=='object'&&typeof value!=='function')&&!expectedSlot(id,key))return;
    // Most roles cannot relocate by shape. Do not scan all their contracts for
    // every unrelated export during Prime's synchronous startup.
    var relevant=observationRoles[id];
    if(!relevant){var local=rolesById[String(id)]||[];relevant=observationRoles[id]=local.concat(discoveryRoles.filter(function(r){return local.indexOf(r)<0;}));}
    relevant.forEach(function(role){
      var expected=prefixes(role),same=String(adapter.modules[role])===String(id);
      expected.forEach(function(prefix){
        var cached=hinted(role,prefix,id,key),relocated=!same||key!==prefix;
        if(!contract(role,prefix,value,relocated)){if(same&&key===prefix&&!rejected[role]){rejected[role]=true;revision++;}return;}
        var slot=role+':'+prefix,list=candidates[slot]||(candidates[slot]=[]);
        var existing=list.find(function(c){return c.value===value||(c.id===String(id)&&c.module===module&&c.key===key);});
        if(existing){if(existing.value!==value){existing.value=value;revision++;if(bindings[slot]){restartRequired=true;if(g.__pvCompat&&g.__pvCompat.bindingChanged)g.__pvCompat.bindingChanged(role);}}return;}
        if(list.length>=8){limited=true;return;}
        list.push({id:String(id),module:module,exports:module.exports,descriptor:Object.getOwnPropertyDescriptor(module.exports,key),key:key,value:value,relocated:relocated,evidence:cached?'cached_slot_probe':relocated?'unique_named_contract':'existing_slot_probe'});revision++;
        if(list.length===1){var next=list[0];notify(role,bindings[slot],next);bindings[slot]=next;}
        else if(bindings[slot]){delete bindings[slot];restartRequired=!!g.__pvAppProbe||restartRequired;if(g.__pvCompat&&g.__pvCompat.bindingChanged)g.__pvCompat.bindingChanged(role);}
      });
    });
  }
  function complete(id,module){
    var exports=module&&module.exports;if(!exports||(typeof exports!=='object'&&typeof exports!=='function'))return;
    var names;try{names=Object.getOwnPropertyNames(exports);}catch(_){return;}
    if(names.length>128)limited=true;
    // A large barrel must not hide its expected exports behind the inventory cap.
    var selected=names.slice(0,128);(rolesById[String(id)]||[]).forEach(function(r){prefixes(r).forEach(function(k){if(selected.indexOf(k)<0)selected.push(k);});});
    selected.forEach(function(key){var d=Object.getOwnPropertyDescriptor(exports,key);if(d&&Object.prototype.hasOwnProperty.call(d,'value'))observe(id,module,key,d.value);});
  }
  function expectedSlot(id,key,unconfirmed){
    if((rolesById[String(id)]||[]).some(function(role){return prefixes(role).indexOf(key)>=0;}))return true;
    return (unconfirmed||hintsCurrent())&&(adapter.roleHints||[]).some(function(h){return roles.indexOf(h.role)>=0&&(h.bindings||[]).some(function(b){return String(b.moduleId)===String(id)&&b.observedExport===key&&prefixes(h.role).indexOf(b.export)>=0;});});
  }
  function accessor(id,module,key,getter,provenance){
    if(typeof getter!=='function')return;
    getterKinds.set(getter,provenance);
    if(!expectedSlot(id,key,true))return;
    if(accessors.length>=256){limited=true;return;}
    accessors.push({id:String(id),module:module,key:key,getter:getter,provenance:provenance,read:false});
  }
  function probe(id){accessors.forEach(function(a){
    if(a.read||a.module.loaded!==true||!expectedSlot(a.id,a.key))return;
    var known=!!(g.__pvBootstrap&&g.__pvBootstrap.knownJavascript&&g.__pvBootstrap.knownJavascript());
    if(a.provenance!=='webpack_export'&&!known)return;
    // Only registered expected slots are evaluated, once after initialization.
    // Discovery of other slots remains passive even when they have getters.
    a.read=true;
    try{observe(a.id,a.module,a.key,a.getter.call(a.module.exports));}catch(_){revision++;}
  });accessors=accessors.filter(function(a){return !a.read;});}
  function status(role){
    var expected=prefixes(role);if(!expected.length)return {state:'existing_slot_probe',role:role};
    for(var i=0;i<expected.length;i++){var slot=role+':'+expected[i],list=candidates[slot]||[];
      if(list.length>1)return {state:'ambiguous',role:role};
      if(!list.length)return {state:rejected[role]?'incompatible':'missing',role:role};
      if(!fresh(list[0],role))return {state:'incompatible',role:role};
      if(list[0].module.loaded!==true)return {state:'initializing',role:role};
      if(!contract(role,expected[i],list[0].value,list[0].relocated))return {state:'incompatible',role:role};
    }
    return {state:'probeable',role:role};
  }
  function exportsFor(id,raw){
    probe(id);
    var associated=rolesById[String(id)]||[];
    if(!associated.length)return raw;
    if(associated.every(function(r){return prefixes(r).length===0;}))return raw;
    var facade=Object.create(null),has=false,contractKeys={};associated.forEach(function(r){prefixes(r).forEach(function(k){contractKeys[k]=true;});});
    // Keep non-contract exports only from this module's naturally observed data.
    // Contract exports below always use the validated role binding.
    if(raw)Object.getOwnPropertyNames(raw).slice(0,128).forEach(function(k){var d=Object.getOwnPropertyDescriptor(raw,k);if(!contractKeys[k]&&d&&Object.prototype.hasOwnProperty.call(d,'value')){facade[k]=d.value;has=true;}});
    associated.forEach(function(role){prefixes(role).forEach(function(prefix){
      var c=bindings[role+':'+prefix];
      if(c&&fresh(c,role)&&c.module.loaded===true&&contract(role,prefix,c.value,c.relocated)){facade[prefix]=c.value;has=true;}
    });});
    return has?facade:undefined;
  }
  function replaced(id){
    accessors=accessors.filter(function(a){return a.id!==String(id);});
    var affected=[];
    Object.keys(candidates).forEach(function(slot){if(candidates[slot].some(function(c){return c.id===String(id);})){delete candidates[slot];delete bindings[slot];affected.push(slot.split(':')[0]);}});
    if(affected.length){revision++;restartRequired=true;}
    return affected;
  }
  function summary(){return {schema:'prime-role-bindings/1',revision:revision,restartRequired:restartRequired,limited:limited};}
  function snapshot(){return Object.assign(summary(),{
    roles:roles.map(function(role){var s=status(role);s.bindings=prefixes(role).map(function(p){var c=bindings[role+':'+p];return c?{export:p,moduleId:c.id,observedExport:c.key,evidence:c.evidence}:null;}).filter(Boolean);return s;}),
    policy:'Observed initialized exports; shape identifies probe candidates; operation validation is still required.'});}
  g.__pvRoles={observe:observe,accessor:accessor,complete:complete,exportsFor:exportsFor,status:status,replaced:replaced,snapshot:snapshot,summary:summary,revision:function(){return revision;}};
  return snapshot();
})(globalThis,__PV_OPTIONS__);
