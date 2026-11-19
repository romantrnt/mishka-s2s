// No scenario state lives here. The adapter sends physical gestures and renders
// the canonical simulator projection; media remains owned by console.js.
class DeviceInput {
  constructor({send, snapshot, enabled, change, error, clock = () => performance.now()}) {
    Object.assign(this,{send,snapshot,enabled,change,error,clock});
    this.active=new Map();
  }
  press(id,owner=null) {
    if(this.active.has(id)||!this.enabled(id))return false;
    const record={at:this.clock(),epoch:this.snapshot()?.epoch,owner};
    this.active.set(id,record);this.change();
    this.send({action:'button',button_id:id,press:'press',epoch:record.epoch}).catch(error=>{
      this.error(error);if(this.active.get(id)===record)this.release(id,true);
    });
    return true;
  }
  release(id,cancelled=false,owner=null) {
    const record=this.active.get(id);if(!record||(owner!==null&&record.owner!==owner))return Promise.resolve();
    this.active.delete(id);this.change();
    const duration=Math.min(120000,Math.max(0,Math.round(this.clock()-record.at)));
    const send=press=>this.send({action:'button',button_id:id,press,epoch:record.epoch,...(press==='hold'||press==='long_hold'?{duration_ms:duration}:{})});
    return (async()=>{
      try {if(!cancelled&&id!==7&&duration>=600)await send(duration>=5000?'long_hold':'hold');}
      catch(error){this.error(error);}
      finally {await this.send({action:'button',button_id:id,press:'release',epoch:record.epoch,...(cancelled?{cancelled:true}:{})}).catch(this.error);}
    })();
  }
  cancel() {for(const id of [...this.active.keys()])this.release(id,true);}
  pressed() {return new Set(this.active.keys());}
}

function mountPhone() {
  const $=id=>document.getElementById(id);
  const bridge=window.MishkaConsole;
  const stage=$('phone-stage'),tools=$('phone-tools');
  const keys=new Map([...document.querySelectorAll('[data-phone-key]')].map(element=>[Number(element.dataset.phoneKey),element]));
  const pointers=new Map();
  const channels={1:[14,15],2:[13,12],3:[10,11],4:[9,8],5:[6,7],6:[3,2,4,5],7:[1,0,17,16]};
  let state=null,simulator=null,model=null,rendererGeneration=0,handsetPending=null,handsetGeneration=0;
  let journalCursor=0,journalInFlight=false,journalItems=[],mediaTimer=null,stopped=false,lastEpoch=null;
  let microphoneError='',drawerReturn=null;
  let sessionPending=null;
  const sessionButtons={start:$('phone-start'),end:$('phone-end'),skip_interview:$('phone-skip-interview')};
  const sessionScope=action=>({action,instance_id:simulator?.instance_id,epoch:simulator?.epoch,session_id:simulator?.session?.session_id,
    ...(action==='skip_interview'?{state_version:simulator?.session?.state_version}:{})});
  const sameSessionScope=body=>body&&JSON.stringify(body)===JSON.stringify(sessionScope(body.action));
  const connected=()=>Boolean(bridge.media().connected&&!bridge.media().authBlocked);
  const canMutate=()=>Boolean(connected()&&bridge.media().inputReady&&simulator?.admission?.input_ready&&simulator?.admission?.connected);
  const showError=error=>{$('phone-error').textContent=String(error?.message||error);$('phone-error').hidden=false;};
  const send=async (body,path='/stand/simulator/action')=>{
    if(bridge.media().authBlocked)throw new Error('Доступ отклонён. Обновите страницу и войдите снова.');
    const value=await bridge.post(path,{...(path.endsWith('/action')?{epoch:simulator?.epoch}:{}),...body});
    if(!value)return null;
    if(value.state?.simulator && bridge.acceptSnapshot(value.state))bridge.render(value.state);
    if(value.result?.error)throw new Error(value.result.error.message||value.result.error.code);
    $('phone-error').hidden=true;
    return value;
  };
  const input=new DeviceInput({send,snapshot:()=>simulator,enabled:id=>canMutate()&&Boolean(simulator.devices.buttons.find(key=>key.id===id)?.enabled),change:()=>renderMechanical(),error:showError});
  function renderMechanical() {
    const pressed=input.pressed();
    model?.update(simulator,pressed);
    for(const [id,key] of keys) {
      const canonical=simulator?.devices?.buttons?.find(item=>item.id===id);
      const down=pressed.has(id)||Boolean(canonical?.pressed);
      key.dataset.pressed=String(down);key.setAttribute('aria-pressed',String(down));
      key.disabled=!canMutate()||!canonical?.enabled;
      key.setAttribute('aria-label',`Кнопка ${id}${id===6?', Мишка':id===7?', погода':''}, ${canonical?.color||'цвет неизвестен'}${key.disabled?', недоступна':''}`);
      key.dataset.fill=String(simulator?.devices?.lighting?.[channels[id][0]]===1);
      key.dataset.digit=String(simulator?.devices?.lighting?.[channels[id][1]]===1);
      key.dataset.symbol=String(simulator?.devices?.lighting?.[channels[id][2]]===1);
    }
  }
  function cleanup({preserveMedia=false,...options}={}) {handsetGeneration++;handsetPending=null;sessionPending=null;input.cancel();pointers.clear();if(!preserveMedia)bridge.cleanup(options);}
  async function sessionAction(action,body) {
    if(!sameSessionScope(body)||!bridge.sessionActionIsCurrent(body))return;
    const pendingStart=sessionPending?.action==='start'?sessionPending:null;
    if(action==='end') {
      cleanup();
      if(pendingStart&&!pendingStart.submitted){render();return;}
    } else if(sessionPending || !bridge.sessionActionIsCurrent(body))return;
    const ticket={action,submitted:false};sessionPending=ticket;render();
    try {
      if(action==='start')await bridge.unlockAudio();
      if(sessionPending!==ticket||!sameSessionScope(body)||!bridge.sessionActionIsCurrent(body))return;
      ticket.submitted=true;
      await send(body);
    } catch(error){if(sessionPending===ticket)showError(error);}
    finally{if(sessionPending===ticket){sessionPending=null;render();}}
  }
  async function handset() {
    if(handsetPending==='down' || !simulator)return;
    const down=handsetPending==='up'||simulator.devices.handset.state==='up';
    if(!down&&!canMutate())return;
    if(down)cleanup();
    const generation=++handsetGeneration;
    handsetPending=down?'down':'up';render();
    try {if(!down)await bridge.unlockAudio();if(generation!==handsetGeneration)return;await send({action:'handset',state:down?'down':'up'});}
    catch(error){if(generation===handsetGeneration)showError(error);}
    finally {if(generation===handsetGeneration){handsetPending=null;render();}}
  }
  async function reset() {cleanup();try{await send({action:'reset'});}catch(error){showError(error);}}
  function renderInstruction(media=bridge.media()) {
    const live=connected(),up=bridge.interactionActive(state);
    const hint=bridge.interactionGuide(state,media);
    const tutorial=['exhibition_menu_tutorial_intro','exhibition_menu_tutorial_prompt','exhibition_menu_tutorial_buttons','exhibition_menu_wave','exhibition_menu_repeat_prompt','exhibition_menu_repeat_listen','exhibition_menu_replay_buttons'].includes(simulator?.session?.node);
    const skipHint='Нажмите любую кнопку, чтобы пропустить подсказку. Затем нажмите нужную кнопку для выбора.';
    $('phone-instruction').textContent=media.reloadRequired?'Сервис перезапущен. Обновите страницу, чтобы продолжить.':!simulator?'Подключение к телефону…':!live?'Соединение с телефоном потеряно':!media.inputReady?'Дождитесь актуального состояния телефона.':!simulator.admission?.input_ready?'Телефон пока недоступен':!simulator.admission.connected?'Устройство ввода отключено':state?.session.node==='session_closed'&&hint?hint[1]:simulator.prompt?.text || hint?.[1] || (tutorial?(simulator.session.node==='exhibition_menu_repeat_listen'?'Скажите «да» или «нет». ':'')+skipHint:up?'Выберите кнопку на лапе':'Поднимите трубку');
  }
  function renderMedia() {
    const media=bridge.media();
    const live=media.microphone;
    const label=live?(media.microphoneAccepting?'● Микрофон включён':'Микрофон включён · приём на паузе'):media.pending?'Разрешение микрофона…':'Микрофон выключен';
    $('phone-mic-indicator').textContent=label;$('phone-mic-indicator').dataset.live=String(live);
    const button=$('phone-microphone');button.textContent=live?'Выключить микрофон':media.pending?'Отменить включение':'Включить микрофон';
    button.disabled=!(live||media.pending||canMutate()&&simulator?.microphone?.eligible);
    $('phone-media-status').textContent=microphoneError|| (media.audioRetry?'Мишка не расслышал. Попробуй ещё раз. ':'')+(live?(media.microphoneAccepting?'● Мишка слушает через микрофон браузера':'Микрофон включён · Мишка пока не принимает речь'):media.pending?'Разрешите микрофон в запросе браузера':simulator?.microphone?.eligible?'Мишка готов слушать · микрофон выключен':media.playing?'Мишка говорит · микрофон выключен':'Микрофон выключен');
    $('phone-media-status').dataset.live=String(live);
    $('phone-sound').textContent=media.audioReady?'Звук включён':'Включить звук';
    $('phone-sound').setAttribute('aria-pressed',String(media.audioReady));
    renderInstruction(media);
    if(state)bridge.updateGuide(state);
  }
  function diagnostics() {
    const media=bridge.media();
    const pipeline=state?.pipeline_metrics?.pipeline||{};
    const pairs=[['Браузер',connected()?'WebSocket подключён':media.authBlocked?'Доступ отклонён':'Соединение потеряно'],['Устройство ввода',simulator?.fault?.input_connected?'Подключено':'Отключено'],['Сеанс',simulator?.session?.state||'Нет данных'],['Сценарий',simulator?.session?.node||'—'],['Профиль',simulator?.profile||'—'],['Идентификация',simulator?.identity_mode||'—'],['Аудиовыход','Web Audio · динамик браузера'],['Маршрут голоса',pipeline.voice_conversion||'Нет данных'],['Резервный маршрут',$('fallback').classList.contains('hidden')?'Нет активного предупреждения':$('fallback').textContent],['Очередь браузера',`${media.queuedPosts} команд · ${media.pendingPCM} PCM`],['Очередь устройства',`${simulator?.fault?.pending_inputs??'—'} / ${simulator?.fault?.max_pending_inputs??'—'}`],['Задержка',`${simulator?.fault?.delay_ms??0} мс`],['Epoch',String(simulator?.epoch??'—')],['Реальное оборудование','Не проверено · виртуальное устройство']];
    $('phone-diagnostics').replaceChildren(...pairs.flatMap(([label,value])=>{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;return[dt,dd];}));
  }
  function render() {
    const live=connected(),up=simulator?.devices?.handset?.state==='up';
    const media=bridge.media();
    const admitted=simulator?.admission?.input_ready;
    $('phone-connection').dataset.live=String(live&&admitted&&media.inputReady);
    $('phone-connection').textContent=media.authBlocked?'Доступ отклонён · обновите страницу':media.reloadRequired?'Сервис перезапущен · обновите страницу':!live?'Соединение потеряно · переподключение…':!media.inputReady||!simulator?'Ожидание актуального состояния…':!admitted?'Сценарий ещё не готов':!simulator.admission.connected?'Виртуальный ввод отключён':'Телефон подключён';
    $('phone-context').textContent=simulator?.devices?.rfid?.synthetic?'Симулятор устройства · синтетическое RFID · звук через браузер':'Симулятор устройства · звук через браузер';
    const handsetLabel=up?'Положить трубку':'Поднять трубку';
    $('phone-handset').textContent=handsetPending==='up'?'Положить трубку':handsetPending==='down'?'Ожидание телефона…':handsetLabel;
    $('phone-handset').disabled=handsetPending==='down'||!simulator||(!up&&handsetPending!=='up'&&!canMutate());
    const actions=Array.isArray(simulator?.session?.session_actions)?simulator.session.session_actions:[];
    for(const [action,button] of Object.entries(sessionButtons))if(button){
      const pending=sessionPending?.action===action;
      const available=actions.includes(action)||(action==='end'&&['start','end'].includes(sessionPending?.action));
      button.hidden=!available;
      button.disabled=!available||pending||(action!=='end'&&Boolean(sessionPending))||!bridge.sessionActionIsCurrent(sessionScope(action));
      button.textContent=pending?{start:'Начинаем…',end:'Завершаем…',skip_interview:'Переходим к занятиям…'}[action]:{start:'Начать',end:'Завершить',skip_interview:'Сразу к занятиям'}[action];
    }
    $('phone-prompt').textContent=simulator?.prompt?.text||'Сейчас нет записанной реплики. Текст личных ответов скрыт.';
    const rfidReady=canMutate()&&Boolean(simulator?.devices?.rfid?.available);
    for(const button of document.querySelectorAll('[data-phone-card]'))button.disabled=!rfidReady;
    if(!rfidReady&&$('phone-rfid-result').textContent==='Карта не предъявлена'){
      $('phone-rfid-result').textContent=!connected()?'Нет соединения с телефоном':!simulator?.devices?.rfid?.available?'Синтетический RFID недоступен':'Дождитесь готовности устройства';
    }
    $('phone-reset').disabled=!simulator||bridge.media().authBlocked;
    $('phone-input-disconnect').disabled=!connected()||!simulator?.fault?.input_connected;
    $('phone-input-reconnect').disabled=!connected()||simulator?.fault?.input_connected;
    $('phone-text').disabled=!canMutate();$('phone-wav').disabled=!canMutate();
    renderMechanical();renderMedia();if(tools.open)diagnostics();
  }
  function update(value) {
    if(!value?.simulator)return;
    if(!bridge.acceptSnapshot(value,state))return;
    const next=value.simulator;
    // console.render already revoked old media before accepting the new owner.
    if((lastEpoch!==null&&next.epoch!==lastEpoch)||(state?.simulator?.session?.session_id!==undefined
      &&next.session.session_id!==state.simulator.session.session_id))cleanup({preserveMedia:true});
    lastEpoch=next.epoch;state=value;simulator=next;
    render();
    if(tools.open)refreshJournal();
  }
  async function refreshJournal() {
    if(journalInFlight||!connected())return;
    journalInFlight=true;
    try {
      const controller=new AbortController();const timer=window.setTimeout(()=>controller.abort(),7000);
      let response;try{response=await fetch(`/stand/simulator/journal?after=${journalCursor}`,{cache:'no-store',signal:controller.signal});}finally{clearTimeout(timer);}
      if(!response.ok)throw new Error(`Журнал: HTTP ${response.status}`);
      const journal=await response.json();
      journalCursor=journal.cursor;
      if(journal.gap)journalItems=[];
      journalItems.push(...journal.items);journalItems=journalItems.slice(-90);
      $('phone-journal-status').textContent=`${journalCursor} событий${journal.gap?' · ранние записи вытеснены':''}`;
      $('phone-journal').replaceChildren(...journalItems.slice().reverse().map(item=>{const li=document.createElement('li');li.textContent=`#${item.cursor} ${item.kind||''} ${item.type||item.name||''} ${JSON.stringify(item.data||item.payload||{})}`;return li;}));
    } catch(error){$('phone-journal-status').textContent=error.message;}
    finally{journalInFlight=false;}
  }
  function openTools() {drawerReturn=document.activeElement;tools.showModal();diagnostics();refreshJournal();}
  function closeTools() {tools.close();drawerReturn?.focus();}
  $('phone-open-tools').onclick=openTools;$('phone-close-tools').onclick=closeTools;
  tools.addEventListener('close',()=>drawerReturn?.focus());
  tools.addEventListener('click',event=>{if(event.target===tools){const r=tools.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right)closeTools();}});
  tools.addEventListener('cancel',()=>input.cancel());
  $('phone-handset').onclick=handset;
  $('phone-reset').onclick=reset;$('reset').onclick=reset;
  $('handset-up').onclick=()=>send({action:'handset',state:'up'}).catch(showError);
  $('handset-down').onclick=()=>{cleanup();send({action:'handset',state:'down'}).catch(showError);};
  $('phone-sound').onclick=async()=>{try{await bridge.unlockAudio();if(state)bridge.render(state);renderMedia();}catch(error){showError(error);}};
  $('phone-microphone').onclick=async()=>{
    microphoneError='';
    try{if(bridge.media().microphone||bridge.media().pending){bridge.stopMic();renderMedia();return;}const promise=bridge.startMic();renderMedia();await promise;}
    catch(error){microphoneError=error.name==='NotAllowedError'?'Доступ к микрофону запрещён. Разрешите его в настройках браузера и повторите.':error.message;}
    renderMedia();
  };
  for(const [action,button] of Object.entries(sessionButtons))if(button){
    let activation=null;
    button.onpointerdown=event=>{if(event.button===0)activation={body:sessionScope(action)};};
    button.onkeydown=event=>{if(['Space','Enter'].includes(event.code)){if(event.repeat)event.preventDefault();else activation={body:sessionScope(action)};}};
    button.onpointercancel=button.onblur=()=>{if(activation)activation.cancelled=true;};
    button.onclick=event=>{
      const captured=activation;activation=null;
      if(button.hidden||button.disabled||captured?.cancelled||(event.detail>0&&!captured))return;
      return sessionAction(action,captured?.body||sessionScope(action));
    };
  }
  for(const [id,key] of keys) {
    key.addEventListener('pointerdown',event=>{if(event.button!==0)return;event.preventDefault();if(input.press(id,event.pointerId)){pointers.set(event.pointerId,id);key.setPointerCapture(event.pointerId);}});
    key.addEventListener('pointerup',event=>{event.preventDefault();pointers.delete(event.pointerId);input.release(id,false,event.pointerId);});
    key.addEventListener('pointercancel',event=>{pointers.delete(event.pointerId);input.release(id,true,event.pointerId);});
    key.addEventListener('lostpointercapture',event=>{pointers.delete(event.pointerId);input.release(id,true,event.pointerId);});
    key.addEventListener('keydown',event=>{if(['Space','Enter'].includes(event.code)){event.preventDefault();if(!event.repeat)input.press(id,event.code);}});
    key.addEventListener('keyup',event=>{if(['Space','Enter'].includes(event.code)){event.preventDefault();input.release(id,false,event.code);}});
    key.addEventListener('blur',()=>input.release(id,true));
    key.addEventListener('click',event=>{event.preventDefault();if(event.detail===0&&!input.active.has(id)){input.press(id);input.release(id);}});
  }
  window.addEventListener('pointerup',event=>{const id=pointers.get(event.pointerId);pointers.delete(event.pointerId);if(id)input.release(id,false,event.pointerId);});
  window.addEventListener('pointercancel',event=>{const id=pointers.get(event.pointerId);pointers.delete(event.pointerId);if(id)input.release(id,true,event.pointerId);});
  window.addEventListener('keydown',event=>{
    if(event.defaultPrevented||event.repeat||tools.open||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)||document.activeElement?.isContentEditable)return;
    const id=Number(event.key);
    if(keys.has(id)){event.preventDefault();input.press(id,event.code);}
    else if(event.code==='Space'&&document.activeElement?.tagName!=='BUTTON'){event.preventDefault();handset();}
  });
  window.addEventListener('keyup',event=>{const id=Number(event.key);if(input.active.has(id)){event.preventDefault();input.release(id,false,event.code);}});
  window.addEventListener('blur',()=>input.cancel());
  document.addEventListener('visibilitychange',()=>{if(document.hidden){cleanup();clearTimeout(mediaTimer);}else if(!stopped)tickMedia();});
  window.addEventListener('pagehide',()=>{stopped=true;cleanup();clearTimeout(mediaTimer);rendererGeneration++;model?.destroy();model=null;});
  for(const button of document.querySelectorAll('[data-phone-card]'))button.onclick=async()=>{
    const card=button.dataset.phoneCard;
    $('phone-rfid-result').textContent=`Чтение ${card} через Max…`;
    try{
      const value=await send({action:'rfid',card});
      if(!value){$('phone-rfid-result').textContent='Операция отменена';return;}
      const status=value.result?.auth_status;
      const messages={
        identified:`Карта ${card}: авторизация принята`,
        no_personalization:`Карта ${card}: профиль не найден или в архиве (гость)`,
        ignored_in_current_state:`Карта ${card}: не применена в текущем состоянии сеанса`,
        duplicate_suppressed:`Карта ${card}: повторное чтение подавлено Max`,
      };
      $('phone-rfid-result').textContent=messages[status]||`Max: ${value.result?.reason||'чтение завершено'}`;
    }
    catch(error){$('phone-rfid-result').textContent=error.message;showError(error);}
  };
  const fault=async body=>{if(body.mode==='disconnect')cleanup();try{await send({device:'input',...body},'/stand/simulator/fault');}catch(error){showError(error);}};
  $('phone-input-disconnect').onclick=()=>fault({mode:'disconnect'});
  $('phone-input-reconnect').onclick=()=>fault({mode:'reconnect'});
  $('phone-delay-form').onsubmit=event=>{event.preventDefault();fault({mode:'delay',delay_ms:Number($('phone-delay').value)});};
  $('phone-text-form').onsubmit=async event=>{event.preventDefault();const text=$('phone-text').value.trim();if(!text||!canMutate())return;bridge.clearMic();try{await bridge.post('/stand/text',{text});$('phone-text').value='';}catch(error){showError(error);}};
  $('phone-wav').onchange=event=>{bridge.clearMic();$('wav').onchange(event).catch(showError);};
  $('phone-refresh-journal').onclick=refreshJournal;
  async function threeView() {
    const generation=++rendererGeneration;
    try {
      const {createPhoneModel}=await import('./phone-model.js?v=3-fill-channel-fix');
      if(generation!==rendererGeneration||stopped)return;
      model=createPhoneModel(stage,{press:id=>input.press(id,'model'),release:(id,cancel)=>input.release(id,cancel,'model'),handset,project:(id,x,y,w,ratio)=>{const key=keys.get(id);key.style.left=`${x*100}%`;key.style.top=`${y*100}%`;key.style.width=`${w*stage.clientWidth}px`;key.style.height=`${w*stage.clientWidth*ratio}px`;},onFailure:message=>{$('phone-render-notice').textContent=message;}});
      stage.dataset.renderer='three';model.update(simulator,input.pressed());$('phone-render-notice').textContent='';
    } catch(error){if(generation===rendererGeneration)$('phone-render-notice').textContent='3D недоступен в этом браузере. Используйте клавиши 1–7 и Пробел.';}
  }
  window.mishkaPhoneUpdate=update;
  window.mishkaPhoneError=showError;
  window.mishkaPhoneMedia=renderMedia;
  window.mishkaPhoneConnection=live=>{if(!live)cleanup({preserveReconnect:true});render();};
  function tickMedia() {clearTimeout(mediaTimer);if(stopped||document.hidden)return;renderMedia();mediaTimer=setTimeout(tickMedia,350);}
  if(bridge.snapshot())update(bridge.snapshot());else render();
  tickMedia();threeView();
}
mountPhone();
