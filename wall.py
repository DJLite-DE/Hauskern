# Hauskern – lokale Smart-Home-Zentrale · Copyright (C) 2026 Die Hauskern-Autoren
# SPDX-License-Identifier: GPL-3.0-or-later – freie Software OHNE JEDE GEWÄHR, siehe LICENSE.txt
"""
wall.py – Wand-Display: Vollbild-Ansicht für ein Tablet an der Wand (http://<adresse>:8770/wand).

Uhr, Datum, Wetter, Termine, wer zu Hause ist, Einkaufsliste (abhakbar), Drucker, Szenen und Hinweise.
Nachts (22–6 Uhr) gedimmt mit großer Uhr; Antippen weckt für 2 Minuten. Gegen Einbrennen wandert
der Inhalt alle paar Minuten um ein paar Pixel. Nutzt dieselbe Anmeldung (PIN) wie die Handy-Oberfläche.
"""

MANIFEST = {"name": "Hauskern Wand", "short_name": "Hauskern Wand", "start_url": "/wand", "display": "fullscreen",
            "orientation": "landscape", "background_color": "#0e0f12", "theme_color": "#0e0f12",
            "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}

PAGE = r"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#0e0f12">
<link rel="manifest" href="/wand.webmanifest"><link rel="apple-touch-icon" href="/icon-512.png">
<title>{{APP}} · Wand</title>
<style>
:root{--bg:#0e0f12;--card:#191b21;--card2:#22252d;--fg:#f2f3f5;--mut:#8b919c;--acc:#4a8df8;--ok:#3ecf7a;--amb:#f2b04a;--bad:#f25c5c}
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%;background:var(--bg);color:var(--fg);font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow:hidden;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
#wrap{height:100%;padding:calc(env(safe-area-inset-top) + 2vh) 2.2vw calc(env(safe-area-inset-bottom) + 2vh);display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:1.6vw;transition:transform 2s,filter 1.5s}
.col{display:flex;flex-direction:column;gap:1.6vh;min-height:0}
.card{background:var(--card);border-radius:18px;padding:1.6vh 1.4vw;min-height:0;overflow:hidden}
.h{font-size:clamp(11px,1.15vw,16px);color:var(--mut);text-transform:uppercase;letter-spacing:.08em;margin-bottom:1vh;font-weight:600}
#clock{font-size:clamp(64px,10.5vw,170px);font-weight:200;line-height:.95;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
#date{font-size:clamp(18px,2.2vw,34px);color:var(--mut);margin-top:.6vh}
.wx{display:flex;align-items:center;gap:1.2vw;margin-top:2vh}.wx .e{font-size:clamp(44px,5.5vw,90px);line-height:1}.wx .t{font-size:clamp(36px,4.2vw,70px);font-weight:300}.wx .d{font-size:clamp(14px,1.4vw,22px);color:var(--mut)}
.days{display:flex;justify-content:space-between;margin-top:2vh;gap:.6vw}.days div{text-align:center;flex:1;font-size:clamp(12px,1.25vw,19px)}.days .e{font-size:clamp(22px,2.4vw,38px);margin:.4vh 0}.days .m{color:var(--mut)}
.warn{margin-top:1.4vh;color:var(--amb);font-size:clamp(13px,1.35vw,20px)}
.ag{display:flex;gap:.8vw;align-items:baseline;padding:.55vh 0;font-size:clamp(14px,1.45vw,22px)}.ag .c{width:.6vw;min-width:6px;height:.6vw;min-height:6px;border-radius:50%;flex:none;transform:translateY(-.15em)}.ag .tm{color:var(--mut);min-width:4.2em;font-variant-numeric:tabular-nums}.day{font-size:clamp(12px,1.15vw,17px);color:var(--acc);margin-top:.8vh;font-weight:600}
.ppl{display:flex;flex-wrap:wrap;gap:.6vw}.pp{background:var(--card2);border-radius:999px;padding:.6vh 1vw;font-size:clamp(14px,1.4vw,21px);display:flex;align-items:center;gap:.5vw}.pp i{width:.7vw;min-width:8px;height:.7vw;min-height:8px;border-radius:50%;background:var(--mut)}.pp.on i{background:var(--ok)}.pp:not(.on){color:var(--mut)}
.li{display:flex;align-items:center;gap:.9vw;padding:.75vh 0;font-size:clamp(15px,1.55vw,24px);border-bottom:1px solid var(--card2);cursor:pointer}.li:last-child{border:0}.li .ck{width:1.5vw;min-width:18px;height:1.5vw;min-height:18px;border:2px solid var(--mut);border-radius:50%;flex:none}.li.d{opacity:.35;text-decoration:line-through}.li.d .ck{background:var(--ok);border-color:var(--ok)}
.pr .bar{height:1vh;min-height:8px;background:var(--card2);border-radius:99px;overflow:hidden;margin:.8vh 0}.pr .bar i{display:block;height:100%;background:var(--ok);border-radius:99px}
.pr .n{font-size:clamp(15px,1.55vw,24px);font-weight:600}.pr .s{font-size:clamp(13px,1.3vw,20px);color:var(--mut)}.pr+.pr{margin-top:1.4vh}
.sc{display:flex;flex-wrap:wrap;gap:.6vw}.sc button{background:var(--card2);color:var(--fg);border:0;border-radius:14px;padding:1.2vh 1.2vw;font-size:clamp(14px,1.4vw,21px);font-family:inherit}.sc button:active{background:var(--acc)}
.nt{font-size:clamp(14px,1.4vw,21px);padding:.5vh 0}.nt.b{color:var(--bad)}.nt.a{color:var(--amb)}
.grow{flex:1}.mut{color:var(--mut);font-size:clamp(13px,1.25vw,19px)}
#foot{position:fixed;right:2vw;bottom:calc(env(safe-area-inset-bottom) + 1vh);font-size:12px;color:var(--mut)}
#toast{position:fixed;left:50%;bottom:6vh;transform:translateX(-50%);background:var(--card2);padding:1.4vh 2vw;border-radius:14px;font-size:clamp(14px,1.4vw,20px);display:none}
body.night #wrap{filter:brightness(.38)}body.night .day-only{display:none!important}body.night #wrap{grid-template-columns:1fr}body.night #c1{align-items:center;justify-content:center;text-align:center}body.night .wx{justify-content:center}
#login{position:fixed;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;gap:3vh;background:var(--bg)}
#pin{font-size:42px;letter-spacing:.4em;height:52px}.pad{display:grid;grid-template-columns:repeat(3,96px);gap:14px}.pad button{height:84px;border-radius:50%;border:0;background:var(--card2);color:var(--fg);font-size:30px;font-family:inherit}
@media (orientation:portrait){#wrap{grid-template-columns:1fr;overflow-y:auto}html,body{overflow:auto}}
</style></head><body>
<div id="wrap">
 <div class="col" id="c1"><div><div id="clock">--:--</div><div id="date"></div></div><div id="wx"></div>
  <div class="card day-only grow" id="cag"><div class="h">Termine</div><div id="ag"></div></div></div>
 <div class="col day-only"><div class="card"><div class="h">Zuhause</div><div class="ppl" id="ppl"></div></div>
  <div class="card grow" id="clist"><div class="h" id="lh">🛒 Einkaufsliste</div><div id="list"></div></div></div>
 <div class="col day-only"><div class="card" id="cpr"><div class="h">3D-Druck</div><div id="pr"></div></div>
  <div class="card" id="csc"><div class="h">Szenen</div><div class="sc" id="sc"></div></div>
  <div class="card grow" id="cnt"><div class="h">Hinweise</div><div id="nt"></div></div></div>
</div>
<div id="foot"></div><div id="toast"></div>
<div id="login"><div style="font-size:22px">🏠 {{APP}} – Wand-Display</div><div class="mut">PIN eingeben (am besten einen eigenen Nutzer „Wand“ anlegen)</div><div id="pin"></div>
 <div class="pad" id="pad"></div></div>
<script>
const $=s=>document.querySelector(s);let S=null,LIST=null,wakeUntil=0,pin="";
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const R=a=>(S&&S.me&&S.me.rights&&S.me.rights[a])||0;
function toast(t){const e=$("#toast");e.textContent=t;e.style.display="block";clearTimeout(e._t);e._t=setTimeout(()=>e.style.display="none",2500)}
async function api(p,b){const r=await fetch(p,b?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b),credentials:"same-origin"}:{credentials:"same-origin"});if(r.status==401){showLogin();throw "login"}return r}
function tick(){const d=new Date();$("#clock").textContent=d.toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"});
 $("#date").textContent=d.toLocaleDateString("de-DE",{weekday:"long",day:"numeric",month:"long"});
 const h=d.getHours(),night=(h>=22||h<6)&&Date.now()>wakeUntil;document.body.classList.toggle("night",night)}
function wx(){const W=S.weather;if(!W){$("#wx").innerHTML="";return}const c=W.current;
 $("#wx").innerHTML=`<div class="wx"><div class="e">${W.emoji}</div><div><div class="t">${Math.round(c.temp)}°</div><div class="d">${esc(W.text)}${W.place?" · "+esc(W.place):""}</div></div></div>
 <div class="days day-only">${W.days.slice(1,5).map(d=>`<div><div class="m">${esc(d.label)}</div><div class="e">${d.emoji}</div><div>${Math.round(d.max)}° <span class="m">${Math.round(d.min)}°</span></div></div>`).join("")}</div>${W.warn?`<div class="warn">${esc(W.warn)}</div>`:""}`}
function agenda(){const A=S.agenda||[];$("#cag").style.display=R("home")?"":"none";if(!A.length){$("#ag").innerHTML='<div class="mut">Keine Termine</div>';return}let h="",ld=null;
 for(const e of A.slice(0,8)){if(e.day!==ld){h+=`<div class="day">${esc(e.day)}</div>`;ld=e.day}h+=`<div class="ag"><span class="c" style="background:${esc(e.color||"#4a8df8")}"></span><span class="tm">${esc(e.time)}</span><span>${esc(e.title)}</span></div>`}$("#ag").innerHTML=h}
function people(){const P=(S.persons||[]).filter(p=>!p.item);$("#ppl").parentNode.style.display=P.length?"":"none";
 $("#ppl").innerHTML=P.map(p=>`<div class="pp ${p.home?"on":""}"><i></i>${esc(p.name)}</div>`).join("")}
async function loadList(){if(!R("lists"))return;try{const j=await (await api("/api/lists")).json();LIST=j.lists;renderList()}catch(e){}}
function renderList(){$("#clist").style.display=R("lists")?"":"none";if(!LIST)return;const L=LIST.einkauf;const open=L.items.filter(i=>!i.done);
 $("#lh").textContent=`🛒 ${L.name}${open.length?" · "+open.length:""}`;
 $("#list").innerHTML=open.slice(0,12).map(i=>`<div class="li" onclick="done('${i.id}')"><span class="ck"></span><span>${esc(i.text)}</span></div>`).join("")+(open.length>12?`<div class="mut">… und ${open.length-12} weitere</div>`:"")||'<div class="mut">Nichts auf der Liste 👍</div>';
 const T=LIST.aufgaben?LIST.aufgaben.items.filter(i=>!i.done):[];if(T.length)$("#list").innerHTML+=`<div class="h" style="margin-top:1.6vh">✅ Aufgaben · ${T.length}</div>`+T.slice(0,5).map(i=>`<div class="li" onclick="done('${i.id}','aufgaben')"><span class="ck"></span><span>${esc(i.text)}</span></div>`).join("")}
async function done(id,list){if(R("lists")<2){toast("Abhaken darf dieser Nutzer nicht");return}const j=await (await api("/api/lists",{op:"done",list:list||"einkauf",id})).json();if(j.lists){LIST=j.lists;renderList();toast("✓ "+j.msg.replace(/^✓ /,""))}}
function printers(){const P=S.printers||[];$("#cpr").style.display=P.length&&R("printers")?"":"none";
 $("#pr").innerHTML=P.map(p=>{const run=["RUNNING","PAUSE","PREPARE"].includes(p.state);return `<div class="pr"><div class="n">${esc(p.name)}</div>${run?`<div class="bar"><i style="width:${p.percent||0}%;${p.state=="PAUSE"?"background:var(--amb)":""}"></i></div>`:""}<div class="s">${!p.connected?"nicht verbunden":run?`${p.percent||0} %${p.eta?" · fertig "+esc(p.eta):""}${p.state=="PAUSE"?" · pausiert":""}`:esc(p.state_text||"bereit")}</div>${p.spaghetti?'<div class="s" style="color:var(--bad)">🍝 möglicher Fehldruck</div>':""}</div>`}).join("")}
function scenes(){const X=S.scenes||[];$("#csc").style.display=X.length&&R("smart")>=2?"":"none";
 $("#sc").innerHTML=X.slice(0,8).map(s=>`<button onclick="scene('${s.key}','${esc(s.name).replace(/'/g,"")}')">▶ ${esc(s.name)}</button>`).join("")}
async function scene(key,name){const j=await (await api("/api/action",{key,action:"run"})).json();toast(j.ok?"▶ "+name+" gestartet":"⚠ "+(j.msg||"Fehler"))}
function notes(){const n=[];const C=S.calls;if(C&&C.missed_today)n.push(["b",`📞 ${C.missed_today} verpasste${C.missed_today>1?" Anrufe":"r Anruf"} heute`+(C.last?` · zuletzt ${esc(C.last.who)}`:"")]);
 if(S.net&&S.net.online===false)n.push(["b","🌐 Internet ist weg"]);
 const B=S.battery;if(B&&B.low)n.push(["a",`🔋 ${B.low} Batterie${B.low>1?"n":""} schwach`+(B.worst?` · ${esc(B.worst.name)}`:"")]);
 const F=S.filament;if(F&&F.low&&F.low.length)n.push(["a",`🧵 Filament knapp: ${esc(F.low.slice(0,2).join(", "))}`]);
 (S.printers||[]).forEach(p=>{if(p.state=="FINISH")n.push(["",`✅ ${esc(p.name)}: Druck fertig`])});
 if(S.guest&&S.guest.enabled)n.push(["",`📶 Gast-WLAN ist an`]);
 $("#nt").innerHTML=n.map(x=>`<div class="nt ${x[0]}">${x[1]}</div>`).join("")||'<div class="mut">Alles ruhig 😊</div>'}
function render(){wx();agenda();people();printers();scenes();notes();renderList();$("#foot").textContent="aktualisiert "+new Date().toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"})}
let VER=null;
async function load(){try{const r=await api("/api/state");if(!r.ok)throw r.status;S=await r.json();if(VER&&S.version&&S.version!==VER)location.reload();VER=S.version;$("#login").style.display="none";render();loadList()}
 catch(e){if(e!=="login")$("#foot").textContent="⚠ keine Verbindung – letzter Stand"}}
function showLogin(){$("#login").style.display="flex";pin="";$("#pin").textContent="";
 $("#pad").innerHTML=[1,2,3,4,5,6,7,8,9,"⌫",0,"OK"].map(k=>`<button onclick="pk('${k}')">${k}</button>`).join("")}
async function pk(k){if(k==="⌫")pin=pin.slice(0,-1);else if(k==="OK"){const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin}),credentials:"same-origin"});
  if(r.ok){$("#login").style.display="none";load()}else{const j=await r.json().catch(()=>({}));toast(j.need_code?"Dieser Nutzer hat Zwei-Faktor – fürs Wand-Display bitte einen eigenen Nutzer ohne anlegen":"PIN stimmt nicht");pin=""}}else if(pin.length<12)pin+=k;$("#pin").textContent="•".repeat(pin.length)}
document.addEventListener("click",()=>{if(document.body.classList.contains("night")){wakeUntil=Date.now()+120000;tick()}},true);
setInterval(()=>{const x=Math.round(Math.random()*12-6),y=Math.round(Math.random()*12-6);$("#wrap").style.transform=`translate(${x}px,${y}px)`},300000);
tick();setInterval(tick,1000);load();setInterval(load,15000);setTimeout(()=>location.reload(),6*3600*1000);
</script></body></html>"""
