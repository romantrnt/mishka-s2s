"""Exercise the actual console renderer with canonical descriptor pages."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest
from test_menu_content import manifest
from app.foundation.menu_content import build_menu_page
from app.foundation.game_engine import IMMERSIVE_GAME_SPECS

@pytest.mark.parametrize('scene',['fixed_story_menu','game_menu'])
def test_console_renders_each_real_descriptor_page(scene):
    node=shutil.which('node')
    if not node:pytest.skip('Node required for actual console renderer')
    source=Path('app/foundation/console/console.js').read_text()
    function=(source[source.index('function interactionActive('):source.index('function sessionActionIsCurrent(')]
        + source[source.index('function interactionGuide('):source.index('\nfunction enqueuePCM(')])
    first=build_menu_page(scene,'3-5',stories=manifest(),game_specs=IMMERSIVE_GAME_SPECS)
    for index in range(first.page_count):
        page=build_menu_page(scene,'3-5',index,stories=manifest(),game_specs=IMMERSIVE_GAME_SPECS)
        state={'session':{'state':'ACTIVE','node':scene,'handset_up':True},'canonical_state':{'mode':scene,'scene_menu_descriptor':page.as_dict()}}
        script='const mic=null,micStarting=false,audioRetry=null; const microphoneAccepting=()=>false; const elements={}; const $=id=>elements[id]??=( {} ); const document={querySelector:()=>null}; const buttonLegendSummary=()=>"root";\n'+function+'\nupdateGuide('+json.dumps(state)+',{}); console.log(JSON.stringify(elements));'
        rendered=json.loads(subprocess.check_output([node,'-e',script],text=True))
        output=rendered['guide-text']['textContent']
        for item in page.items:assert f'{item.slot} — {item.title}' in output
        assert ('5 — помощь' in output) == (5 in page.buttons and not any(item.slot == 5 for item in page.items))
        assert ('6 — следующие варианты' in output) == (6 in page.buttons)
        assert '7 — назад' in output
