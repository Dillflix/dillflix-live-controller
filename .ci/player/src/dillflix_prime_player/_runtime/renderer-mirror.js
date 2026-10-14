/* Passive reducer for JavaScript -> native renderer submissions that returned without throwing. */
(function(bounded){
  'use strict';
  var nodes=Object.create(null),roots=[],revision=0,counts={},omitted=0,animations=0;
  function id(x){return typeof x==='number'||typeof x==='string'?String(x):null;}
  function node(key){
    if(key===null)return null;
    if(!nodes[key]){
      if(Object.keys(nodes).length>=1000){omitted++;return null;}
      nodes[key]={nodeId:key,creationObserved:false,children:[]};
    }return nodes[key];
  }
  function command(c){
    if(!c||typeof c.commandType!=='string')return false;
    var type=c.commandType;
    if(!/renderable|sceneCommandFlush|Animation/i.test(type))return false;
    counts[type]=(counts[type]||0)+1;
    if(/Animation/.test(type)){animations++;return true;}
    if(type==='sceneCommandFlush')return true;
    var key=id(c.nodeId),n=node(key);if(!n)return true;
    if(/RenderableCreate$/.test(type)){n.creationObserved=true;n.renderableType=type.replace(/RenderableCreate$/,'');}
    ['layoutData','transformation','style','attributes'].forEach(function(k){
      if(c[k]!==undefined){var value=bounded(c[k]).value;n[k]=value&&typeof value==='object'?Object.assign({},n[k]||{},value):value;}
    });
    if(type==='renderableAddRoot'&&roots.indexOf(key)<0)roots.push(key);
    if(type==='renderableAddChild'){
      var child=id(c.childId),before=id(c.beforeId);node(child);
      if(child!==null){n.children=n.children.filter(function(x){return x!==child;});var pos=n.children.indexOf(before);if(pos<0)n.children.push(child);else n.children.splice(pos,0,child);}
    }
    if(type==='renderableRemoveChild'){var child=id(c.childId);n.children=n.children.filter(function(x){return x!==child;});}
    if(type==='renderableUpdateOpacity')n.opacity=c.opacity;
    if(type==='renderableUpdateTranslationX')n.translationX=c.translationX;
    if(type==='renderableUpdateTranslationY')n.translationY=c.translationY;
    revision++;return true;
  }
  function snapshot(){
    var keys=Object.keys(nodes),missing=keys.filter(function(k){return !nodes[k].creationObserved;}),output=[],chars=0;
    keys.some(function(k){var size=JSON.stringify(nodes[k]).length;if(output.length>=600||chars+size>380000)return true;output.push(nodes[k]);chars+=size;return false;});
    return {revision:revision,roots:roots.slice(),nodes:output,nodeCount:keys.length,
      truncated:output.length<keys.length||omitted>0,omittedUpdates:omitted,creationMissing:missing,commandCounts:Object.assign({},counts),animationCommands:animations,
      provenance:'JavaScript renderer submissions that returned without throwing; not native/compositor acknowledgements. Keys are scoped to this process/context.',
      limits:['Native Rust renderables bypass this graph.','Missing creation records and truncated state remain explicit.',
        'Layout and animation commands describe requested state, not a compositor screenshot or proof of visibility.',
        'Detached/cached nodes may remain; node presence is not current focus or complete page residency.']};
  }
  return {command:command,snapshot:snapshot,revision:function(){return revision;}};
})
