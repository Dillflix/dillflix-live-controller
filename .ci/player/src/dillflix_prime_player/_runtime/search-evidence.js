/* Search candidates are evidence, not a promise of complete/visible results. */
(function(g, options, api) {
  'use strict';
  var moduleIds=options.profile.adapter.modules;
  var current=null, serial=0;
  function isSearch(p){return !!p && /^(RUST_SEARCH|RUST_AI_SEARCH|FIND_PAGE|SEARCH_RESULTS_PAGE)$/.test(p.pageType||'');}
  function text(v){return typeof v==='string'?v.slice(0,1000):null;}
  function idKind(v){return typeof v!=='string'?null:/^amzn1\.dv\.gti\.[A-Za-z0-9-]+$/.test(v)?'gti':/^amzn1\.dv\.icid\.[A-Za-z0-9-]+$/.test(v)?'icid':/^[A-Z0-9]{10}$/.test(v)?'asin':null;}
  function own(o,k){var d=o&&Object.getOwnPropertyDescriptor(o,k);return d&&Object.prototype.hasOwnProperty.call(d,'value')?d.value:undefined;}
  function normalized(q){return typeof q==='string'?q.trim().replace(/\s+/g,' ').toLowerCase():null;}
  function emit(k,d){api.emit(k,d);}
  function begin(query,commandId){
    if(typeof query!=='string'||!query.trim()||query.length>512)throw new Error('query must contain 1..512 characters');
    current={searchId:commandId,query:query,startedAtMs:Date.now(),expiresAtMs:Date.now()+120000,generation:0,
      loaded:false,loadStartedAtMs:null,loadedAtMs:null,items:{},discarded:{},impressionsReceived:0,impressionsAccepted:0,rejectionSamples:[],revision:0};
    emit('search_watch_started',{searchId:commandId,query:query,expiresAtMs:current.expiresAtMs});
    return {searchId:commandId,query:query,watchSeconds:120};
  }
  function discard(reason,eventTimeMs){if(current){current.discarded[reason]=(current.discarded[reason]||0)+1;if(current.rejectionSamples.length<20)current.rejectionSamples.push({reason:reason,eventTimeMs:typeof eventTimeMs==='number'?eventTimeMs:null,receivedTimeMs:Date.now(),loadedAtMs:current.loadedAtMs,generation:current.generation});}}
  function active(){return !!current&&Date.now()<=current.expiresAtMs;}
  function event(e){
    if(!active()||!e||e.type!=='AppEvent')return;
    var p=e.params||{},q=p.findResultsSearchTerm,t=p.timestamp,name=e.name;
    if(name==='NAVIGATE_TO_PAGE'&&current.loaded&&p.pageType&&!isSearch({pageType:p.pageType})){
      current.loaded=false;current.items={};current.revision++;emit('search_watch_invalidated',{searchId:current.searchId,reason:'page_departure'});return;
    }
    if(name!=='FIND_PAGE_RESULTS_LOAD_BEGIN'&&name!=='FIND_PAGE_RESULTS_LOAD_SUCCESS')return;
    if(typeof t!=='number'||!isFinite(t)||t<current.startedAtMs||t>Date.now()+1000){discard('stale_or_missing_query_timestamp');return;}
    if(!isSearch(api.page())){discard('query_event_outside_search');return;}
    if(name==='FIND_PAGE_RESULTS_LOAD_BEGIN'){
      current.generation++;current.loaded=false;current.items={};current.revision++;
      current.loadStartedAtMs=normalized(q)===normalized(current.query)?t:null;
    }
    if(normalized(q)!==normalized(current.query)){current.loaded=false;discard('different_query');return;}
    if(name==='FIND_PAGE_RESULTS_LOAD_SUCCESS'){
      if(current.loadStartedAtMs===null||t<current.loadStartedAtMs){discard('success_without_matching_begin');return;}
      current.loaded=true;current.loadedAtMs=t;
    }
    emit('search_query_evidence',{searchId:current.searchId,query:current.query,reportedQuery:q,comparison:'trim_collapse_whitespace_lowercase',generation:current.generation,phase:name,eventTimeMs:t,receivedTimeMs:Date.now(),loaded:current.loaded});
  }
  function add(identifier,info){
    if(!active()||!current.loaded||!isSearch(api.page()))return;
    var kind=idKind(identifier);if(!kind){discard('unsupported_identifier');return;}
    var item=current.items[identifier];
    if(!item){
      if(Object.keys(current.items).length>=256){discard('candidate_limit');return;}
      item=current.items[identifier]={candidateId:++serial,titleIdentifier:identifier,identifierKind:kind,labels:[],sources:[],firstReceivedTimeMs:Date.now()};
    }
    if(info.label&&item.labels.length<4&&item.labels.indexOf(info.label)<0)item.labels.push(info.label);
    item.lastReceivedTimeMs=Date.now();
    if(item.sources.length<12)item.sources.push(info);else discard('source_limit');
    current.revision++;
    emit('search_candidate',{searchId:current.searchId,query:current.query,generation:current.generation,candidate:item,
      membershipVerified:false,note:'Query/time-scoped candidate. Resolve identifiers fresh; do not infer current focus or parse an ID out of artwork.'});
  }
  function impression(payload){
    if(!current)return;
    current.impressionsReceived++;
    if(!active()){discard('impression_watch_expired');return;}
    if(!current.loaded){discard('impression_before_loaded');return;}
    if(!isSearch(api.page())){discard('impression_outside_search');return;}
    var e=payload&&payload.ImpressionEntryEvent,i=payload&&payload.ImpressionEntryItem,t=e&&e.eventTimestamp;
    if(typeof t!=='number'||!isFinite(t)||t<current.loadedAtMs||t>Date.now()+1000){discard('stale_or_missing_impression_timestamp',t);return;}
    if(!i||!e||['View','Highlight','Select'].indexOf(e.eventType)<0){discard('unsupported_impression');return;}
    current.impressionsAccepted++;
    if(options.metadataDiscovery&&api.bounded)emit('search_tile_metadata',{searchId:current.searchId,query:current.query,generation:current.generation,titleIdentifier:i.itemContentId,source:'native_impression',eventTimeMs:t,receivedTimeMs:Date.now(),page:api.page(),metadata:api.bounded(payload),queryMembershipVerified:false});
    add(i.itemContentId,{kind:'native_impression',association:'query_time_window_only',eventType:e.eventType,eventTimeMs:t,
      receivedTimeMs:Date.now(),slot:text(i.itemSlotId),widgetType:text(i.itemName),artworkUrl:text(i.itemCreativeId),
      focusVerified:false,queryMembershipVerified:false});
  }
  function inspectModels(){
    if(!active()||!current.loaded||!isSearch(api.page()))return;
    var m=g.__pvModules.exports(moduleIds.store),raw=m&&m.default&&typeof m.default.getState==='function'?m.default.getState():null;
    var seen=[],budget=2500;
    function walk(o,path,depth){
      if(!o||typeof o!=='object'||depth<0||budget--<=0||seen.indexOf(o)>=0)return;
      seen.push(o);
      var label=text(own(o,'titleText'))||text(own(o,'title'))||text(own(o,'label'));
      ['gti','titleId','contentId','itemContentId','id'].forEach(function(k){var v=own(o,k);if(idKind(v)){if(options.metadataDiscovery&&api.bounded)emit('search_tile_metadata',{searchId:current.searchId,query:current.query,titleIdentifier:v,source:'javascript_search_store',path:path,metadata:api.bounded(o),queryMembershipVerified:false});add(v,{kind:'javascript_search_store',association:'own_fields_in_loaded_search_root',path:path+'.'+k,label:label,queryMembershipVerified:false});}});
      Object.keys(o).slice(0,128).forEach(function(k){if(!/auth|token|cookie|password|customer|profile|device|envelope|correlation/i.test(k))walk(own(o,k),path+'.'+k,depth-1);});seen.pop();
    }
    if(raw&&typeof raw==='object')Object.keys(raw).filter(function(k){return /search|find/i.test(k);}).forEach(function(k){walk(own(raw,k),'store.'+k,7);});
    if(budget<=0)discard('model_budget');
  }
  function snapshot(scan){
    if(scan)inspectModels();
    var s=current;
    var out=s?{searchId:s.searchId,query:s.query,generation:s.generation,loaded:s.loaded&&isSearch(api.page()),expired:!active(),
      startedAtMs:s.startedAtMs,loadStartedAtMs:s.loadStartedAtMs,loadedAtMs:s.loadedAtMs,revision:s.revision,impressionsReceived:s.impressionsReceived,impressionsAccepted:s.impressionsAccepted,rejectionSamples:s.rejectionSamples,
      candidates:Object.keys(s.items).map(function(k){var i=s.items[k];return {candidateId:i.candidateId,titleIdentifier:i.titleIdentifier,identifierKind:i.identifierKind,labels:i.labels,sourceCount:i.sources.length,latestEvidence:i.sources[i.sources.length-1],firstReceivedTimeMs:i.firstReceivedTimeMs,lastReceivedTimeMs:i.lastReceivedTimeMs};}),discarded:s.discarded}: {loaded:false,candidates:[],reason:'no_search_watch'};
    out.completeResultSet=false;out.selectionVerified=false;
    out.limits=['Impressions can be delayed; timestamps exclude earlier pages but do not prove query membership.',
      'GTI, ICID and ASIN identifiers are kept distinct; artwork IDs are never substituted.',
      'Titles may be absent. Choose the intended event explicitly before play-live; every launch resolves fresh.'];
    if(scan)emit('search_results_snapshot',out);return out;
  }
  return {begin:begin,event:event,impression:impression,snapshot:snapshot,active:active,idKind:idKind};
})

