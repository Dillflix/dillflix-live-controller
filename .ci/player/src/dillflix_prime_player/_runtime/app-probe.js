/* Uses only the application's existing modules and local command transport. */
(function (g, options) {
  'use strict';
  var moduleIds=options.profile.adapter.modules;
  if (g.__pvAppProbe) return g.__pvRoles&&g.__pvRoles.snapshot().restartRequired?{installed:false,reason:'runtime_restart_required'}:{installed:true};
  if(!g.__pvModules)return {installed:false,reason:'module_observer_missing'};
  var required=[moduleIds.focus,moduleIds.page,moduleIds.navigation,moduleIds.context];
  var modules={};Object.keys(moduleIds).forEach(function(role){var id=moduleIds[role];modules[id]=g.__pvModules.exports(id);});
  if(!required.every(function(id){return !!modules[id];}))return {installed:false,reason:'modules_not_initialized',modules:g.__pvModules.status()};
  // Only read exports captured after normal factory completion. Never initialize
  // a module from a nested sampling callback or disturb Webpack's module cache.
  var Focus,Page,Navigation,Context,Bridge,Manager,PluginCommand,RustPlayback;
  try{
    Focus=modules[moduleIds.focus].IK;Page=modules[moduleIds.page].Z;Navigation=modules[moduleIds.navigation].ul;
    Context=modules[moduleIds.context].v;Bridge=(modules[moduleIds.bridge]||{}).CrossAppEventChannel;
    Manager=(modules[moduleIds.manager]||{}).WasmCommunicationManager;PluginCommand=(modules[moduleIds.plugin_command]||{}).default;
    RustPlayback=modules[moduleIds.rust_playback];
  }catch(error){return {installed:false,reason:'exports_not_ready',message:String(error).slice(0,300)};}
  if(!Focus||typeof Focus.getFocusedTestID!=='function'||!Page||typeof Page.getCurrentPage!=='function'||
     !Navigation||!Context||typeof Context.getCurrentState!=='function')
    return {installed:false,reason:'export_shape_not_ready'};
  var control=null,lastBackground=0,nativeRequested=false,selectionRequested=false,sceneInspectionId=null,playerRequests=[];
  var stablePageKey=null, stablePageSince=0, lastSceneAssociation=0;
  var queue=[],queueChars=0,dropped=0,droppedByKind={},criticalDropped=0,sequence=0,lastSample=0,lastScene=Date.now(),lastPlayer=Date.now();
  var pending=null, stopped=false, listening=null, disabled={}, retryAfter={}, nativeVerified=false;
  var association=null, associationSequence=0, playerRouting=null;
  var nativeStats={started:0,completed:0,failed:0,timedOut:0,responseChars:0,totalDurationMs:0,byMethod:{}};
  var focusPlugin='app-focus-plugin', viewPlugin='app-view-inspection-plugin';
  var fields=('id key type gti titleId contentId text title titleText subtitle contentType liveliness '+
    'liveEventDateHeader metadataBadgeMessage message startTime endTime providerName label focusKey '+
    'testID componentID focused focusedIndex focusedRowIndex focusedItemIndex index section pageId '+
    'pageType screen actionName itemContentId itemContentType itemSlotId itemName itemCreativeId '+
    'eventType eventTrigger eventTimestamp inViewTime inViewStripe').split(' ');
  function call(obj,key){try{return obj&&typeof obj[key]==='function'?obj[key]():undefined;}catch(_){return undefined;}}
  function project(value,depth) {
    if(value==null||typeof value==='number'||typeof value==='boolean')return value;
    if(typeof value==='string')return value.length>4096?{omitted:'string_limit',prefix:value.slice(0,4096),totalChars:value.length}:value;
    if(typeof value!=='object')return undefined;
    if(depth<=0)return {omitted:'depth_limit'};
    if(Array.isArray(value))return {items:value.slice(0,128).map(function(v){return project(v,depth-1);}),total:value.length,truncated:value.length>128};
    var out={}; fields.forEach(function(k){try{if(value[k]!==undefined)out[k]=project(value[k],depth-1);}catch(_){}});return out;
  }
  function page(){var s=call(Context,'getCurrentState');return {pageType:call(Page,'getCurrentPage'),pageId:call(Page,'getCurrentPageId'),activeLayer:Navigation.activeLayer,screen:s&&s.appContext&&s.appContext.screen};}
  function emit(kind,data){
    var e={schema:'prime-runtime-experiment/1',sequence:++sequence,timeMs:Date.now(),kind:kind,data:data};
    var wire;
    try{wire=JSON.stringify(e);}catch(error){wire=JSON.stringify({kind:'serialization_error',message:String(error)});}
    var omitted=wire.length>options.maxRecordChars;
    if(omitted)wire=JSON.stringify({kind:kind,sequence:e.sequence,timeMs:e.timeMs,payloadOmitted:'record_size_limit',originalChars:wire.length});
    var critical=/^(?:page_tile_metadata|broadcasts_piece|broadcasts_done|catalog_search_piece|catalog_search_done|native_scene_chunk|native_query|native_selection_snapshot|native_focus_association|control_app_event|control_state|control_completion|control_action_|javascript_page_structure|javascript_renderer_snapshot|live_resolution|content_resolution|content_identity|content_metadata|search_tile_metadata|javascript_store_section|input_event|javascript_input_edge|navigation_strategy_result)/.test(kind);
    queue.push({wire:wire,kind:kind,critical:critical});queueChars+=wire.length;
    while(queue.length>256||queueChars>2200000){
      var discard=queue.findIndex(function(x){return !x.critical;});if(discard<0)discard=0;
      var lost=queue.splice(discard,1)[0];queueChars-=lost.wire.length;dropped++;droppedByKind[lost.kind]=(droppedByKind[lost.kind]||0)+1;if(lost.critical)criticalDropped++;
    }
    return {sequence:e.sequence,payloadOmitted:omitted};
  }
  function nativeEvent(envelope){
    if(g.__pvCompat&&!g.__pvCompat.allows('core'))return;
    var e=envelope&&envelope.event;if(!e)return;if(control)control.event(e);
    if(e.type==='ImpressionEvent'&&e.payload&&control&&control.impression)control.impression(e.payload);
    if(!options.lightweight&&e.type==='ImpressionEvent'&&e.payload)emit('native_impression',{pageAtReceipt:page(),entry:project(e.payload.ImpressionEntryEvent,3),item:project(e.payload.ImpressionEntryItem,3),view:project(e.payload.ImpressionEntryView,3),coverage:'Historical impression event; current focus and visibility are unverified.'});
    else if(!options.lightweight&&e.type==='AppEvent')emit('native_app_event',{pageAtReceipt:page(),name:e.name,params:project(e.params,4)});
  }
  function subscribe(){Bridge=(g.__pvModules.exports(moduleIds.bridge)||{}).CrossAppEventChannel;var b=Bridge&&Bridge.instance;if(!listening&&b&&Array.isArray(b.appHandlers)&&typeof b.subscribe==='function'){b.subscribe(nativeEvent);listening=b;}}
  function node(n){
    if(!n)return null;var h=n.hostNode,c=h&&h.customNavigableNode,o=c||h,a=o&&(o.attributes||(h&&h.props)),vm=a&&a.viewModel;
    var result={testID:call(n,'getTestID'),path:call(n,'getFocusPath'),focusKey:call(n,'getFocusKey'),focusState:project(o&&o.focusState,3),props:project(h&&h.props,3),model:project(vm,3)};
    if(vm&&options.loadedItems){var sections=Array.isArray(vm.sections)?vm.sections:[{items:vm.items}],remaining=256;
      result.loadedSections=sections.slice(0,32).map(function(s){var items=Array.isArray(s.items)?s.items:[],count=Math.min(remaining,items.length);remaining-=count;return {header:project(s.header,3),items:items.slice(0,count).map(function(i){return project(i,4);}),totalLoadedItems:items.length,truncated:count<items.length};});
      result.totalLoadedSections=sections.length;result.sectionsTruncated=sections.length>32;
    }return result;
  }
  function snapshot(){var p=page(),out={page:p,allPageItemsRead:false,playerRouting:playerRouting};
    if(p.activeLayer==='JavaScript'){out.focusedTestId=call(Focus,'getFocusedTestID');out.focusedPath=call(Focus,'getFocusedFocusablePath');out.focusable=node(call(Focus,'getFocusedFocusable'));out.navigable=node(call(Focus,'getFocusedNavigable'));out.coverage='JavaScript focus and loaded host models; visibility unverified.';}
    else out.coverage='Native layer; JavaScript focus intentionally not used.';
    return out;
  }
  function cleanup(p){var map=p.manager.requestResponseListeners;if(p.listener&&map&&map[p.id]===p.listener)delete map[p.id];}
  function samePage(a,b){return JSON.stringify(a)===JSON.stringify(b);}
  function rustPlaybackRouting(){
    RustPlayback=g.__pvModules.exports(moduleIds.rust_playback);
    try{
      if(!RustPlayback||typeof RustPlayback.g1!=='function')return {enabled:null,error:'predicate_unavailable'};
      return {enabled:!!RustPlayback.g1(),predicate:'adapter.rust_playback.g1'};
    }catch(error){return {enabled:null,error:String(error).slice(0,1000),predicate:'adapter.rust_playback.g1'};}
  }
  function finish(p,success,value){
    if(pending!==p)return;pending=null;cleanup(p);
    nativeStats.completed++;nativeStats.totalDurationMs+=Math.max(0,Date.now()-p.started);
    if(Date.now()-p.started>=options.timeoutMs)nativeStats.timedOut++;
    if(g.__pvCompat&&!g.__pvCompat.allows('native_inspection')){nativeStats.failed++;emit('native_query',{inspectionId:p.inspectionId,method:p.method,success:false,result:{error:'compatibility_changed_during_inspection'}});return;}
    if(success&&value!==null&&((p.method==='getTitleId'&&typeof value!=='string')||(p.method==='getCurrentPosition'&&(typeof value!=='number'||!isFinite(value)||value<0)))){
      if(g.__pvCompat)g.__pvCompat.schemaFailure(p.method);
      success=false;value={error:'response_schema_failed:'+p.method};
    }
    if(success&&p.method==='inspectPage'&&!p.sceneWire&&!(value&&value.payloadOmitted)&&(!Array.isArray(value)||value.length>10000||!value.every(function(n){return n&&typeof n==='object'&&Array.isArray(n.childNodeIds)&&n.boundingBox&&n.id!==undefined;}))){if(g.__pvCompat)g.__pvCompat.schemaFailure(p.method);success=false;value={error:'response_schema_failed:inspectPage'};}
    if(success&&Date.now()-p.started>=options.timeoutMs){success=false;value={error:'timeout',note:'Response arrived after the query deadline; retained as stale evidence.',lateResponse:value};}
    if(!success){nativeStats.failed++;disabled[p.key]=true;if(String(value&&value.error||'').indexOf('response_schema_failed:')!==0)retryAfter[p.key]=Date.now()+60000;}
    var nowPage=page(),changed=!samePage(p.page,nowPage);
    var transfer=null,wireResult=value;
    if(p.sceneWire&&success){
      transfer={id:String(p.id)+':'+String(sequence+1),chars:p.sceneWire.length,parts:Math.ceil(p.sceneWire.length/32768),encoding:'json-utf16',checksum:p.sceneHash};
      wireResult={chunkedScene:transfer};
    }
    var record=emit('native_query',{commandId:p.id,inspectionId:p.inspectionId,plugin:p.plugin,method:p.method,arguments:p.args,associationId:p.associationId,phase:p.phase,routingAtRequest:p.routing,success:success,requestedTimeMs:p.started,receivedTimeMs:Date.now(),pageAtRequest:p.page,pageAtReceipt:nowPage,pageChanged:changed,inputEpochAtRequest:p.inputEpoch,inputEpochAtReceipt:control?control.epoch():null,result:wireResult});
    if(transfer)for(var ci=0;ci<transfer.parts;ci++)emit('native_scene_chunk',{transferId:transfer.id,index:ci,text:p.sceneWire.slice(ci*32768,(ci+1)*32768)});
    if(p.associationId)associationResult(p,success,record.payloadOmitted?{payloadOmitted:'record_size_limit'}:value,nowPage,record.sequence);
  }
  function query(plugin,method,args,group,inspectionId){
    if(g.__pvCompat&&!g.__pvCompat.allows('native_inspection'))return false;
    var key=plugin+'/'+method;if(pending||disabled[key])return false;
    Manager=(g.__pvModules.exports(moduleIds.manager)||{}).WasmCommunicationManager;PluginCommand=(g.__pvModules.exports(moduleIds.plugin_command)||{}).default;
    var manager=Manager&&Manager.instance;
    if(!manager||typeof manager.sendCommand!=='function')return false;
    var pluginCall={pluginMethod:method};Object.keys(args||{}).forEach(function(k){pluginCall[k]=args[k];});
    var p={inspectionId:inspectionId,key:key,plugin:plugin,method:method,args:args||{},associationId:group&&group.id,phase:group&&group.phase,routing:plugin==='app-player-plugin'?playerRouting:undefined,manager:manager,started:Date.now(),page:page(),inputEpoch:control?control.epoch():null,listener:null};pending=p;
    nativeStats.started++;nativeStats.byMethod[key]=(nativeStats.byMethod[key]||0)+1;
    try{
      var command=new PluginCommand({pluginName:plugin,pluginCall:pluginCall});p.id=command.getCommandId();
      var promise=manager.sendCommand(command);p.listener=manager.requestResponseListeners&&manager.requestResponseListeners[p.id];
      if(!promise||typeof promise.then!=='function')throw new Error('No response promise');
      promise.then(function(response){
        if(pending!==p)return;
        if(!response||!response.isSuccess){finish(p,false,{error:response&&response.message||'unsuccessful_response'});return;}
        var data=response.message;
        if(typeof data==='string')nativeStats.responseChars+=data.length;
        // Transfer bounded raw UTF-16; host validates the tree after checksum verification.
        if(options.sceneChunks&&p.method==='inspectPage'&&!p.associationId&&typeof data==='string'&&data.length){
          if(data.length>1048576){finish(p,true,{payloadOmitted:'scene_transfer_limit',originalChars:data.length});return;}
          var hash=2166136261;for(var hi=0;hi<data.length;hi++){hash^=data.charCodeAt(hi);hash=Math.imul(hash,16777619)>>>0;}
          p.sceneWire=data;p.sceneHash=hash;finish(p,true,{chunked:true});return;
        }
        // Do not parse unbounded scene data into a second object graph.
        if(typeof data==='string'&&data.length>options.maxRecordChars){finish(p,true,{payloadOmitted:'response_size_limit',originalChars:data.length});return;}
        if(typeof data==='string'&&data.length){try{data=JSON.parse(data);}catch(_){data={rawMessage:data};}}
        finish(p,true,data);
      },function(error){finish(p,false,{error:String(error&&error.message||error).slice(0,2000)});});
    }catch(error){finish(p,false,{error:String(error).slice(0,2000)});}
    return true;
  }
  function describeElements(value){
    var isArray=Array.isArray(value),objects=isArray&&value.every(function(v){return v&&typeof v==='object'&&!Array.isArray(v);});
    var summary={responseType:value===null?'null':isArray?'array':typeof value,elementCount:isArray?value.length:null,structureRecognized:!!objects,elementKeys:[],textSamples:[]};
    var items=isArray?value.slice(0,8):value&&typeof value==='object'?[value]:[];
    summary.elementKeys=items.map(function(v){return v&&typeof v==='object'?Object.keys(v).slice(0,32):[];});
    var visited=0;
    function scan(v,depth){
      if(!v||typeof v!=='object'||depth<=0||++visited>256)return;
      Object.keys(v).slice(0,64).forEach(function(k){
        if((k==='text'||k==='titleText'||k==='label')&&typeof v[k]==='string'&&v[k].trim()&&summary.textSamples.length<8&&summary.textSamples.indexOf(v[k])<0)summary.textSamples.push(v[k].slice(0,4096));
        else if(typeof v[k]==='object')scan(v[k],depth-1);
      });
    }
    scan(value,5);return summary;
  }
  function completeAssociation(reason){
    var a=association;if(!a)return;association=null;
    emit('native_focus_association',{associationId:a.id,associationAccepted:!reason,geometryAssociationAccepted:!!(a.geometry&&a.geometry.accepted&&reason==='non_unique_test_id'),geometryEvidence:a.geometry,reason:reason||'matching_focus_and_page_endpoints',startedTimeMs:a.started,completedTimeMs:Date.now(),pageAtStart:a.page,pageAtEnd:page(),inputEpochAtStart:a.inputEpoch,inputEpochAtEnd:control?control.epoch():null,focusedTestIdBefore:a.before,focusedTestIdAfter:a.after,queryRecords:a.records,elements:a.elements,textStatus:a.textStatus||'not_queried',text:a.text,
      coverage:'Links only the recorded responses for this test ID. Matching focus/page endpoints do not prove continuous focus, viewport visibility, uniqueness, or a catalog/title ID.'});
  }
  function associationResult(p,success,value,nowPage,recordSequence){
    var a=association;if(!a||p.associationId!==a.id)return;
    a.records.push({phase:p.phase,sequence:recordSequence});
    if(control&&(a.inputEpoch!==control.epoch()||p.inputEpoch!==a.inputEpoch)){completeAssociation('input_during_association');return;}
    if(!samePage(a.page,p.page)||!samePage(a.page,nowPage)){completeAssociation('page_changed');return;}
    if(Date.now()-a.started>=options.timeoutMs*4){completeAssociation('association_deadline');return;}
    if(a.purpose==='selection'){
      if(!success){completeAssociation('selection_query_failed');return;}
      if(p.phase==='before'){
        if(typeof value!=='string'||!value){completeAssociation('no_selection_focus');return;}
        a.before=value;a.phase='scene';
      }else if(p.phase==='scene'){
        if(!Array.isArray(value)||recordSequence==null){completeAssociation('selection_scene_missing');return;}
        a.sceneSequence=recordSequence;a.phase='after';
      }else if(p.phase==='after'){
        emit('native_selection_snapshot',{success:value===a.before,focusTestId:a.before,focusAfter:value,sceneSequence:a.sceneSequence,
          page:a.page,inputEpoch:a.inputEpoch,queryRecords:a.records,startedTimeMs:a.started,completedTimeMs:Date.now()});association=null;
      }
      return;
    }
    if(p.phase==='before'){
      if(!success){completeAssociation('initial_focus_query_failed');return;}
      if(value===null||value===undefined||value===''){completeAssociation('no_focus');return;}
      if(typeof value!=='string'||value.length>4096){completeAssociation('unsupported_focus_response');return;}
      a.before=value;a.phase='elements';
    }else if(p.phase==='elements'){
      if(!success){a.rejection='elements_query_failed';a.phase='after';return;}
      if(value&&value.payloadOmitted){a.rejection='elements_response_omitted';a.phase='after';return;}
      a.elements=describeElements(value);
      if(!a.elements.structureRecognized)a.rejection='unrecognized_elements_structure';
      else if(!a.elements.elementCount)a.rejection='no_matching_elements';
      else if(a.elements.elementCount>1)a.rejection='non_unique_test_id';
      if(a.rejection==='non_unique_test_id'&&options.capabilities&&options.scene&&!disabled[viewPlugin+'/inspectPage']){a.candidates=value;a.phase='geometry';return;}
      // Retain unknown structures for inspection; do not guess native wrapper fields.
      // Text is supplemental: use the exact same ID only when returned nodes lack text.
      if(a.elements.structureRecognized&&a.elements.elementCount&&!a.elements.textSamples.length){a.phase='text';}
      else {a.textStatus=a.elements.textSamples.length?'present_in_elements':'not_applicable';a.phase='after';}
    }else if(p.phase==='geometry'){
      var rings=success&&Array.isArray(value)?value.filter(function(n){var b=n&&n.boundingBox;return n&&n.id==='matched-geometry-focus-ring'&&n.visible&&b&&b.width>0&&b.height>0;}):[];
      var candidates=[],sceneMatches=[];
      function sameBox(x,y){return x&&y&&['left','top','width','height'].every(function(k){return x[k]===y[k];});}
      if(rings.length===1){
        a.candidates.forEach(function(n,index){if(n.visible&&n.focusable&&sameBox(n.boundingBox,rings[0].boundingBox))candidates.push(index);});
        sceneMatches=value.filter(function(n){return n.id===a.before&&n.visible&&n.focusable&&sameBox(n.boundingBox,rings[0].boundingBox);});
      }
      a.geometry={accepted:rings.length===1&&candidates.length===1&&sceneMatches.length===1,authoritative:false,kind:'matched_native_focus_ring_geometry',sceneSequence:recordSequence,
        candidateIndex:candidates.length===1?candidates[0]:null,visibleRingCount:rings.length,sceneMatchCount:sceneMatches.length,
        note:'Geometry inference bracketed by matching focus test IDs and page/input epochs; no screenshot or native node handle is used.'};
      lastScene=Date.now();lastSceneAssociation=associationSequence;a.phase='after';
    }else if(p.phase==='text'){
      a.textStatus=success&&typeof value==='string'&&value.trim()?'available':success?'empty_or_unrecognized':'query_failed';
      if(a.textStatus==='available')a.text=value.slice(0,4096);
      a.phase='after';
    }else if(p.phase==='after'){
      if(!success){completeAssociation('final_focus_query_failed');return;}
      a.after=typeof value==='string'&&value.length<=4096?value:null;
      completeAssociation(a.after!==a.before?'focus_changed':a.rejection);
    }
  }
  function advanceAssociation(){
    var a=association;if(!a||pending)return;
    if(!nativeVerified){completeAssociation('native_runtime_unverified');return;}
    if(!samePage(a.page,page())){completeAssociation('page_changed');return;}
    if(Date.now()-a.started>=options.timeoutMs*4){completeAssociation('association_deadline');return;}
    var phase=a.phase,method=(phase==='geometry'||phase==='scene')?'inspectPage':phase==='elements'?'getElementsByTestId':phase==='text'?'getTextByTestId':'getFocusedNodeTestId';
    var plugin=phase==='scene'||phase==='geometry'||phase==='elements'||phase==='text'?viewPlugin:focusPlugin;
    if(disabled[plugin+'/'+method]){
      if(phase==='text'){a.textStatus='method_disabled';a.phase='after';advanceAssociation();}
      else completeAssociation(phase+'_method_disabled');
      return;
    }
    if(!query(plugin,method,phase==='elements'||phase==='text'?{testId:a.before}:{},a))completeAssociation('transport_unavailable');
  }
  function tick(settings){
    nativeVerified=!!(settings&&settings.nativeVerified);
    if(stopped)return drain();if(g.__pvCompat&&!g.__pvCompat.allows('core'))return drain();subscribe();if(control)control.tick();var now=Date.now();
    Object.keys(retryAfter).forEach(function(key){if(now>=retryAfter[key]){delete retryAfter[key];delete disabled[key];emit('native_retry_ready',{method:key});}});
    var currentPage=page(),pageKey=JSON.stringify(currentPage);
    if(pageKey!==stablePageKey){stablePageKey=pageKey;stablePageSince=now;}
    var queryReady=!options.settleMs||(currentPage.pageType&&currentPage.pageType!=='UNLOADED'&&now-stablePageSince>=options.settleMs);
    if(pending&&now-pending.started>=options.timeoutMs)finish(pending,false,{error:'timeout',note:'This method can be retried after a 60-second cooldown.'});
    if(association&&(!nativeVerified||!samePage(association.page,page())))completeAssociation(!nativeVerified?'native_runtime_unverified':'page_changed');
    advanceAssociation();
    if(!options.lightweight&&now-lastSample>=options.intervalMs){lastSample=now;
      playerRouting=options.mode==='test-utils'&&options.player&&nativeVerified?rustPlaybackRouting():null;
      emit('snapshot',snapshot());
      if(options.mode==='test-utils'&&nativeVerified&&queryReady&&!pending&&!association){
        if(options.scene&&associationSequence>lastSceneAssociation&&page().activeLayer==='Wasm'&&now-lastScene>=5000&&!disabled[viewPlugin+'/inspectPage']){lastScene=now;lastSceneAssociation=associationSequence;query(viewPlugin,'inspectPage');}
        else if(options.player&&playerRouting&&playerRouting.enabled===true&&now-lastPlayer>=5000&&!disabled['app-player-plugin/getTitleId']){lastPlayer=now;query('app-player-plugin','getTitleId');}
        else if(page().activeLayer==='Wasm'&&!disabled[focusPlugin+'/getFocusedNodeTestId']&&!disabled[viewPlugin+'/getElementsByTestId']){
          association={id:++associationSequence,phase:'before',started:now,page:page(),inputEpoch:control?control.epoch():null,records:[]};advanceAssociation();
        }
      }
    }
    if(options.lightweight&&playerRequests.length&&nativeVerified&&queryReady&&!pending&&!association){
      var pr=playerRequests.shift();
      if(samePage(pr.page,page())&&rustPlaybackRouting().enabled===true){playerRouting=rustPlaybackRouting();query('app-player-plugin',pr.method,{},null,pr.inspectionId);}
    }
    if(options.lightweight&&nativeRequested&&nativeVerified&&queryReady&&!pending&&!association){
      nativeRequested=false;
      if(selectionRequested){selectionRequested=false;association={id:++associationSequence,purpose:'selection',phase:'before',started:now,page:page(),inputEpoch:control?control.epoch():null,records:[]};advanceAssociation();}
      else query(viewPlugin,'inspectPage',{},null,sceneInspectionId);
    }
    return settings&&settings.retain?{queued:queue.length}:drain();
  }
  function background(){
    if(stopped||Date.now()-lastBackground<500)return;
    lastBackground=Date.now();tick({nativeVerified:nativeVerified,retain:true});
  }
  function requestNative(selection,inspectionId){
    if(g.__pvCompat&&!g.__pvCompat.allows('native_inspection'))throw new Error('compatibility:native_inspection_unavailable');
    if(options.mode!=='test-utils'||!nativeVerified)throw new Error('native test runtime not enabled/verified; no query sent');
    if(nativeRequested||pending||association||playerRequests.length)throw new Error('a native inspection is already pending');
    if(selection==='player'){
      if(page().pageType!=='RUST_PLAYBACK'||rustPlaybackRouting().enabled!==true)throw new Error('verified Rust playback routing required');
      if(disabled['app-player-plugin/getTitleId']||disabled['app-player-plugin/getCurrentPosition'])throw new Error('player inspection disabled after a prior query failure');
      playerRequests=['getTitleId','getCurrentPosition'].map(function(method){return {method:method,page:page(),inspectionId:inspectionId};});
      return {snapshotRequested:true,method:'playerIdentityAndPosition'};
    }
    sceneInspectionId=inspectionId;selectionRequested=!!selection;nativeRequested=true;return {snapshotRequested:true,method:selection?'selectionSnapshot':'inspectPage'};
  }
  function drain(){var batch=[],chars=0;while(queue.length&&batch.length<128){var next=queue[0];if(batch.length&&chars+next.wire.length>550000)break;chars+=next.wire.length;queueChars-=next.wire.length;batch.push(queue.shift().wire);}return {installed:true,records:batch.map(function(s){return JSON.parse(s);}),completions:control&&control.takeCompletions?control.takeCompletions():[],queued:queue.length,dropped:dropped,droppedByKind:droppedByKind,criticalDropped:criticalDropped,nativeVerified:nativeVerified,disabledMethods:Object.keys(disabled),retryAfterMs:retryAfter};}
  function stop(){stopped=true;if(control)control.stop();completeAssociation('probe_stopped');if(pending){cleanup(pending);pending=null;}if(listening){var i=listening.appHandlers.indexOf(nativeEvent);if(i>=0)listening.appHandlers.splice(i,1);listening=null;}return drain();}
  function memory(){return {queuedRecords:queue.length,queuedChars:queueChars,droppedRecords:dropped,
    pendingNativeQueries:pending?1:0,queuedPlayerQueries:playerRequests.length,
    nativeQueries:nativeStats,modules:g.__pvModules&&g.__pvModules.memory?g.__pvModules.memory():null};}
  if(options.control)control=(__PV_CONTROL_FACTORY__)(g,options,{Focus:Focus,Page:Page,Navigation:Navigation,Manager:Manager,Bridge:Bridge,emit:emit,page:page,onBoundary:background,requestNative:requestNative});
  g.__pvAppProbe={memory:memory,fence:function(epoch){nativeRequested=false;selectionRequested=false;playerRequests=[];if(!control)throw new Error("control unavailable");return control.fence(epoch);},tick:tick,drain:drain,stop:stop,health:function(){return control?control.health():null;},transport:function(settings){nativeVerified=!!(settings&&settings.nativeVerified);return {batch:drain(),health:control?control.health():null};},command:function(c){if(!control)throw new Error('control disabled');return control.command(c);}};
  emit('probe_ready',{mode:options.mode,probeVersion:'control-inspection-2',queryTimeoutMs:options.timeoutMs,settleMs:options.settleMs||0,coverage:'Native queries require the matching test runtime to be observed in memory. Player routing uses adapter.rust_playback.g1; focus/view routing requires the Wasm layer.'});
  return {installed:true};
})(globalThis, __PV_OPTIONS__);

