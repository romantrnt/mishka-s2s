(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const events = {
    AnonymousStart:'Гостевой вход',SessionStarted:'Сеанс начат',SessionAudioEnabled:'Звук разрешён',
    InterviewSkipped:'Знакомство пропущено',SessionReset:'Сеанс сброшен',SessionCancel:'Сеанс завершён',
    InteractionIdleExpired:'Завершение по бездействию',HandsetLifted:'Трубка поднята',HandsetPlaced:'Трубка положена',
    ButtonPressed:'Нажата кнопка',ButtonReleased:'Кнопка отпущена',ButtonHeld:'Удержание кнопки',ButtonLongHeld:'Долгое удержание',
    SpeechReceived:'Обнаружена речь',VoiceIntentReady:'Голосовая команда обработана',
    AudioStarted:'Воспроизведение началось',AudioCompleted:'Воспроизведение завершилось',AudioFailed:'Ошибка воспроизведения',
    DialogueFallback:'Резервный ответ',TimeoutFired:'Сработал таймер',WelcomeTimerFired:'Окончилось ожидание приветствия',
    GameTimer:'Шаг игрового таймера',HardwareDisconnected:'Устройство отключено',HardwareReconnected:'Устройство подключено',
    ExhibitionOnboardingComplete:'Знакомство завершено',ExhibitionMenuWaveComplete:'Подсказка меню завершена',StoryChoiceWaveComplete:'Выбор сказки готов',
    ClientProfileReady:'Авторизация готова',ClientProfileCleared:'Авторизация очищена',IdentityReadyExpired:'Ожидание авторизации истекло',IdentityAuthorizationExpired:'Авторизация истекла',
  };
  const modes = {
    idle:'Ожидание',profile_ready:'Готов к началу',menu_ready:'Меню готово',root_menu:'Главное меню',
    game_menu:'Выбор игры',game_active:'Игра',fixed_story_menu:'Выбор записанной сказки',fixed_story_playing:'Записанная сказка',
    personal_story_theme:'Выбор новой сказки',personal_story_generating:'Подготовка новой сказки',personal_story_playing:'Новая сказка',
    story_paused:'Сказка на паузе',story_pause_announcement:'Переход к паузе',story_resume_announcement:'Возвращение к сказке',
    story_speech_analysis:'Обработка команды к сказке',bear_dialogue:'Разговор с Мишкой',support_intake:'Рассказать или спросить',
    wellbeing_check:'Вопрос о самочувствии',safety_stop:'Безопасная остановка',schedule_weather:'День и планы',awaiting_handset:'Ожидание трубки',
    anonymous_visual:'Гостевой режим без звука',automatic_mood_check:'Вопрос о настроении',complaint_intake:'Рассказ о самочувствии',
    dialogue_memory_expired:'Контекст разговора завершён',dialogue_suspended_ram:'Разговор приостановлен',
    exhibition_age_complete:'Возраст выбран',exhibition_age_hint:'Подсказка выбора возраста',exhibition_age_prompt:'Вопрос о возрасте',exhibition_age_selection:'Выбор возраста',
    exhibition_introduction:'Знакомство',exhibition_menu_repeat_listen:'Ожидание ответа о меню',exhibition_menu_repeat_prompt:'Повтор меню',
    exhibition_menu_replay_buttons:'Подсказка кнопок',exhibition_menu_tutorial_buttons:'Знакомство с кнопками',exhibition_menu_tutorial_intro:'Объяснение меню',
    exhibition_menu_tutorial_prompt:'Подсказка меню',exhibition_menu_wave:'Световая подсказка меню',exhibition_name_capture:'Ожидание имени',
    exhibition_name_question:'Вопрос об имени',exhibition_onboarding_complete:'Знакомство завершено',fixed_story_choice_wave:'Подсветка выбора сказки',
    mood_report:'Ответ о настроении',personal_story_validating:'Проверка новой сказки',personal_welcome_listen:'Ожидание ответа на приветствие',
    schedule_navigation:'Навигация по планам',session_closed:'Сеанс закрыт',soft_end:'Завершение сеанса',story_suspended_ram:'Сказка приостановлена',
    visual_welcome:'Приветствие без звука',voice_welcome:'Приветствие',
  };
  const statuses = {
    ready:'Готов',degraded:'Требует внимания',available:'Доступно',unavailable:'Недоступно',simulated:'Симулятор',
    active:'Работает',stopped:'Остановлено',not_started:'Не запущено',device_unavailable:'Устройство недоступно',
    device_disconnected:'Устройство отключено',input_desynchronized:'Нарушена синхронизация',reader_exception:'Ошибка чтения',dispatch_failure:'Ошибка обработки',
    up:'Поднята',down:'Положена',BOOTING:'Запуск',WARMING_UP:'Прогрев',READY:'Готов',DEGRADED:'Требует внимания',STOPPING:'Остановка',
  };
  const components = {
    hid_start:'Запуск кнопок и трубки',hid_reader:'Кнопки и трубка',physical_lighting_loss:'Подсветка',story_playback:'Воспроизведение сказки',
    device_command:'Команда устройству',panel_session:'Управление сеансом',uart_ingress:'Ввод UART',uart_egress:'Вывод UART',
    raw_uart:'Транспорт UART',phrase_audio:'Звук реплики',story_audio:'Звук сказки',shutdown:'Завершение работы',
    headless_audio:'Звук телефона',headless_audio_capture:'Микрофон телефона',headless_audio_processing:'Обработка звука',
  };
  const label = (map, key, fallback='Неизвестно') => Object.hasOwn(map, key) ? map[key] : fallback;
  const clock = value => new Date(value * 1000).toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  let cursor=0,stream='',items=[],timer=null,controller=null,stopped=false;

  function describe(item) {
    const d=item.data || {};
    if(item.kind==='event') return [label(events,d.event,'Событие устройства')+(Number.isInteger(d.button)&&d.button>=1&&d.button<=7?` ${d.button}`:''),''];
    if(item.kind==='state') return ['Смена режима',`${label(modes,d.from,'Другой режим')} → ${label(modes,d.to,'Другой режим')}`];
    if(item.kind==='error') return ['Техническая ошибка',label(components,d.component,'Компонент не определён; проверьте диагностику телефона.')];
    if(item.kind==='alert_opened') return [d.kind==='urgent_safety'?'Сигнал безопасности':'Запрошена помощь взрослого','Локальный сигнал. Доставка и помощь не подтверждены.'];
    if(item.kind==='alert_ended') return ['Сигнал больше не активен','Режим или сеанс завершён. Подтверждения помощи взрослого нет.'];
    if(item.kind==='reset') return ['Сброс сеанса','История технических событий сохранена до перезапуска процесса.'];
    if(item.kind==='started') return ['Технический буфер запущен','Здесь сохраняются только технические события.'];
    if(item.kind==='health') return ['Состояние компонентов изменилось',`Звук: ${label(statuses,d.audio)}. Ввод: ${label(statuses,d.hid)}. Подсветка: ${label(statuses,d.lighting)}.`];
    return ['Техническое событие',''];
  }
  function row(item) {
    const li=document.createElement('li');li.dataset.level=item.level==='error'?'error':'info';
    const time=document.createElement('time');time.textContent=clock(item.at);time.dateTime=new Date(item.at*1000).toISOString();
    const body=document.createElement('div'),title=document.createElement('div'),detail=document.createElement('div');
    const [a,b]=describe(item);title.className='operator-row-title';title.textContent=a;detail.className='operator-row-detail';detail.textContent=b;
    body.append(title,detail);li.append(time,body);return li;
  }
  function renderItems() {
    const filter=$('operator-filter').value;
    const timeline=items.filter(i=>{
      if(['error','health','started','reset'].includes(i.kind)) return false;
      if(filter==='alerts') return i.kind==='alert_opened'||i.kind==='alert_ended';
      if(filter==='controls') return i.kind==='event'&&['HandsetLifted','HandsetPlaced','ButtonPressed','ButtonReleased','ButtonHeld','ButtonLongHeld','SessionStarted','SessionCancel'].includes(i.data?.event);
      if(filter==='audio') return i.kind==='event'&&['AudioStarted','AudioCompleted','AudioFailed','SpeechReceived','VoiceIntentReady'].includes(i.data?.event);
      return true;
    }).slice().reverse();
    const logs=items.filter(i=>['error','health','started','reset'].includes(i.kind)).slice().reverse();
    $('operator-timeline').replaceChildren(...timeline.map(row));$('operator-timeline-empty').hidden=timeline.length>0;
    $('operator-logs').replaceChildren(...logs.map(row));$('operator-logs-empty').hidden=logs.length>0;
  }
  function render(data) {
    if(!Number.isSafeInteger(data.cursor)||data.cursor<0||typeof data.stream!=='string'||!Array.isArray(data.items)) throw new Error('invalid');
    if(stream!==data.stream||data.gap) items=[];
    const prior=new Set(items.map(i=>i.cursor));
    for(const item of data.items.slice(-240)) {
      if(Number.isSafeInteger(item.cursor)&&Number.isFinite(item.at)&&item.at>0&&item.at<1e12&&!prior.has(item.cursor)) {items.push(item);prior.add(item.cursor);}
    }
    items=items.slice(-240);cursor=data.cursor;stream=data.stream;
    $('operator-gap').hidden=!data.gap;
    const alert=data.alert,active=alert&&['help_requested','urgent_safety'].includes(alert.kind);
    $('operator-alert').dataset.active=String(Boolean(active));$('operator-alert').dataset.stale='false';
    $('operator-alert-title').textContent=active?(alert.kind==='urgent_safety'?'Сигнал безопасности: нужен взрослый рядом':'Нужна помощь взрослого рядом'):'Активных сигналов помощи нет';
    $('operator-alert-text').textContent=active?'Проверьте, что происходит у телефона. Сигнал показан локально; автоматической отправки взрослому нет.':'Здесь появится запрос помощи или сигнал безопасности.';
    const h=data.health||{},fields=[['Контур',label(statuses,h.runtime)],['Трубка',label(statuses,h.handset)],['Звук',label(statuses,h.audio)],['Кнопки',label(statuses,h.hid)],['Подсветка',label(statuses,h.lighting)],['Каналы света',Number.isInteger(h.lit_channels)&&h.lit_channels>=0&&h.lit_channels<=18?`${h.lit_channels} из 18`:'Неизвестно']];
    $('operator-health').replaceChildren(...fields.map(([a,b])=>{const div=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=a;dd.textContent=b;div.append(dt,dd);return div;}));
    $('operator-mode').textContent=`Режим: ${label(modes,data.mode,'другой режим')}`;
    $('operator-updated').textContent=`Обновлено ${clock(Date.now()/1000)}`;
    $('operator-connection').textContent='На связи';$('operator-connection').classList.remove('offline');$('operator-error').hidden=true;
    renderItems();
  }
  async function refresh() {
    clearTimeout(timer);
    if(stopped||controller||document.hidden) return;
    controller=new AbortController();const timeout=setTimeout(()=>controller?.abort(),5000);
    try {
      const params=new URLSearchParams({after:String(cursor),stream});
      const response=await fetch(`/stand/operator?${params}`,{cache:'no-store',credentials:'same-origin',signal:controller.signal});
      if(!response.ok) throw new Error(response.status===401?'auth':'connection');
      render(await response.json());
    } catch(error) {
      if(stopped) return;
      $('operator-connection').textContent='Нет свежих данных';$('operator-connection').classList.add('offline');
      $('operator-alert').dataset.stale='true';$('operator-error').hidden=false;
      $('operator-error').textContent=error.message==='auth'?'Нет доступа. Откройте панель телефона и войдите, затем обновите эту страницу.':'Не удалось обновить состояние. Показаны последние полученные данные; актуальность сигналов не подтверждена. Повторяем подключение.';
    } finally {clearTimeout(timeout);controller=null;if(!stopped&&!document.hidden) timer=setTimeout(refresh,1000);}
  }
  $('operator-refresh').addEventListener('click',refresh);
  $('operator-filter').addEventListener('change',renderItems);
  document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(timer);controller?.abort();}else refresh();});
  window.addEventListener('pagehide',()=>{stopped=true;clearTimeout(timer);controller?.abort();});
  window.addEventListener('pageshow',()=>{stopped=false;refresh();});
  refresh();
})();
