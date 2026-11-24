'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
class Element {
 constructor(){this.children=[];this.dataset={};this.textContent='';this.hidden=false;this.value='all';this.listeners={};this.classList={add(){},remove(){}};}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=nodes;}
 addEventListener(name,fn){this.listeners[name]=fn;}
}
const elements=new Map(),documentListeners={},windowListeners={},requests=[];
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
const payload=(cursor=3,alert=null)=>({stream:'process-a',cursor,gap:false,items:[{cursor:1,at:1700000000,kind:'started',data:{}},{cursor:2,at:1700000001,kind:'event',data:{event:'ButtonPressed',button:7}},{cursor:3,at:1700000002,kind:'alert_opened',data:{kind:'help_requested'}}],health:{runtime:'READY',handset:'up',audio:'available',hid:'active',lighting:'available',lit_channels:2},mode:'safety_stop',alert});
let response=payload(3,{kind:'help_requested'}),status=200;
const context=vm.createContext({document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener:(k,v)=>documentListeners[k]=v},window:{addEventListener:(k,v)=>windowListeners[k]=v},Date,URLSearchParams,AbortController,setTimeout:()=>1,clearTimeout(){},fetch:async(url,options)=>{requests.push({url,options});return {ok:status===200,status,json:async()=>response};},WebSocket:class{constructor(){throw Error('operator must never own a websocket');}}});
vm.runInContext(fs.readFileSync('app/foundation/console/operator.js','utf8'),context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const text=node=>node.textContent+' '+node.children.map(text).join(' ');
(async()=>{
 await flush();
 assert.strictEqual(get('operator-alert').dataset.active,'true');
 assert(text(get('operator-timeline')).includes('Нажата кнопка 7'));
 assert(text(get('operator-timeline')).includes('Доставка и помощь не подтверждены'));
 assert.strictEqual(get('operator-connection').textContent,'На связи');
 assert(requests.every(r=>r.url.startsWith('/stand/operator?')&&!r.options.method));
 assert(requests.every(r=>r.options.credentials==='same-origin'&&r.options.cache==='no-store'));
 get('operator-filter').value='alerts';get('operator-filter').listeners.change();
 assert(!text(get('operator-timeline')).includes('Нажата кнопка'));
 assert(text(get('operator-timeline')).includes('Запрошена помощь'));
 status=401;await get('operator-refresh').listeners.click();await flush();
 assert.strictEqual(get('operator-alert').dataset.stale,'true');
 assert.strictEqual(get('operator-alert').dataset.active,'true','disconnect must not claim resolution');
 assert(text(get('operator-error')).includes('Нет доступа'));
 status=200;response=payload(4,null);response.items=[{cursor:4,at:1700000003,kind:'alert_ended',data:{kind:'help_requested',adult_acknowledged:false}}];
 await get('operator-refresh').listeners.click();await flush();
 assert.strictEqual(get('operator-alert').dataset.active,'false');
 assert.strictEqual(get('operator-alert').dataset.stale,'false');
 assert(text(get('operator-timeline')).includes('Подтверждения помощи взрослого нет'));
 assert(requests.at(-1).url.includes('after=3'));
 response={...payload(1,null),stream:'process-b',gap:true,items:[{cursor:1,at:1700000004,kind:'started',data:{}}]};
 await get('operator-refresh').listeners.click();await flush();
 assert.strictEqual(get('operator-gap').hidden,false);
 assert(!text(get('operator-timeline')).includes('Запрошена помощь'));
 windowListeners.pagehide();windowListeners.pageshow();await flush();
 assert.strictEqual(get('operator-connection').textContent,'На связи','return from browser page cache resumes polling');
 assert.strictEqual(requests.length,5);
 console.log('operator console: private read-only polling, filters, offline stale alert, resolution, restart gap and page-cache return passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
