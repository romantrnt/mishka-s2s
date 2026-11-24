'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app/foundation/console/phone.js','utf8');
const functions=source.slice(source.indexOf('  function flatView('),source.indexOf('  window.mishkaPhoneUpdate='))
 .replace("import('./phone-model.js?v=3-fill-channel-fix')",'__importPhone()');
const setup=()=>{
 const fields={'phone-render-notice':{textContent:''}}, key={style:{left:'91%',top:'14%',width:'15px',height:'20px',removeProperty(property){delete this[property];}}};
 const releases=[];
 const context=vm.createContext({rendererGeneration:0,stopped:false,state:null,simulator:{},model:null,
  stage:{dataset:{renderer:'three'},clientWidth:600},keys:new Map([[1,key]]),$:id=>fields[id],
  input:{pressed:()=>new Set(),press(){},release:(...args)=>releases.push(args)},handset(){},renderMechanical(){},
 });
 vm.runInContext(functions,context);
 return {context,key,fields,releases};
};
(async()=>{
 for(const failure of ['import','create','context']){
  const {context,key,fields,releases}=setup();let handlers,destroyed=0;
  context.__importPhone=async()=>{
   if(failure==='import')throw Error('import blocked');
   return {createPhoneModel:(stage,callbacks)=>{
    if(failure==='create')throw Error('WebGL unavailable');
    handlers=callbacks;
    callbacks.project(1,.9,.3,.1,1);
    return {update(){},destroy(){destroyed++;context.input.release(1,true,'model');}};
   }};
  };
  await context.threeView();
  if(failure==='context'){
   assert.strictEqual(context.stage.dataset.renderer,'three');
   handlers.onFailure('3D недоступен: включено плоское управление.');
   assert.strictEqual(destroyed,1);
   assert.deepStrictEqual(releases,[[1,true,'model']],'model cleanup retains its own input owner');
  }
  assert.strictEqual(context.stage.dataset.renderer,'flat',failure+' restores visible native keys');
  assert.strictEqual(context.model,null);
  for(const property of ['left','top','width','height'])assert.strictEqual(key.style[property],undefined,'remove stale projected '+property);
  assert.match(fields['phone-render-notice'].textContent,/плоское управление/);
  if(handlers){
   context.stage.dataset.renderer='three';
   handlers.onFailure('stale callback');
   assert.strictEqual(context.stage.dataset.renderer,'three','old renderer cannot tear down a replacement');
   assert.strictEqual(destroyed,1);
  }
 }
 console.log('renderer import/create/context failures restore flat keys, remove projected geometry and preserve model input ownership');
})().catch(error=>{console.error(error);process.exitCode=1;});
