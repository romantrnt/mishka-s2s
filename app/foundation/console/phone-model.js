import * as THREE from './vendor/three.module.min.js';

// Reference proportions describe the prototype, not a measured CAD model.
export const KEY_LAYOUT = [
  { id: 1, x: .174, y: .681, w: .177, h: .224, color: '#ff100b' },
  { id: 2, x: .320, y: .252, w: .134, h: .198, color: '#ffad00' },
  { id: 3, x: .519, y: .155, w: .130, h: .200, color: '#ffe600' },
  { id: 4, x: .724, y: .250, w: .131, h: .200, color: '#12eb00' },
  { id: 5, x: .868, y: .426, w: .126, h: .190, color: '#00d5e3' },
  { id: 6, x: .442, y: .711, w: .209, h: .510, color: '#0739ef' },
  { id: 7, x: .677, y: .709, w: .210, h: .511, color: '#ee22e8' },
];
const CHANNELS = {1:[14,15],2:[13,12],3:[10,11],4:[9,8],5:[6,7],6:[3,2,4,5],7:[1,0,17,16]};

export function drawSymbol(ctx, id, x, y, size) {
  ctx.save(); ctx.translate(x, y); ctx.scale(size / 100, size / 100);
  ctx.fillStyle = '#fff';
  const circle = (x,y,r) => {ctx.beginPath();ctx.arc(x,y,r,0,Math.PI*2);ctx.fill();};
  if (id === 6) {
    circle(-31,-27,15); circle(31,-27,15); circle(0,2,40);
    ctx.globalCompositeOperation='destination-out'; circle(-15,-3,4); circle(15,-3,4);
    ctx.beginPath();ctx.ellipse(0,19,21,16,0,0,Math.PI*2);ctx.fill();
    ctx.globalCompositeOperation='source-over';
    ctx.beginPath();ctx.ellipse(0,12,9,6,0,0,Math.PI*2);ctx.fill();
  } else if (id === 7) {
    circle(-22,-16,23);
    ctx.globalCompositeOperation='destination-out'; circle(-9,-4,23);
    ctx.globalCompositeOperation='source-over'; circle(3,4,22); circle(25,12,19);circle(-17,17,16);
    ctx.fillRect(-17,9,42,25);
  }
  ctx.restore();
}

function labelTexture(id, symbol = false) {
  const canvas=document.createElement('canvas');canvas.width=256;canvas.height=256;
  const ctx=canvas.getContext('2d');
  if (symbol) drawSymbol(ctx,id,128,128,194);
  else {ctx.fillStyle='#fff';ctx.font='600 210px Arial, sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(String(id),128,139);}
  const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;
  return texture;
}
function softShape(w,h,id) {
  const s=new THREE.Shape();
  if (id===6 || id===7) {
    const flip=id===7?-1:1;
    const p=(x,y)=>[x*w*flip,y*h];
    s.moveTo(...p(.42,.43));
    s.bezierCurveTo(...p(.3,.57),...p(-.29,.56),...p(-.43,.34));
    s.bezierCurveTo(...p(-.7,-.02),...p(-.34,-.43),...p(.31,-.5));
    s.bezierCurveTo(...p(.48,-.53),...p(.48,-.28),...p(.47,.02));
    s.bezierCurveTo(...p(.47,.23),...p(.46,.37),...p(.42,.43));
  } else {
    s.absellipse(0,0,w*.5,h*.5,0,Math.PI*2,false,0);
  }
  return s;
}
function extruded(shape, depth, bevel) {
  return new THREE.ExtrudeGeometry(shape,{depth,bevelEnabled:true,bevelSegments:6,steps:1,bevelSize:bevel,bevelThickness:bevel,curveSegments:32});
}
function pawShape() {
  const s=new THREE.Shape();
  const p=(x,y)=>[(x-.5)*3.8,(.52-y)*2.7];
  s.moveTo(...p(.10,.59));
  s.bezierCurveTo(...p(.01,.65),...p(.06,.84),...p(.23,.85));
  s.bezierCurveTo(...p(.30,.86),...p(.31,.83),...p(.33,.85));
  s.bezierCurveTo(...p(.42,1.03),...p(.65,1.05),...p(.79,.91));
  s.bezierCurveTo(...p(.91,.79),...p(.91,.64),...p(.92,.55));
  s.bezierCurveTo(...p(1.02,.43),...p(.97,.27),...p(.88,.29));
  s.bezierCurveTo(...p(.83,.31),...p(.83,.33),...p(.81,.30));
  s.bezierCurveTo(...p(.87,.09),...p(.66,.05),...p(.64,.18));
  s.bezierCurveTo(...p(.63,.21),...p(.62,.21),...p(.61,.15));
  s.bezierCurveTo(...p(.58,-.04),...p(.42,-.04),...p(.42,.15));
  s.bezierCurveTo(...p(.42,.22),...p(.40,.22),...p(.39,.19));
  s.bezierCurveTo(...p(.29,.04),...p(.19,.15),...p(.22,.30));
  s.bezierCurveTo(...p(.23,.40),...p(.30,.48),...p(.29,.53));
  s.bezierCurveTo(...p(.27,.58),...p(.17,.55),...p(.10,.59));
  return s;
}

export function createPhoneModel(stage, handlers) {
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,powerPreference:'low-power'});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.75));
  renderer.outputColorSpace=THREE.SRGBColorSpace;
  renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=.92;
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  renderer.domElement.className='phone-canvas';renderer.domElement.setAttribute('aria-hidden','true');
  stage.prepend(renderer.domElement);
  const scene=new THREE.Scene();
  const camera=new THREE.PerspectiveCamera(36,1,.1,60);
  const root=new THREE.Group();scene.add(root);root.rotation.y=-.12;root.rotation.x=-.035;
  const shellMaterial=new THREE.MeshPhysicalMaterial({color:'#198c9d',roughness:.48,metalness:0,clearcoat:.12,clearcoatRoughness:.65});
  const bodyShape=new THREE.Shape();
  bodyShape.moveTo(-1.7,-1.55);
  bodyShape.bezierCurveTo(-2.24,-1.55,-2.28,-1.13,-2.15,-.45);
  bodyShape.bezierCurveTo(-2.07,.34,-1.79,1.3,-1.31,1.47);
  bodyShape.bezierCurveTo(-.6,1.72,.81,1.72,1.4,1.42);
  bodyShape.bezierCurveTo(1.89,1.15,2.09,.2,2.16,-.62);
  bodyShape.bezierCurveTo(2.28,-1.26,2.06,-1.56,1.63,-1.56);
  bodyShape.bezierCurveTo(.8,-1.64,-.93,-1.64,-1.7,-1.55);
  const shell=new THREE.Mesh(extruded(bodyShape,.78,.22),shellMaterial);shell.position.z=-.66;shell.castShadow=true;shell.receiveShadow=true;root.add(shell);
  const plateShape=pawShape();
  const gasket=new THREE.Mesh(extruded(plateShape,.025,.054),new THREE.MeshStandardMaterial({color:'#334b52',roughness:.83}));gasket.position.set(.05,-.03,.347);root.add(gasket);
  const plate=new THREE.Mesh(extruded(plateShape,.028,.032),new THREE.MeshStandardMaterial({color:'#697780',roughness:.72}));plate.position.set(.05,-.03,.38);plate.receiveShadow=true;root.add(plate);
  const handset=new THREE.Group();root.add(handset);
  const curve=new THREE.CatmullRomCurve3([new THREE.Vector3(-1.83,1.55,.12),new THREE.Vector3(-1.13,1.87,.05),new THREE.Vector3(0,1.98,.015),new THREE.Vector3(1.13,1.88,.05),new THREE.Vector3(1.83,1.57,.12)]);
  const receiver=new THREE.Mesh(new THREE.TubeGeometry(curve,72,.315,24,false),shellMaterial);receiver.castShadow=true;receiver.userData.handset=true;handset.add(receiver);
  const interactive=[receiver];
  for (const t of [0,1]) {
    const cap=new THREE.Mesh(new THREE.SphereGeometry(.318,32,24),shellMaterial);cap.position.copy(curve.getPoint(t));cap.castShadow=true;cap.userData.handset=true;handset.add(cap);interactive.push(cap);
  }
  for(const x of [-1.42,1.42]) {
    const cradle=new THREE.Mesh(new THREE.SphereGeometry(.33,24,16),new THREE.MeshStandardMaterial({color:'#126472',roughness:.65}));cradle.scale.set(1,.32,.83);cradle.position.set(x,1.37,.04);root.add(cradle);
  }
  const keys=new Map();
  for(const item of KEY_LAYOUT) {
    const w=item.w*3.8,h=item.h*2.7;
    const group=new THREE.Group();group.position.set((item.x-.5)*3.8+.05,(.52-item.y)*2.7-.03,.47);root.add(group);
    if(item.id<6) group.rotation.z=({1:.12,2:.16,3:-.035,4:-.18,5:-.3})[item.id];
    const shape=softShape(w,h,item.id);
    const base=new THREE.Mesh(extruded(shape,.04,.045),new THREE.MeshStandardMaterial({color:'#2b414a',roughness:.68}));group.add(base);
    const material=new THREE.MeshPhysicalMaterial({color:item.color,roughness:.41,metalness:0,clearcoat:.14,emissive:item.color,emissiveIntensity:0});
    const cap=new THREE.Group();group.add(cap);cap.position.z=.045;
    const mesh=new THREE.Mesh(extruded(shape,.045,.027),material);mesh.castShadow=true;mesh.receiveShadow=true;mesh.userData.button=item.id;cap.add(mesh);interactive.push(mesh);
    const labelMaterial=new THREE.MeshBasicMaterial({map:labelTexture(item.id),transparent:true,depthWrite:false,toneMapped:false,color:'#d8e0e0'});
    const digitSize=item.id>=6?.38:Math.min(w,h)*.84;
    const digit=new THREE.Mesh(new THREE.PlaneGeometry(digitSize,digitSize),labelMaterial);digit.position.set(item.id===6?.08:item.id===7?-.08:0,item.id>=6?-h*.24:0,.076);cap.add(digit);
    let symbolMaterial=null;
    if(item.id>=6) {
      symbolMaterial=new THREE.MeshBasicMaterial({map:labelTexture(item.id,true),transparent:true,depthWrite:false,toneMapped:false,color:'#d8e0e0'});
      const symbol=new THREE.Mesh(new THREE.PlaneGeometry(w*.68,w*.68),symbolMaterial);symbol.position.set(item.id===6?-.035:.035,h*.21,.076);cap.add(symbol);
    }
    keys.set(item.id,{group,cap,mesh,material,labelMaterial,symbolMaterial,color:item.color,pressed:false});
  }
  const ground=new THREE.Mesh(new THREE.PlaneGeometry(200,200),new THREE.ShadowMaterial({color:'#385e67',opacity:.16}));ground.rotation.x=-Math.PI/2;ground.position.y=-1.9;ground.receiveShadow=true;scene.add(ground);
  scene.add(new THREE.HemisphereLight('#ffffff','#89abb1',1.5));
  const keyLight=new THREE.DirectionalLight('#ffffff',2.1);keyLight.position.set(-3.5,7,6);keyLight.castShadow=true;keyLight.shadow.mapSize.set(2048,2048);keyLight.shadow.camera.left=-5;keyLight.shadow.camera.right=5;keyLight.shadow.camera.top=6;keyLight.shadow.camera.bottom=-4;keyLight.shadow.normalBias=.035;keyLight.shadow.bias=-.0002;keyLight.shadow.radius=5;scene.add(keyLight);
  const fill=new THREE.DirectionalLight('#d9f0f3',.6);fill.position.set(5,2,4);scene.add(fill);
  const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2();
  let activePointer=null,hover=null,destroyed=false,frame=null,up=false,handsetValue=0,lastTime=performance.now();
  const reduced=window.matchMedia('(prefers-reduced-motion: reduce)');
  const hit=event=>{const rect=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1);raycaster.setFromCamera(pointer,camera);return raycaster.intersectObjects(interactive,false)[0]?.object.userData;};
  const down=event=>{
    if(event.button!==0 || activePointer) return;
    const target=hit(event);if(!target)return;
    if(target.button && !handlers.press(target.button))return;
    event.preventDefault();activePointer={id:event.pointerId,button:target.button,handset:target.handset};renderer.domElement.setPointerCapture(event.pointerId);
  };
  const finish=(event,cancel=false)=>{if(!activePointer||event.pointerId!==activePointer.id)return;const target=activePointer;activePointer=null;if(target.button)handlers.release(target.button,cancel);else if(target.handset&&!cancel)handlers.handset();};
  const move=event=>{hover=hit(event);renderer.domElement.style.cursor=hover?'pointer':'default';};
  renderer.domElement.addEventListener('pointerdown',down);
  renderer.domElement.addEventListener('pointerup',event=>finish(event));
  renderer.domElement.addEventListener('pointercancel',event=>finish(event,true));
  renderer.domElement.addEventListener('lostpointercapture',event=>finish(event,true));
  renderer.domElement.addEventListener('pointermove',move);
  renderer.domElement.addEventListener('webglcontextlost',event=>{event.preventDefault();handlers.onFailure('3D недоступен: включено плоское управление.');});
  const resize=()=>{
    const {width,height}=stage.getBoundingClientRect();if(!width||!height)return;
    camera.aspect=width/height;
    const distance=camera.aspect<.85?11.2/camera.aspect*.78:camera.aspect<1.1?10.5:8.8;
    camera.position.set(distance*.12,distance*.125,distance);camera.lookAt(0,.55,0);camera.updateProjectionMatrix();renderer.setSize(width,height,false);
  };
  const observer=new ResizeObserver(resize);observer.observe(stage);resize();
  const projectControls=()=>{
    root.updateMatrixWorld(true);
    for(const [id,key] of keys) {
      const center=new THREE.Vector3();key.mesh.getWorldPosition(center);center.z+=.1;center.project(camera);
      const item=KEY_LAYOUT[id-1];
      const edge=key.mesh.localToWorld(new THREE.Vector3(item.w*3.8*.55,0,0)).project(camera);
      handlers.project(id,(center.x+1)*.5,(1-center.y)*.5,Math.max(.045,Math.abs(edge.x-center.x)),item.h/item.w*.71);
    }
  };
  const animate=now=>{
    if(destroyed)return;
    const delta=Math.min(.06,(now-lastTime)/1000);lastTime=now;
    handsetValue+=(Number(up)-handsetValue)*(reduced.matches?1:1-Math.exp(-delta*9));
    handset.position.set(-.06*handsetValue,.77*handsetValue,.37*handsetValue);handset.rotation.z=-.08*handsetValue;
    for(const key of keys.values()) {
      const target=key.pressed?-.06:.045;
      key.cap.position.z+=(target-key.cap.position.z)*(reduced.matches?1:1-Math.exp(-delta*(key.pressed?30:23)));
    }
    projectControls();renderer.render(scene,camera);frame=requestAnimationFrame(animate);
  };
  frame=requestAnimationFrame(animate);
  return {
    update(simulator,localPressed=new Set()) {
      up=simulator?.devices?.handset?.state==='up';
      const lights=simulator?.devices?.lighting||{};
      for(const [id,key] of keys) {
        const state=simulator?.devices?.buttons?.find(item=>item.id===id);
        key.pressed=localPressed.has(id)||Boolean(state?.pressed);
        const [fill,digit,symbol]=CHANNELS[id];
        const lit=lights[fill]===1;
        key.material.color.set(lit?key.color:'#8b8b8b');
        key.material.emissiveIntensity=lit?.32:0;
        key.labelMaterial.color.set(lights[digit]===1?'#ffffff':'#d8e0e0');
        if(key.symbolMaterial)key.symbolMaterial.color.set(lights[symbol]===1?'#ffffff':'#d8e0e0');
      }
    },
    destroy() {destroyed=true;cancelAnimationFrame(frame);observer.disconnect();if(activePointer?.button)handlers.release(activePointer.button,true);scene.traverse(object=>{object.geometry?.dispose();const materials=Array.isArray(object.material)?object.material:[object.material];for(const material of materials){material?.map?.dispose();material?.dispose();}});renderer.dispose();renderer.domElement.remove();},
  };
}
