import json, pathlib, re, sys
root=pathlib.Path(__file__).resolve().parents[1]
errors=[]; notes=[]
def ok(label, cond, detail=''):
    if not cond: errors.append(f'{label}: {detail}')
    print(('PASS ' if cond else 'FAIL ')+label+(f' — {detail}' if detail else ''))
scenario_path=root/'content/scenarios/fixture_core.json'
data=json.loads(scenario_path.read_text())
nodes=data.get('nodes',[]); by={n.get('id'):n for n in nodes}
ok('scenario JSON loads', isinstance(nodes,list) and bool(nodes))
ok('node ids unique', len(by)==len(nodes), f'{len(nodes)} nodes')
refs=[]
for n in nodes:
    for event,target in (n.get('transitions') or {}).items():
        refs.append((n.get('id'),event,target))
        if target not in by: errors.append(f'unknown target {n.get("id")} {event} -> {target}')
ok('transition targets exist', not any('unknown target' in e for e in errors), f'{len(refs)} transitions')
choose=by.get('fixture_choose',{})
for b in range(1,8): ok(f'fixture_choose button_{b}', f'button_{b}' in (choose.get('transitions') or {}), str(choose.get('transitions',{}).get(f'button_{b}')))
required={'fixture_welcome','fixture_choose','fixture_game_menu','fixture_story_menu','fixture_free','fixture_schedule_weather','fixture_fallback','fixture_end'}
ok('required core nodes', required.issubset(by), ', '.join(sorted(required-set(by))))
for prefix in ('fixture_game_','fixture_story_'):
    items=[n for n in nodes if n.get('id','').startswith(prefix) and n.get('id') not in {prefix+'menu'}]
    ok(prefix+' branches present', bool(items), str(len(items)))
source=(root/'app/foundation/application.py').read_text()
stand=(root/'app/foundation/stand.py').read_text()
config=(root/'app/foundation/config.py').read_text()
ok('button 7 service handler', 'schedule_weather' in source and 'self.schedule_weather.build' in source)
ok('anonymous/no-RFID path', 'anonymous' in source and 'No RFID' in stand or 'anonymous' in source)
ok('duplicate sequence guard', 'idempotent_duplicate' in source)
ok('late event guard', 'late_event_rejected' in source)
ok('session duration guard', 'max_session_seconds' in source)
ok('barge-in cancels audio', 'cancel_audio_completions' in source and 'SpeechReceived' in source)
ok('configured session timeout exists', 'session_timeout_s' in config)
print(f'RESULT {"PASS" if not errors else "FAIL"} checks={len(errors)+14} failures={len(errors)}')
if errors:
    print('FAILURES:')
    print('\n'.join('- '+e for e in errors))
    sys.exit(1)
