"use strict";

const API_BASE = (() => {
  const saved = localStorage.getItem('ow_api_base');
  if (saved) return saved.replace(/\/$/, '');
  if (window.location.protocol.startsWith('http')) return window.location.origin;
  return 'http://192.168.0.111:8001';
})();

/* ═══ STATE ═══ */
const S = {
  devices: new Map,
  networks: new Map,
  prevMACs: new Set,
  firstSeen: new Map,
  pktHist: new Map,
  sigHist: new Map,
  prevDistances: new Map,
  prevPktTotal: 0,
  log: [],
  insights: [],
  t0: Date.now(),
  filter: 'all',
  query: '',
  tick: 0,
  ready: false,
  selected: null,
  failCount: 0,
  lastPollOk: true,
  countHistory: [],
  weather: null,
  sunData: null,
  lastInsightTick: 0,
  aliases: {},
  soundEnabled: false,
  audioCtx: null,
  pollLatency: 0,
  lastDataTs: 0,
  watchlist: {},
  _mapInit: false,
};

/* ═══ DEVICE ALIASES (localStorage) ═══ */
try { S.aliases = JSON.parse(localStorage.getItem('ow_aliases') || '{}'); } catch(e) { S.aliases = {}; }
function getAlias(mac) { return S.aliases[mac] || ''; }
function saveAlias(mac, alias) {
  alias = alias.trim();
  if (alias) S.aliases[mac] = alias; else delete S.aliases[mac];
  try { localStorage.setItem('ow_aliases', JSON.stringify(S.aliases)); } catch(e) {}
}

/* ═══ WATCHLIST (localStorage) ═══ */
try { S.watchlist = JSON.parse(localStorage.getItem('ow_watchlist') || '{}'); } catch(e) { S.watchlist = {}; }
function getWatch(mac) { return S.watchlist[mac] || ''; }
function setWatch(mac, tag) {
  if (tag) S.watchlist[mac] = tag; else delete S.watchlist[mac];
  try { localStorage.setItem('ow_watchlist', JSON.stringify(S.watchlist)); } catch(e) {}
}

/* ═══ SOUND ALERT ═══ */
function playProximityTone() {
  if (!S.soundEnabled) return;
  try {
    if (!S.audioCtx) S.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = S.audioCtx, osc = ctx.createOscillator(), gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = 'sine'; osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.15);
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.start(ctx.currentTime); osc.stop(ctx.currentTime + 0.3);
  } catch(e) {}
}

/* ═══ EXPORT ═══ */
function exportCSV() {
  const devs = [...S.devices.values()];
  const alias = mac => getAlias(mac);
  const header = 'MAC,Alias,Vendor,Type,Signal,Distance,Packets,BSSID,ESSID,Associated,Probed SSIDs,Duration,Active\n';
  const rows = devs.map(d => [
    d.mac, '"'+(alias(d.mac)||'')+'"', '"'+(d.vendor||'')+'"', d._type||d.type||'',
    d.signal, d.distance, d.packets||0, d.bssid||'', '"'+(d.essid||'')+'"',
    d._isAssociated?'yes':'no', '"'+(d.probed_ssids||[]).join(';')+'"',
    d.duration||0, d.active?'yes':'no'
  ].join(',')).join('\n');
  const blob = new Blob([header + rows], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'overwatch_devices_' + new Date().toISOString().slice(0,19).replace(/:/g,'-') + '.csv';
  a.click(); URL.revokeObjectURL(a.href);
}

/* ═══ CAMERA ═══ */
!function(){
  const v=document.getElementById('video'),b=document.getElementById('cam-badge'),src='stream.m3u8';
  const ok=()=>{b.textContent='LIVE';b.style.cssText='background:var(--green-10);color:var(--green);border:1px solid rgba(0,230,118,.2)'};
  const fail=()=>{b.textContent='OFFLINE';b.style.cssText='background:var(--red-10);color:var(--red);border:1px solid rgba(255,61,113,.2)'};
  if(typeof Hls!=='undefined'&&Hls.isSupported()){const h=new Hls({enableWorker:true,lowLatencyMode:true});h.loadSource(src);h.attachMedia(v);h.on(Hls.Events.MANIFEST_PARSED,ok);h.on(Hls.Events.ERROR,(_,d)=>{if(d.fatal)fail()})}
  else if(v.canPlayType('application/vnd.apple.mpegurl')){v.src=src;v.addEventListener('playing',ok);v.addEventListener('error',fail)}
  else fail();
}();

/* ═══ CLOCK ═══ */
setInterval(()=>{
  const n=new Date,ts=n.toLocaleTimeString('en-US',{hour12:false});
  document.getElementById('clock').textContent=ts;
  document.getElementById('cam-time').textContent=ts;
  const e=Math.floor((Date.now()-S.t0)/1e3);
  document.getElementById('uptime').textContent=`UP ${String(e/3600|0).padStart(2,'0')}:${String((e%3600)/60|0).padStart(2,'0')}:${String(e%60).padStart(2,'0')}`;
},1000);

/* ═══ HELPERS ═══ */
const TL={phone:'Phone',laptop:'Laptop',iot:'IoT',infra:'Infra',random:'Random',unknown:'Unknown'};
const TC={phone:'dt-phone',laptop:'dt-laptop',iot:'dt-iot',infra:'dt-infra',random:'dt-random',unknown:'dt-unknown'};

function isRealBssid(bssid) {
  return bssid && bssid !== '(not associated)' && /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/.test(bssid);
}

function classify(vendor, mac, type) {
  if (type === 'random') return 'random';
  const v = (vendor||'').toLowerCase();
  if (/apple|samsung|huawei|xiaomi|oneplus|google|pixel|oppo|vivo|motorola|realme/i.test(v)) return 'phone';
  if (/dell|lenovo|hp\b|hewlett|intel|microsoft|asus|acer|razer/i.test(v)) return 'laptop';
  if (/amazon|ring|nest|ecobee|tuya|espressif|shenzhen|tp-link|sonos|roku|wyze|vizio|broadlink/i.test(v)) return 'iot';
  if (/cisco|ubiquiti|aruba|netgear|linksys|meraki|mikrotik|ruckus|cambium/i.test(v)) return 'infra';
  if (parseInt(mac.split(':')[0],16) & 0x02) return 'random';
  return 'unknown';
}

function sigClass(r){return r>-45?'sig-strong':r>-65?'sig-medium':'sig-weak'}
function sigHTML(r){if(r===-1)return'<span style="color:var(--t4);font-family:var(--mono);font-size:9px">n/a</span>';const c=sigClass(r);return`<div class="sig-bars ${c}">${[3,5,8,11,14].map(h=>`<div class="sig-tick" style="height:${h}px"></div>`).join('')}</div>`}
function ago(ts,now){const d=now-ts;if(d<5)return'Now';if(d<60)return(d|0)+'s';if(d<3600)return(d/60|0)+'m';return(d/3600|0)+'h '+(d%3600/60|0)+'m'}
function fmtDuration(s){if(s<60)return s+'s';if(s<3600)return(s/60|0)+'m '+(s%60)+'s';return(s/3600|0)+'h '+(s%3600/60|0)+'m'}

function encClass(enc) {
  if (!enc) return 'enc-open';
  const e = enc.toUpperCase();
  if (e.includes('WPA3') || e.includes('SAE')) return 'enc-wpa3';
  if (e.includes('WPA')) return 'enc-wpa2';
  if (e.includes('WEP')) return 'enc-wep';
  if (e.includes('OPN')) return 'enc-open';
  return 'enc-wpa2';
}

/* ═══ CONNECTION HEALTH ═══ */
function updateConnHealth() {
  const el = document.getElementById('conn-health');
  const statusEl = document.getElementById('cmd-status');
  const dotEl = document.getElementById('cmd-dot');
  const textEl = document.getElementById('cmd-status-text');

  if (S.failCount >= 3) {
    el.className = 'conn-health disconnected';
    el.textContent = 'DISCONNECTED (' + S.failCount + ' failures)';
    statusEl.style.cssText = 'display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:1px;text-transform:uppercase;color:var(--red);padding:3px 10px;border-radius:4px;background:var(--red-10);border:1px solid rgba(255,61,113,.15)';
    dotEl.style.background = 'var(--red)';
    textEl.textContent = 'Offline';
  } else if (S.failCount >= 1) {
    el.className = 'conn-health degraded';
    el.textContent = 'RETRYING\u2026';
    statusEl.style.cssText = 'display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:1px;text-transform:uppercase;color:var(--amber);padding:3px 10px;border-radius:4px;background:var(--amber-10);border:1px solid rgba(255,167,38,.15)';
    dotEl.style.background = 'var(--amber)';
    textEl.textContent = 'Degraded';
  } else {
    el.className = 'conn-health';
    el.textContent = '';
    statusEl.style.cssText = '';
    dotEl.style.background = '';
    textEl.textContent = 'Monitoring';
  }
}

/* ═══ LOG ═══ */
function logEv(type, msg) {
  S.log.unshift({type, msg, t: new Date().toLocaleTimeString('en-US',{hour12:false})});
  if (S.log.length > 80) S.log.length = 80;
}

function renderFeed() {
  const el = document.getElementById('feed');
  const serverEvts = (S._lastServerEvents || []).slice(0, 40);

  if (serverEvts.length > 0) {
    const icons = {device_join:'\u{1F4E1}',device_leave:'\u{1F50C}',close_alert:'\u{1F6A8}',new_ap:'\u{1F4E1}',burst:'\u26A1',move:'\u{1F3C3}'};
    const evCls = {device_join:'ev-join',device_leave:'ev-leave',close_alert:'ev-move',new_ap:'ev-probe',burst:'ev-join',move:'ev-move'};
    const labels = {device_join:'Joined',device_leave:'Left',close_alert:'Close',new_ap:'New AP',burst:'Burst'};
    el.innerHTML = serverEvts.map(e => {
      const dt = new Date(e.ts * 1000);
      const t = dt.toLocaleTimeString('en-US',{hour12:false});
      const alias = e.mac ? getAlias(e.mac) : '';
      const macDisp = alias || (e.mac ? e.mac.substring(0,11)+'\u2026' : '');
      return `<div class="feed-item"><div class="feed-ico ${evCls[e.type]||''}">${icons[e.type]||'\u2022'}</div><div class="feed-body"><div class="feed-msg"><b>${macDisp}</b> ${labels[e.type]||e.type} ${e.detail ? '\u00B7 '+e.detail : ''}</div><div class="feed-ts">${t}</div></div></div>`;
    }).join('');
  } else {
    const ic = {join:'\u{1F4E1}', leave:'\u{1F50C}', probe:'\u{1F50D}', move:'\u{1F3C3}'};
    el.innerHTML = S.log.slice(0,30).map(e =>
      `<div class="feed-item"><div class="feed-ico ev-${e.type}">${ic[e.type]||'\u2022'}</div><div class="feed-body"><div class="feed-msg">${e.msg}</div><div class="feed-ts">${e.t}</div></div></div>`
    ).join('') || '<div style="padding:16px;text-align:center;color:var(--t4);font-size:10px">No events yet \u2014 waiting for data</div>';
  }
}

/* ═══ INSIGHTS ENGINE ═══ */
function generateInsights(devs, nets) {
  if (S.tick - S.lastInsightTick < 5 && S.insights.length > 0) return;
  S.lastInsightTick = S.tick;
  const insights = [];
  const now = Date.now() / 1000;

  const typeCounts = {};
  devs.forEach(d => { typeCounts[d._type] = (typeCounts[d._type]||0) + 1; });
  const typeList = Object.entries(typeCounts).sort((a,b)=>b[1]-a[1]).map(([t,c])=>`<b>${c}</b> ${TL[t]||t}`).join(', ');
  if (devs.length > 0) insights.push({icon:'i-info', msg:`Device mix: ${typeList}`});

  const randoms = devs.filter(d => d._type === 'random');
  if (randoms.length > 0) insights.push({icon:'i-info', msg:`<b>${randoms.length}</b> device${randoms.length>1?'s':''} using randomized MACs \u2014 likely phones with privacy enabled`});

  const probing = devs.filter(d => !d._isAssociated && d.probed_ssids && d.probed_ssids.length > 0);
  if (probing.length > 0) {
    const allProbed = new Set();
    probing.forEach(d => (d.probed_ssids||[]).forEach(s => { if(s) allProbed.add(s); }));
    if (allProbed.size > 0) insights.push({icon:'i-warn', msg:`<b>${probing.length}</b> unassociated device${probing.length>1?'s':''} probing for: ${[...allProbed].slice(0,4).map(s=>`<b>${s}</b>`).join(', ')}${allProbed.size>4?' + '+(allProbed.size-4)+' more':''}`});
  }

  const longStay = devs.filter(d => d.duration > 1800);
  if (longStay.length > 0) insights.push({icon:'i-ok', msg:`<b>${longStay.length}</b> device${longStay.length>1?'s':''} present for 30+ minutes \u2014 likely stationary/resident`});

  const recent = devs.filter(d => d._firstSeen && (now - d._firstSeen) < 120);
  if (recent.length > 0 && S.tick > 5) insights.push({icon:'i-alert', msg:`<b>${recent.length}</b> device${recent.length>1?'s':''} appeared in the last 2 minutes`});

  const veryClose = devs.filter(d => d.signal > -40 && d.signal !== -1);
  if (veryClose.length > 0) insights.push({icon:'i-alert', msg:`<b>${veryClose.length}</b> device${veryClose.length>1?'s':''} within very close range (RSSI > -40) \u2014 within ~3m`});

  const noSignal = devs.filter(d => d.signal === -1);
  if (noSignal.length > 0) insights.push({icon:'i-info', msg:`<b>${noSignal.length}</b> device${noSignal.length>1?'s':''} with no signal data \u2014 may be out of range or dormant`});

  const netCounts = {};
  devs.forEach(d => { if (d.essid) netCounts[d.essid] = (netCounts[d.essid]||0) + 1; });
  const sorted = Object.entries(netCounts).sort((a,b) => b[1] - a[1]);
  if (sorted.length > 0 && sorted[0][1] > 1) insights.push({icon:'i-info', msg:`Most popular: <b>${sorted[0][0]}</b> (${sorted[0][1]} clients)${sorted.length>1?' \u00B7 2nd: <b>'+sorted[1][0]+'</b> ('+sorted[1][1]+')':''}`});

  const assocPct = devs.length ? Math.round(devs.filter(d=>d._isAssociated).length / devs.length * 100) : 0;
  if (devs.length > 3) insights.push({icon:'i-info', msg:`<b>${assocPct}%</b> of devices are associated to a network \u00B7 <b>${100-assocPct}%</b> unassociated/probing`});

  const openNets = [...nets.values()].filter(n => { const p = (n.privacy||'').toUpperCase(); return p === '' || p === 'OPN' || p.includes('OPEN'); });
  if (openNets.length > 0) insights.push({icon:'i-alert', msg:`<b>${openNets.length}</b> open (unencrypted) network${openNets.length>1?'s':''} visible: ${openNets.slice(0,2).map(n=>`<b>${n.essid||'hidden'}</b>`).join(', ')}`});

  const highPkt = devs.filter(d => d.packets > 500).sort((a,b) => b.packets - a.packets);
  if (highPkt.length > 0) {
    const hp = highPkt[0], a = getAlias(hp.mac);
    insights.push({icon:'i-info', msg:`Highest traffic: <b>${a||hp.mac.substring(0,8)+'\u2026'}</b> with ${hp.packets.toLocaleString()} packets${hp.essid ? ' on <b>'+hp.essid+'</b>' : ''}`});
  }

  const chanCounts = {};
  [...nets.values()].forEach(n => { if(n.channel) chanCounts[n.channel] = (chanCounts[n.channel]||0) + 1; });
  const congested = Object.entries(chanCounts).filter(([_,c]) => c >= 3).sort((a,b) => b[1] - a[1]);
  if (congested.length > 0) insights.push({icon:'i-warn', msg:`Channel <b>${congested[0][0]}</b> is congested with <b>${congested[0][1]}</b> networks \u2014 may cause interference`});

  if (S._lastHistory && S._lastHistory.total_ever_seen > devs.length) {
    const returning = S._lastRegistry ? S._lastRegistry.returning_this_week : 0;
    if (returning > 0) insights.push({icon:'i-ok', msg:`<b>${returning}</b> returning device${returning>1?'s':''} this week out of <b>${S._lastHistory.total_ever_seen}</b> total ever seen`});
  }

  const presResidents = devs.filter(d => d.presence === 'resident');
  const presAnomalies = devs.filter(d => d.presence === 'anomaly');
  const presFamiliar = devs.filter(d => d.presence === 'resident' || d.presence === 'regular');
  const presUnknown = devs.filter(d => d.presence !== 'resident' && d.presence !== 'regular');
  if (devs.length > 0) {
    const famPct = Math.round(presFamiliar.length / devs.length * 100);
    insights.push({icon: famPct >= 60 ? 'i-ok' : 'i-warn', msg: `<b>${famPct}%</b> familiar (${presFamiliar.length} known) \u00B7 <b>${100-famPct}%</b> unknown (${presUnknown.length} new/passing)`});
  }
  if (presAnomalies.length > 0) insights.push({icon:'i-alert', msg:`<b>${presAnomalies.length}</b> anomalous device${presAnomalies.length>1?'s':''} \u2014 new + high threat score`});

  if (S.weather && S.weather.main) {
    const rain = S.weather.rain ? S.weather.rain['1h'] || S.weather.rain['3h'] : 0;
    if (rain > 0) insights.push({icon:'i-warn', msg:`Rain detected (${rain}mm) \u2014 WiFi signal attenuation likely`});
    if (S.weather.main.humidity > 85) insights.push({icon:'i-info', msg:`High humidity (${S.weather.main.humidity}%) may cause minor 2.4GHz signal degradation`});
  }

  S.insights = insights;
}

function renderInsights() {
  const el = document.getElementById('insights-list');
  document.getElementById('insight-n').textContent = S.insights.length;
  el.innerHTML = S.insights.map(i =>
    `<div class="insight"><div class="insight-icon ${i.icon}">${i.icon==='i-info'?'\u{1F4CA}':i.icon==='i-warn'?'\u26A0\uFE0F':i.icon==='i-alert'?'\u{1F6A8}':'\u2705'}</div><div class="insight-body"><div class="insight-msg">${i.msg}</div></div></div>`
  ).join('') || '<div style="padding:16px;text-align:center;color:var(--t4);font-size:11px">Collecting data\u2026</div>';
}

/* ═══ RADAR ═══ */
function renderRadar(devs) {
  const el = document.getElementById('radar-blips'), active = devs.filter(d => d.active && d.signal !== -1);
  document.getElementById('radar-n').textContent = active.length + ' blips';
  const existing = new Map; el.querySelectorAll('.radar-blip').forEach(b => existing.set(b.dataset.mac, b));
  const current = new Set;
  active.forEach(d => {
    current.add(d.mac);
    const norm = Math.max(0, Math.min(1, (d.signal + 90) / 55)), r = (1-norm) * 84;
    let h = 0; for (let i = 0; i < d.mac.length; i++) h = ((h<<5)-h+d.mac.charCodeAt(i))|0;
    const a = (Math.abs(h)%360)*(Math.PI/180), x = 100+Math.cos(a)*r, y = 100+Math.sin(a)*r;
    let b = existing.get(d.mac);
    if (!b) {b = document.createElement('div'); b.className='radar-blip'; b.dataset.mac=d.mac; el.appendChild(b)}
    b.className = `radar-blip ${d.distance}`; b.style.left=x+'px'; b.style.top=y+'px'; b.title=`${d.mac}\n${d.signal} dBm\n${d.essid||'unassociated'}`;
  });
  existing.forEach((b,mac) => { if (!current.has(mac)) b.remove() });
}

/* ═══ NETWORKS LIST ═══ */
function renderNetworks() {
  const el = document.getElementById('nets-list');
  const nets = [...S.networks.values()].sort((a,b) => (b.power||0) - (a.power||0));
  document.getElementById('nets-n').textContent = nets.length;
  document.getElementById('m-nets').textContent = nets.length;
  const clientCounts = {};
  S.devices.forEach(d => { if (isRealBssid(d.bssid)) clientCounts[d.bssid] = (clientCounts[d.bssid]||0) + 1; });
  el.innerHTML = nets.map(n => {
    const clients = clientCounts[n.bssid] || 0;
    return `<div class="net-row" title="${n.bssid}\nCh ${n.channel} \u00B7 ${n.privacy} ${n.cipher}\n${clients} clients">
      <div class="net-essid">${n.essid || '<hidden>'}</div>
      <div class="net-ch">${n.channel||'?'}</div>
      <div class="net-enc ${encClass(n.privacy)}">${(n.privacy||'OPN').replace(/\s/g,'')}</div>
      <div class="net-clients">${clients}</div>
    </div>`;
  }).join('') || '<div style="padding:12px;text-align:center;color:var(--t4);font-size:10px">No AP data \u2014 update parser to v2</div>';
}

/* ═══ DENSITY ═══ */
function renderDensity(c) {
  const f=document.getElementById('density-fill'),l=document.getElementById('density-level'),d=document.getElementById('density-desc');
  document.getElementById('density-n').textContent=c;
  let p,lb,col,txt;
  if(c<=5){p=12;lb='LOW';col='var(--green)';txt='Quiet'}
  else if(c<=15){p=35;lb='MODERATE';col='var(--amber)';txt='Normal'}
  else if(c<=30){p=65;lb='HIGH';col='var(--amber)';txt='Busy'}
  else{p=90;lb='SATURATED';col='var(--red)';txt='Dense'}
  f.style.width=p+'%';f.style.background=col;l.textContent=lb;l.style.color=col;d.textContent=txt;
}

function renderTypeBreakdown(devs) {
  const el = document.getElementById('type-breakdown');
  if (!el) return;
  const counts = {};
  devs.forEach(d => { counts[d._type] = (counts[d._type]||0) + 1; });
  const total = devs.length || 1;
  const colors = {phone:'var(--purple)',laptop:'var(--cyan)',iot:'var(--amber)',infra:'var(--green)',random:'var(--red)',unknown:'var(--t4)'};
  const sorted = Object.entries(counts).sort((a,b)=>b[1]-a[1]);

  el.innerHTML = sorted.map(([type, count]) => {
    const pct = Math.round(count / total * 100);
    return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-family:var(--mono);font-size:9px">
      <span style="width:50px;color:var(--t3)">${TL[type]||type}</span>
      <div style="flex:1;height:6px;background:var(--raised);border-radius:3px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${colors[type]||'var(--t4)'};border-radius:3px"></div></div>
      <span style="width:30px;text-align:right;color:${colors[type]||'var(--t4)'};font-weight:600">${count}</span>
    </div>`;
  }).join('') || '<div style="color:var(--t4);font-size:9px">No devices</div>';
}

function renderQuickStats(devs, nets) {
  const el = document.getElementById('quick-stats');
  if (!el) return;
  const active = devs.filter(d => d.active);
  const assoc = devs.filter(d => d._isAssociated);
  const probing = devs.filter(d => (d.probed_ssids||[]).filter(s=>s).length > 0);
  const tagged = devs.filter(d => getAlias(d.mac));
  const avgSig = devs.filter(d=>d.signal!==-1);
  const avg = avgSig.length ? Math.round(avgSig.reduce((a,d)=>a+d.signal,0)/avgSig.length) : '--';

  const newest = devs.reduce((best, d) => (!best || (d._firstSeen||0) > (best._firstSeen||0)) ? d : best, null);
  const newestStr = newest ? (getAlias(newest.mac) || newest.mac.substring(0,11)+'\u2026') : '--';

  const residents = devs.filter(d => d.presence === 'resident').length;
  const regulars = devs.filter(d => d.presence === 'regular').length;
  const passers = devs.filter(d => d.presence === 'passerby').length;
  const anomalies = devs.filter(d => d.presence === 'anomaly').length;
  const newDevs = devs.filter(d => d.presence === 'new').length;

  const rows = [
    ['Active', active.length, 'var(--green)'],
    ['Residents', residents, 'var(--cyan)'],
    ['Regulars', regulars, 'var(--green)'],
    ['Passers-by', passers, 'var(--t3)'],
    ['Anomalies', anomalies, 'var(--red)'],
    ['New', newDevs, 'var(--amber)'],
    ['Associated', assoc.length, 'var(--cyan)'],
    ['Probing', probing.length, 'var(--purple)'],
    ['Tagged', tagged.length, 'var(--amber)'],
    ['Avg RSSI', avg + (avg !== '--' ? ' dBm' : ''), 'var(--t2)'],
    ['APs Visible', nets.size, 'var(--purple)'],
    ['Newest', newestStr, 'var(--cyan)'],
  ];

  el.innerHTML = rows.map(([k,v,c]) =>
    `<div style="display:flex;justify-content:space-between;padding:3px 0;font-family:var(--mono);font-size:9px"><span style="color:var(--t4)">${k}</span><span style="color:${c};font-weight:600">${v}</span></div>`
  ).join('');
}

/* ═══ TOP CLOSEST ═══ */
function renderTop(devs) {
  const el = document.getElementById('top-list');
  const ranked = [...devs].filter(d=>d.signal!==-1).sort((a,b)=>b.signal-a.signal).slice(0,10);
  const rk = ['gold','silver','bronze','','','','','','',''];

  el.innerHTML = ranked.map((d,i) => {
    const c = d.distance==='close'?'var(--green)':d.distance==='medium'?'var(--amber)':'var(--red)';
    const alias = getAlias(d.mac);
    const mvmt = d.movement || 'unknown';
    let trend = '';
    if (mvmt === 'approaching') trend = '<span class="mv-arrow mv-approach" style="font-size:8px;margin-left:3px">\u25B2</span>';
    else if (mvmt === 'leaving') trend = '<span class="mv-arrow mv-leave" style="font-size:8px;margin-left:3px">\u25BC</span>';
    else if (mvmt === 'stationary') trend = '<span class="mv-arrow mv-static" style="font-size:8px;margin-left:3px">\u2500</span>';
    const distBadge = `<span style="font-family:var(--mono);font-size:7px;padding:1px 5px;border-radius:2px;background:${d.distance==='close'?'var(--green-10)':d.distance==='medium'?'var(--amber-10)':'var(--red-10)'};color:${c};font-weight:600;text-transform:uppercase;letter-spacing:.3px;margin-left:4px">${d.distance}</span>`;
    const durStr = d.duration > 60 ? ' \u00B7 ' + fmtDuration(d.duration) : '';
    return `<div class="top-item" data-mac="${d.mac}" style="cursor:pointer"><div class="top-rank ${rk[i]}">${i+1}</div><div class="top-body"><div class="top-mac">${alias ? '<span style="color:var(--cyan);font-weight:700">'+alias+'</span> <span style="color:var(--t4);font-size:8px">'+d.mac.substring(0,8)+'</span>' : d.mac}</div><div class="top-meta">${d.vendor||'Unknown'} \u00B7 ${d.essid||'no network'}${durStr}</div></div><div style="text-align:right"><div class="top-rssi" style="color:${c}">${d.signal}${trend}</div>${distBadge}</div></div>`;
  }).join('') || '<div style="padding:16px;text-align:center;color:var(--t4);font-size:10px">No devices with signal data</div>';
}

document.getElementById('top-list').addEventListener('click', function(e) {
  const item = e.target.closest('.top-item');
  if (item && item.dataset.mac) {
    S.selected = S.selected === item.dataset.mac ? null : item.dataset.mac;
    renderTable();
  }
});

/* ═══ SPARKLINE ═══ */
function renderSparkline() {
  const canvas = document.getElementById('sparkline');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const data = S.countHistory;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const max = Math.max(...data, 1), min = Math.min(...data, 0), range = max - min || 1;
  const step = w / (Math.max(data.length - 1, 1));
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(0,212,245,.4)');
  grad.addColorStop(1, 'rgba(0,212,245,.02)');
  ctx.beginPath(); ctx.moveTo(0, h);
  data.forEach((v, i) => { ctx.lineTo(i * step, h - ((v - min) / range) * (h - 2) - 1); });
  ctx.lineTo((data.length - 1) * step, h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  data.forEach((v, i) => { const x = i * step, y = h - ((v - min) / range) * (h - 2) - 1; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.strokeStyle = '#00d4f5'; ctx.lineWidth = 1.5; ctx.stroke();
  const lastY = h - ((data[data.length-1] - min) / range) * (h - 2) - 1;
  ctx.beginPath(); ctx.arc((data.length-1) * step, lastY, 2, 0, Math.PI * 2);
  ctx.fillStyle = '#00d4f5'; ctx.fill();
}

/* ═══ WEATHER ═══ */
const WEATHER_ICONS = {'01d':'\u2600\uFE0F','01n':'\u{1F311}','02d':'\u26C5','02n':'\u{1F311}','03d':'\u2601\uFE0F','03n':'\u2601\uFE0F','04d':'\u2601\uFE0F','04n':'\u2601\uFE0F','09d':'\u{1F327}\uFE0F','09n':'\u{1F327}\uFE0F','10d':'\u{1F326}\uFE0F','10n':'\u{1F327}\uFE0F','11d':'\u26C8\uFE0F','11n':'\u26C8\uFE0F','13d':'\u{1F328}\uFE0F','13n':'\u{1F328}\uFE0F','50d':'\u{1F32B}\uFE0F','50n':'\u{1F32B}\uFE0F'};

function windDir(deg) {
  const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  return dirs[Math.round(deg / 22.5) % 16];
}

async function fetchWeather() {
  try {
    const res = await fetch('https://wttr.in/Athens,GA?format=j1');
    if (!res.ok) throw new Error('wttr error');
    const data = await res.json();
    const cur = data.current_condition[0];
    S.weather = {
      main: { temp: parseFloat(cur.temp_F), feels_like: parseFloat(cur.FeelsLikeF), humidity: parseInt(cur.humidity), pressure: parseInt(cur.pressure) },
      wind: { speed: parseFloat(cur.windspeedMiles), deg: parseInt(cur.winddirDegree) },
      weather: [{ description: cur.weatherDesc[0].value, icon: '03d' }],
      visibility: parseInt(cur.visibility) * 1000,
      clouds: { all: parseInt(cur.cloudcover) },
      name: 'Athens, GA'
    };
    renderWeather(); renderCmdWeather();
  } catch(e) {
    console.error('[OW] Weather fetch failed:', e);
    document.getElementById('weather-content').innerHTML = '<div class="weather-loading">Weather unavailable \u2014 check connection</div>';
  }
}

async function fetchSunData() {
  try {
    const res = await fetch('https://api.sunrise-sunset.org/json?lat=33.9519&lng=-83.3576&formatted=0');
    if (!res.ok) throw new Error('Sun API error');
    const data = await res.json();
    if (data.status === 'OK') { S.sunData = data.results; renderWeather(); }
  } catch(e) { console.error('[OW] Sun data failed:', e); }
}

function fmtTime12(iso) {
  const d = new Date(iso); let h = d.getHours(), m = d.getMinutes();
  const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
  return h + ':' + String(m).padStart(2,'0') + ' ' + ap;
}

function renderCmdWeather() {
  const el = document.getElementById('cmd-weather');
  if (!S.weather || !S.weather.main) { el.innerHTML = ''; return; }
  const w = S.weather;
  const ico = (w.weather && w.weather[0]) ? (WEATHER_ICONS[w.weather[0].icon] || '\u{1F321}\uFE0F') : '\u{1F321}\uFE0F';
  el.innerHTML = `<span class="cmd-weather-icon">${ico}</span><span class="cmd-weather-temp">${Math.round(w.main.temp)}\u00B0F</span>`;
}

function renderWeather() {
  const el = document.getElementById('weather-content');
  if (!S.weather || !S.weather.main) return;
  const w = S.weather;
  const ico = (w.weather && w.weather[0]) ? (WEATHER_ICONS[w.weather[0].icon] || '\u{1F321}\uFE0F') : '\u{1F321}\uFE0F';
  const desc = (w.weather && w.weather[0]) ? w.weather[0].description : 'Unknown';
  const vis = w.visibility ? (w.visibility / 1609.34).toFixed(1) + ' mi' : 'n/a';
  let sunHTML = '';
  if (S.sunData) {
    sunHTML = `<div class="weather-sun">
      <div class="weather-sun-item"><span>\u{1F305}</span> ${fmtTime12(S.sunData.sunrise)}</div>
      <div class="weather-sun-item"><span>\u{1F307}</span> ${fmtTime12(S.sunData.sunset)}</div>
      <div class="weather-sun-item"><span>\u2600\uFE0F</span> ${((new Date(S.sunData.sunset) - new Date(S.sunData.sunrise)) / 3600000).toFixed(1)}h daylight</div>
    </div>`;
  }
  el.innerHTML = `<div class="weather-box">
    <div class="weather-current">
      <div class="weather-icon">${ico}</div>
      <div class="weather-temp">${Math.round(w.main.temp)}<span class="weather-temp-unit">\u00B0F</span></div>
      <div class="weather-desc">
        <div class="weather-condition">${desc}</div>
        <div class="weather-detail">Feels like ${Math.round(w.main.feels_like)}\u00B0F</div>
      </div>
    </div>
    <div class="weather-grid">
      <div class="weather-stat"><span class="weather-stat-label">Humidity</span><span class="weather-stat-val">${w.main.humidity}%</span></div>
      <div class="weather-stat"><span class="weather-stat-label">Wind</span><span class="weather-stat-val">${Math.round(w.wind.speed)} mph ${windDir(w.wind.deg)}</span></div>
      <div class="weather-stat"><span class="weather-stat-label">Pressure</span><span class="weather-stat-val">${w.main.pressure} hPa</span></div>
      <div class="weather-stat"><span class="weather-stat-label">Visibility</span><span class="weather-stat-val">${vis}</span></div>
      <div class="weather-stat"><span class="weather-stat-label">Clouds</span><span class="weather-stat-val">${w.clouds ? w.clouds.all : 0}%</span></div>
      <div class="weather-stat"><span class="weather-stat-label">WiFi Impact</span><span class="weather-stat-val" style="color:${w.main.humidity > 80 ? 'var(--amber)' : 'var(--green)'}">${w.main.humidity > 80 ? 'Possible' : 'None'}</span></div>
    </div>
    ${sunHTML}
  </div>`;
  document.getElementById('weather-updated').textContent = new Date().toLocaleTimeString('en-US',{hour12:false});
}

/* ═══ SIDEBAR TABS ═══ */
document.querySelectorAll('.sidebar-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.sidebar-tab').forEach(t => t.classList.remove('on'));
    document.querySelectorAll('.sidebar-view').forEach(v => v.classList.remove('on'));
    tab.classList.add('on');
    document.getElementById('view-'+tab.dataset.v).classList.add('on');
  });
});

/* ═══ NAV RAIL PAGE SWITCHING ═══ */
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('on'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
    btn.classList.add('on');
    const page = document.getElementById('page-' + btn.dataset.page);
    if (page) page.classList.add('on');
    if (btn.dataset.page === 'map') setTimeout(initMap, 100);
    if (btn.dataset.page === 'timeline') fetchTimeline();
  });
});

/* ═══ FILTERS ═══ */
document.querySelectorAll('.tbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tbtn').forEach(b=>b.classList.remove('on'));
    btn.classList.add('on'); S.filter=btn.dataset.f; renderTable();
  });
});
document.getElementById('search').addEventListener('input', e => { S.query=e.target.value.toLowerCase(); renderTable(); });

/* ═══ EVENT DELEGATION for device rows + alias inputs ═══ */
document.getElementById('device-list').addEventListener('click', function(e) {
  if (e.target.closest('.alias-input') || e.target.closest('.alias-row') || e.target.closest('.wl-btn')) return;
  const row = e.target.closest('.d-row');
  if (row && row.dataset.mac) {
    S.selected = S.selected === row.dataset.mac ? null : row.dataset.mac;
    renderTable();
  }
});
document.getElementById('device-list').addEventListener('change', function(e) {
  if (e.target.classList.contains('alias-input')) {
    saveAlias(e.target.dataset.mac, e.target.value);
    renderTable();
  }
});

/* ═══ WATCHLIST CLICK HANDLER ═══ */
document.getElementById('device-list').addEventListener('click', function(e) {
  const wlBtn = e.target.closest('.wl-btn');
  if (wlBtn) {
    const mac = wlBtn.dataset.mac, tag = wlBtn.dataset.wl;
    setWatch(mac, getWatch(mac) === tag ? '' : tag);
    renderTable();
  }
});

/* ═══ EXPORT BUTTON ═══ */
document.getElementById('btn-export').addEventListener('click', exportCSV);

/* ═══ SOUND TOGGLE ═══ */
document.getElementById('btn-sound').addEventListener('click', function() {
  S.soundEnabled = !S.soundEnabled;
  this.classList.toggle('snd-on', S.soundEnabled);
  this.textContent = S.soundEnabled ? 'SND ON' : 'SND';
  if (S.soundEnabled) playProximityTone();
});

/* ═══ DEVICE DETAIL DRAWER ═══ */
function buildDetail(d) {
  const realAssoc = d._isAssociated;
  const probes = (d.probed_ssids||[]).filter(s=>s);
  const alias = getAlias(d.mac);
  const sh = S.sigHist.get(d.mac) || [];
  const hasSigData = sh.length >= 2 && sh.some(v => v !== -1);

  return `<div class="detail-grid">
    <div class="detail-section">
      <div class="detail-section-title">Network Association</div>
      <div class="detail-row"><span class="detail-key">BSSID</span><span class="detail-val ${realAssoc?'highlight':''}">${realAssoc ? d.bssid : 'Not associated'}</span></div>
      <div class="detail-row"><span class="detail-key">ESSID</span><span class="detail-val highlight">${d.essid||'\u2014'}</span></div>
      <div class="detail-row"><span class="detail-key">Channel</span><span class="detail-val">${d.channel||'\u2014'}</span></div>
      <div class="detail-row"><span class="detail-key">Encryption</span><span class="detail-val">${d.encryption||'\u2014'} ${d.cipher||''}</span></div>
      <div class="detail-row"><span class="detail-key">Auth</span><span class="detail-val">${d.auth||'\u2014'}</span></div>
      <div class="detail-row"><span class="detail-key">Status</span><span class="detail-val" style="color:${realAssoc?'var(--green)':'var(--amber)'}">${realAssoc?'Associated':'Unassociated'}</span></div>
    </div>
    <div class="detail-section">
      <div class="detail-section-title">Signal / Activity</div>
      <div class="detail-row"><span class="detail-key">RSSI</span><span class="detail-val">${d.signal === -1 ? 'No data' : d.signal + ' dBm'}</span></div>
      <div class="detail-row"><span class="detail-key">Distance</span><span class="detail-val" style="color:${d.distance==='close'?'var(--green)':d.distance==='medium'?'var(--amber)':d.distance==='far'?'var(--red)':'var(--t4)'}">${d.signal === -1 ? 'UNKNOWN' : (d.distance||'?').toUpperCase()}</span></div>
      <div class="detail-row"><span class="detail-key">Movement</span><span class="detail-val" style="color:${d.movement==='approaching'?'var(--green)':d.movement==='leaving'?'var(--red)':'var(--t4)'}">${d.movement==='approaching'?'\u25B2 APPROACHING':d.movement==='leaving'?'\u25BC LEAVING':d.movement==='stationary'?'\u2500 STATIONARY':'UNKNOWN'}</span></div>
      <div class="detail-row"><span class="detail-key">Packets</span><span class="detail-val">${(d.packets||0).toLocaleString()}</span></div>
      <div class="detail-row"><span class="detail-key">Pkt Rate</span><span class="detail-val">${d.packet_rate||0}/s</span></div>
      <div class="detail-row"><span class="detail-key">Duration</span><span class="detail-val">${fmtDuration(d.duration||0)}</span></div>
      <div class="detail-row"><span class="detail-key">Active</span><span class="detail-val" style="color:${d.active?'var(--green)':'var(--t4)'}">${d.active?'YES':'NO'}</span></div>
      <div class="detail-row"><span class="detail-key">Threat</span><span class="detail-val" style="color:${d.threat_level==='high'?'var(--red)':d.threat_level==='medium'?'var(--amber)':'var(--green)'}">${(d.threat_level||'low').toUpperCase()} (${d.threat_score||0}) ${(d.threat_reasons||[]).join(', ')}</span></div>
      ${hasSigData ? `<div class="sig-spark-wrap"><div class="sig-spark-label">RSSI History (last ${sh.length} polls)</div><canvas class="sig-spark-canvas" data-mac="${d.mac}" width="280" height="36"></canvas></div>` : ''}
    </div>
    <div class="detail-section">
      <div class="detail-section-title">Identity / Probes</div>
      <div class="detail-row"><span class="detail-key">MAC</span><span class="detail-val">${d.mac}</span></div>
      <div class="detail-row"><span class="detail-key">Vendor</span><span class="detail-val">${d.vendor||'Unknown'}</span></div>
      <div class="detail-row"><span class="detail-key">Type</span><span class="detail-val">${TL[d._type]||d.type||'Unknown'}</span></div>
      <div class="detail-row"><span class="detail-key">MAC Type</span><span class="detail-val" style="color:${(parseInt(d.mac.split(':')[0],16)&0x02)?'var(--red)':'var(--green)'}">${(parseInt(d.mac.split(':')[0],16)&0x02)?'Randomized':'Global (real)'}</span></div>
      <div class="detail-row"><span class="detail-key">Presence</span><span class="detail-val"><span class="d-presence dp-${d.presence||'new'}">${(d.presence||'new').toUpperCase()}</span></span></div>
      <div style="margin-top:4px">
        <span class="detail-key">Probed SSIDs</span>
        <div class="probe-tags" style="margin-top:4px">
          ${probes.length ? probes.map(s => `<span class="probe-tag ${s===d.essid?'connected':''}">${s}${s===d.essid?' \u2713':''}</span>`).join('') : '<span style="font-family:var(--mono);font-size:8px;color:var(--t4)">None detected</span>'}
        </div>
      </div>
      <div class="alias-row">
        <span class="alias-label">Alias</span>
        <input class="alias-input" data-mac="${d.mac}" value="${alias}" placeholder="Tag this device\u2026" maxlength="40">
      </div>
      <div class="wl-btns">
        <button class="wl-btn ${getWatch(d.mac)==='trusted'?'wl-trusted':''}" data-mac="${d.mac}" data-wl="trusted">Trusted</button>
        <button class="wl-btn ${getWatch(d.mac)==='suspicious'?'wl-suspicious':''}" data-mac="${d.mac}" data-wl="suspicious">Suspicious</button>
        <button class="wl-btn ${getWatch(d.mac)==='ignore'?'wl-ignore':''}" data-mac="${d.mac}" data-wl="ignore">Ignore</button>
      </div>
    </div>
  </div>`;
}

function drawSigSparkline(mac) {
  const canvas = document.querySelector(`.sig-spark-canvas[data-mac="${mac}"]`);
  if (!canvas) return;
  const data = (S.sigHist.get(mac) || []).filter(v => v !== -1);
  if (data.length < 2) return;
  const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(...data), min = Math.min(...data);
  const range = max - min || 1;
  const step = w / (data.length - 1);
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(0,230,118,.3)');
  grad.addColorStop(1, 'rgba(0,230,118,.02)');
  ctx.beginPath(); ctx.moveTo(0, h);
  data.forEach((v, i) => ctx.lineTo(i * step, h - ((v - min) / range) * (h - 4) - 2));
  ctx.lineTo((data.length - 1) * step, h); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  data.forEach((v, i) => { const x = i * step, y = h - ((v - min) / range) * (h - 4) - 2; i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
  ctx.strokeStyle = '#00e676'; ctx.lineWidth = 1.5; ctx.stroke();
  ctx.fillStyle = 'var(--t4)'; ctx.font = '7px IBM Plex Mono';
  ctx.fillText(max + ' dBm', 2, 8);
  ctx.fillText(min + ' dBm', 2, h - 2);
}

/* ═══ DEVICE TABLE ═══ */
function renderTable() {
  const list = document.getElementById('device-list');
  const now = Date.now() / 1000;
  let devs = [...S.devices.values()];
  switch(S.filter) {
    case 'active': devs=devs.filter(d=>d.active); break;
    case 'associated': devs=devs.filter(d=>d._isAssociated); break;
    case 'probing': devs=devs.filter(d=>(d.probed_ssids||[]).filter(s=>s).length>0); break;
    case 'random': devs=devs.filter(d=>d._type==='random'); break;
    case 'close': devs=devs.filter(d=>d.distance==='close'&&d.signal!==-1); break;
    case 'known': devs=devs.filter(d=>d.presence==='resident'||d.presence==='regular'); break;
    case 'unknown': devs=devs.filter(d=>d.presence!=='resident'&&d.presence!=='regular'); break;
  }
  if (S.query) {
    const q = S.query;
    devs = devs.filter(d => d.mac.toLowerCase().includes(q) || (d.vendor||'').toLowerCase().includes(q) || (d.essid||'').toLowerCase().includes(q) || (d.bssid||'').toLowerCase().includes(q) || (d.probed_ssids||[]).some(s => s.toLowerCase().includes(q)) || (d._type||'').includes(q) || (getAlias(d.mac)||'').toLowerCase().includes(q) || (getWatch(d.mac)||'').includes(q) || (d.presence||'').includes(q));
  }
  devs.sort((a,b) => { if (a.active !== b.active) return b.active - a.active; return b.signal - a.signal; });
  const existing = new Map;
  list.querySelectorAll('.d-row').forEach(r => existing.set(r.dataset.mac, r));
  const currentMACs = new Set(devs.map(d => d.mac));
  existing.forEach((row, mac) => {
    if (!currentMACs.has(mac)) {
      row.classList.add('is-gone');
      const det = list.querySelector(`.d-detail[data-mac="${mac}"]`);
      if (det) det.remove();
      setTimeout(() => row.remove(), 400);
    }
  });
  devs.forEach((d, idx) => {
    const isNew = S.tick > 0 && !S.prevMACs.has(d.mac);
    const essidDisplay = d.essid
      ? `<div class="d-essid">${d.essid}</div><div class="d-chan">Ch ${d.channel||'?'} \u00B7 ${(d.encryption||'').replace(/\s/g,'') || '?'}</div>`
      : `<div class="d-essid unassoc">${d._isAssociated ? d.bssid||'\u2014' : 'Not associated'}</div><div class="d-chan">${(d.probed_ssids||[]).filter(s=>s).length ? 'Probing\u2026' : '\u2014'}</div>`;
    const distLabel = d.signal === -1 ? 'N/A' : (d.distance||'?').toUpperCase();
    const distClass = d.signal === -1 ? 'dd-unknown' : 'dd-'+d.distance;
    const rowAlias = getAlias(d.mac);
    const wl = getWatch(d.mac);
    const wlBadge = wl ? '<span class="d-wl-badge wl-b-'+wl+'">'+wl+'</span>' : '';
    const mvmt = d.movement || 'unknown';
    const mvmtIcon = mvmt==='approaching'?'<span class="mv-arrow mv-approach">\u25B2</span>':mvmt==='leaving'?'<span class="mv-arrow mv-leave">\u25BC</span>':mvmt==='stationary'?'<span class="mv-arrow mv-static">\u2500</span>':'';
    const threatBadge = d.threat_level && d.threat_level !== 'low' ? `<span class="d-threat d-threat-${d.threat_level}">${d.threat_level==='high'?'\u{1F6A8}':'\u26A0'}</span>` : '';
    const pres = d.presence || 'new';
    const presenceBadge = `<span class="d-presence dp-${pres}">${pres}</span>`;
    const html = `<div class="d-dot ${d.active?'on':'off'}"></div>
      <div class="d-id">${rowAlias ? '<div class="d-alias">'+rowAlias+'</div>' : ''}${wlBadge}${threatBadge}<div class="d-mac">${d.mac}</div><div class="d-vendor">${d.vendor||'Unknown'}</div></div>
      <div><span class="d-type ${TC[d._type]}">${TL[d._type]}</span>${presenceBadge}</div>
      <div class="d-net">${essidDisplay}</div>
      <div class="d-signal">${sigHTML(d.signal)}<span>${d.signal===-1?'':d.signal}</span>${mvmtIcon}</div>
      <div class="d-dist ${distClass}">${distLabel}</div>
      <div class="d-pkts">${(d.packets||0).toLocaleString()}</div>
      <div class="d-seen">${ago(d._firstSeen||d.first_seen||now, now)}</div>`;
    let row = existing.get(d.mac);
    if (row) { row.innerHTML = html; } else {
      row = document.createElement('div');
      row.className = 'd-row' + (isNew ? ' is-new' : '');
      row.dataset.mac = d.mac;
      row.innerHTML = html;
      list.appendChild(row);
    }
    row.style.order = idx * 2;
    row.classList.toggle('selected', S.selected === d.mac);
    let det = list.querySelector(`.d-detail[data-mac="${d.mac}"]`);
    if (S.selected === d.mac) {
      if (!det) { det = document.createElement('div'); det.className = 'd-detail open'; det.dataset.mac = d.mac; row.insertAdjacentElement('afterend', det); }
      det.innerHTML = buildDetail(d); det.style.order = idx * 2 + 1;
      drawSigSparkline(d.mac);
    } else if (det) { det.remove(); }
  });
  document.getElementById('reg-badge').textContent = devs.length;
}

/* ═══ RENDER PI SYSTEM PAGE ═══ */
function renderPiSystem(sys) {
  if (!sys) return;
  const $ = id => document.getElementById(id);
  $('pi-hostname').textContent = sys.hostname || 'Raspberry Pi';
  $('pi-ip').textContent = `${sys.ip || '?'} \u00B7 up ${fmtDuration(sys.uptime_sec||0)}`;

  const pct = (v,t) => v >= 0 ? v+'%' : '--';
  const cls = v => v < 60 ? 'ok' : v < 85 ? 'warn' : 'err';
  const col = c => c === 'ok' ? 'var(--green)' : c === 'warn' ? 'var(--amber)' : 'var(--red)';

  $('pi-grid').innerHTML = `
    <div class="pi-card">
      <div class="pi-card-title">Compute</div>
      <div class="pi-stat"><span class="pi-stat-key">CPU Usage</span><span class="pi-stat-val ${cls(sys.cpu_pct)}">${pct(sys.cpu_pct)}</span></div>
      <div class="pi-bar"><div class="pi-bar-fill" style="width:${Math.max(sys.cpu_pct,0)}%;background:${col(cls(sys.cpu_pct))}"></div></div>
      <div class="pi-stat" style="margin-top:6px"><span class="pi-stat-key">CPU Temp</span><span class="pi-stat-val ${sys.cpu_temp_c>70?'err':sys.cpu_temp_c>55?'warn':'ok'}">${sys.cpu_temp_c>=0?sys.cpu_temp_c+'\u00B0C':'--'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">RAM</span><span class="pi-stat-val ${cls(sys.ram_pct)}">${sys.ram_used_mb}/${sys.ram_total_mb} MB (${pct(sys.ram_pct)})</span></div>
      <div class="pi-bar"><div class="pi-bar-fill" style="width:${Math.max(sys.ram_pct,0)}%;background:${col(cls(sys.ram_pct))}"></div></div>
      <div class="pi-stat" style="margin-top:6px"><span class="pi-stat-key">Disk</span><span class="pi-stat-val ${cls(sys.disk_pct)}">${sys.disk_used_gb}/${sys.disk_total_gb} GB (${pct(sys.disk_pct)})</span></div>
      <div class="pi-bar"><div class="pi-bar-fill" style="width:${Math.max(sys.disk_pct,0)}%;background:${col(cls(sys.disk_pct))}"></div></div>
    </div>
    <div class="pi-card">
      <div class="pi-card-title">Network Interfaces</div>
      <div class="pi-stat"><span class="pi-stat-key">eth0</span><span class="pi-stat-val ${sys.eth0_up?'ok':'err'}">${sys.eth0_up?'UP':'DOWN'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">wlan0</span><span class="pi-stat-val ${sys.wlan0_up?'ok':'err'}">${sys.wlan0_up?'UP':'DOWN'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">wlan1</span><span class="pi-stat-val ${sys.wlan1_up?'ok':'err'}">${sys.wlan1_up?'UP':'DOWN'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">wlan1mon</span><span class="pi-stat-val ${sys.wlan1mon_up?'ok':'warn'}">${sys.wlan1mon_up?'MONITOR':'DOWN'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">IP Address</span><span class="pi-stat-val">${sys.ip||'--'}</span></div>
    </div>
    <div class="pi-card">
      <div class="pi-card-title">Data Pipeline</div>
      <div class="pi-stat"><span class="pi-stat-key">CSV Age</span><span class="pi-stat-val ${sys.csv_age<5?'ok':sys.csv_age<15?'warn':'err'}">${sys.csv_age>=0?sys.csv_age.toFixed(0)+'s':'--'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">JSON Age</span><span class="pi-stat-val ${sys.json_age<5?'ok':sys.json_age<15?'warn':'err'}">${sys.json_age>=0?sys.json_age.toFixed(0)+'s':'--'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">Stream Age</span><span class="pi-stat-val ${sys.stream_age<10?'ok':sys.stream_age<30?'warn':'err'}">${sys.stream_age>=0?sys.stream_age.toFixed(0)+'s':'--'}</span></div>
      <div class="pi-stat"><span class="pi-stat-key">Parser</span><span class="pi-stat-val ok">v3</span></div>
      <div class="pi-stat"><span class="pi-stat-key">Uptime</span><span class="pi-stat-val">${fmtDuration(sys.uptime_sec||0)}</span></div>
    </div>`;
}

/* ═══ RENDER RF CHANNEL MAP ═══ */
function renderRfMap(rf) {
  if (!rf) return;
  const splitEl = document.getElementById('rf-split');
  const cardsEl = document.getElementById('rf-cards');

  splitEl.innerHTML = `
    <div class="rf-split-block"><div class="rf-split-val v-amber">${rf.total_24}</div><div class="rf-split-label">2.4 GHz APs</div></div>
    <div class="rf-split-vs">vs</div>
    <div class="rf-split-block"><div class="rf-split-val v-cyan">${rf.total_5}</div><div class="rf-split-label">5 GHz APs</div></div>
    <div class="rf-split-block"><div class="rf-split-val v-purple">${rf.busiest_count}</div><div class="rf-split-label">Ch ${rf.busiest_channel} (busiest)</div></div>
    <div class="rf-split-block"><div class="rf-split-val v-red">${rf.hidden_count}</div><div class="rf-split-label">Hidden SSIDs</div></div>`;

  const maxCh = Math.max(...Object.values(rf.channels_24||{}), ...Object.values(rf.channels_5||{}), 1);
  const barRow = (ch, count, color) => `<div class="ch-bar-row"><span class="ch-num">${ch}</span><div class="ch-bar"><div class="ch-bar-fill" style="width:${(count/maxCh)*100}%;background:${color}"></div></div><span class="ch-count">${count}</span></div>`;

  let ch24html = ''; for (let c = 1; c <= 14; c++) { const n = (rf.channels_24||{})[c]||0; if (n > 0) ch24html += barRow(c, n, 'var(--amber)'); }
  let ch5html = ''; for (const [c,n] of Object.entries(rf.channels_5||{}).sort((a,b)=>a[0]-b[0])) { if (n > 0) ch5html += barRow(c, n, 'var(--cyan)'); }

  let encHtml = ''; for (const [e,c] of Object.entries(rf.encryption||{}).sort((a,b)=>b[1]-a[1])) {
    const cl = e.includes('WPA3')||e.includes('SAE')?'v-cyan':e.includes('WPA')?'v-green':e.includes('WEP')?'v-amber':'v-red';
    encHtml += `<div class="an-row"><span class="an-row-key">${e||'OPEN'}</span><span class="an-row-val ${cl}">${c}</span></div>`;
  }

  let apHtml = (rf.ap_by_clients||[]).map(a =>
    `<tr><td>${a.essid||'<hidden>'}</td><td>${a.channel}</td><td>${a.power}</td><td>${a.privacy}</td><td style="color:var(--cyan);font-weight:600">${a.clients}</td></tr>`
  ).join('');

  cardsEl.innerHTML = `
    <div class="rf-card"><div class="rf-card-title">2.4 GHz Channels <span class="rf-card-badge v-amber">${rf.total_24} APs</span></div>${ch24html||'<div style="color:var(--t4);font-size:9px">No 2.4 GHz APs detected</div>'}</div>
    <div class="rf-card"><div class="rf-card-title">5 GHz Channels <span class="rf-card-badge v-cyan">${rf.total_5} APs</span></div>${ch5html||'<div style="color:var(--t4);font-size:9px">No 5 GHz APs detected</div>'}</div>
    <div class="rf-card"><div class="rf-card-title">Encryption Types</div>${encHtml||'<div style="color:var(--t4);font-size:9px">No data</div>'}</div>
    <div class="rf-card" style="grid-column:1/-1"><div class="rf-card-title">APs by Client Count</div>
      <table class="rf-table"><thead><tr><th>ESSID</th><th>Ch</th><th>Power</th><th>Encryption</th><th>Clients</th></tr></thead><tbody>${apHtml||'<tr><td colspan="5" style="color:var(--t4)">No AP data</td></tr>'}</tbody></table>
    </div>`;
}

/* ═══ RENDER ANALYTICS / HISTORY PAGE ═══ */
function renderAnalytics(hist, reg) {
  if (!hist) return;
  const el = document.getElementById('an-cards');

  let vendorHtml = (hist.top_vendors||[]).map(v =>
    `<div class="an-row"><span class="an-row-key">${v.vendor}</span><span class="an-row-val">${v.count}</span></div>`
  ).join('');

  let recurHtml = (hist.recurring_devices||[]).slice(0,10).map(d =>
    `<div class="an-row"><span class="an-row-key" title="${d.mac}">${getAlias(d.mac)||d.mac.substring(0,11)+'\u2026'}</span><span class="an-row-val">${d.visits} visits \u00B7 ${d.vendor}</span></div>`
  ).join('');

  el.innerHTML = `
    <div class="an-card"><div class="an-big">${hist.total_ever_seen||0}</div><div class="an-big-label">Total Unique MACs Ever Seen</div></div>
    <div class="an-card"><div class="an-big" style="color:var(--green)">${hist.seen_today||0}</div><div class="an-big-label">Unique Devices Today</div></div>
    <div class="an-card"><div class="an-big" style="color:var(--amber)">${hist.events_today||0}</div><div class="an-big-label">Events Today</div></div>
    <div class="an-card"><div class="an-big" style="color:var(--purple)">${reg?reg.returning_this_week:0}</div><div class="an-big-label">Returning This Week</div></div>
    <div class="an-card"><div class="an-card-title">Top Vendors Today</div>${vendorHtml||'<div style="color:var(--t4);font-size:9px">No data yet</div>'}</div>
    <div class="an-card"><div class="an-card-title">Recurring Devices (All Time)</div>${recurHtml||'<div style="color:var(--t4);font-size:9px">No recurring devices yet</div>'}</div>`;
}

/* ═══ RENDER EVENT LOG PAGE ═══ */
let evFilter = 'all';
document.getElementById('ev-filters').addEventListener('click', function(e) {
  const btn = e.target.closest('.tbtn');
  if (!btn) return;
  this.querySelectorAll('.tbtn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  evFilter = btn.dataset.ef;
  renderEventLog(S._lastEvents || []);
});

function renderEventLog(evts) {
  S._lastEvents = evts;
  const el = document.getElementById('ev-list');
  const icons = {device_join:'join',device_leave:'leave',close_alert:'close',new_ap:'ap',burst:'burst'};
  const labels = {device_join:'Joined',device_leave:'Left',close_alert:'Close Range',new_ap:'New AP',burst:'Pkt Burst'};
  const filtered = evFilter === 'all' ? evts : evts.filter(e => e.type === evFilter);
  el.innerHTML = filtered.slice(0,80).map(e => {
    const dt = new Date(e.ts * 1000);
    const t = dt.toLocaleTimeString('en-US',{hour12:false});
    const d = dt.toLocaleDateString('en-US',{month:'short',day:'numeric'});
    return `<div class="ev-item"><div class="ev-dot ${icons[e.type]||''}"></div><div class="ev-body"><div class="ev-msg"><b>[${labels[e.type]||e.type}]</b> ${e.mac?'<b>'+e.mac.substring(0,11)+'\u2026</b> ':''}${e.detail||''}</div><div class="ev-time">${d} ${t}</div></div></div>`;
  }).join('') || '<div style="padding:20px;text-align:center;color:var(--t4);font-size:10px">No events recorded yet</div>';
}

/* ═══ SYSTEM STATUS ═══ */
function renderSystemStatus() {
  const age = S.lastDataTs ? Math.round((Date.now()/1000) - S.lastDataTs) : -1;
  const ageEl = document.getElementById('sys-age');
  const badgeEl = document.getElementById('sys-badge');
  if (age < 0) { ageEl.textContent = '--'; ageEl.className = 'sys-val'; }
  else if (age <= 5) { ageEl.textContent = age + 's'; ageEl.className = 'sys-val ok'; }
  else if (age <= 15) { ageEl.textContent = age + 's'; ageEl.className = 'sys-val warn'; }
  else { ageEl.textContent = age + 's STALE'; ageEl.className = 'sys-val err'; }

  const latEl = document.getElementById('sys-latency');
  latEl.textContent = S.pollLatency ? S.pollLatency + 'ms' : '--';
  latEl.className = 'sys-val' + (S.pollLatency > 1000 ? ' warn' : S.pollLatency > 3000 ? ' err' : ' ok');

  document.getElementById('sys-polls').textContent = S.tick;
  const failEl = document.getElementById('sys-fails');
  failEl.textContent = S.failCount;
  failEl.className = 'sys-val' + (S.failCount === 0 ? ' ok' : S.failCount < 3 ? ' warn' : ' err');

  const camEl = document.getElementById('sys-cam');
  const camBadge = document.getElementById('cam-badge');
  const camLive = camBadge && camBadge.textContent === 'LIVE';
  camEl.textContent = camLive ? 'LIVE' : 'OFFLINE';
  camEl.className = 'sys-val' + (camLive ? ' ok' : ' err');

  const parserEl = document.getElementById('sys-parser');
  if (age >= 0 && age <= 10) { parserEl.textContent = 'ACTIVE'; parserEl.className = 'sys-val ok'; }
  else if (age > 10 && age <= 30) { parserEl.textContent = 'SLOW'; parserEl.className = 'sys-val warn'; }
  else { parserEl.textContent = 'DOWN'; parserEl.className = 'sys-val err'; }

  const worst = S.failCount >= 3 ? 'ERR' : (age > 15 || S.failCount >= 1) ? 'WARN' : 'OK';
  badgeEl.textContent = worst;
  badgeEl.style.color = worst === 'OK' ? 'var(--green)' : worst === 'WARN' ? 'var(--amber)' : 'var(--red)';
}
setInterval(renderSystemStatus, 1000);

/* ═══ MAP ═══ */
function initMap() {
  if (S._mapInit) return;
  const container = document.getElementById('map-container');
  if (!container || !window.L) return;
  S._mapInit = true;
  const map = L.map('map-container').setView([33.9519, -83.3576], 16);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; OSM &copy; CARTO', maxZoom: 19
  }).addTo(map);
  const marker = L.circleMarker([33.9519, -83.3576], {
    radius: 8, fillColor: '#00d4f5', color: '#00d4f5', weight: 2, opacity: 0.8, fillOpacity: 0.3
  }).addTo(map);
  marker.bindPopup('<b>OVERWATCH Station</b><br>Athens, GA').openPopup();
  S._map = map;
  setTimeout(() => map.invalidateSize(), 100);
}

/* ═══ ATHENS LOCAL DATA ═══ */
const ATHENS = {
  bars: [
    { name:'FIVE Bar', icon:'\u{1F378}', type:'drink', note:'Verify specials on Instagram',
      deals:[
        {days:[2,3,4,5,0], time:'4:00–6:00 PM', start:16, end:18, info:'Happy hour: half-price well liquor & drafts, $2 off wine, half off signature cocktails'},
        {days:[3], time:'All evening', start:17, end:23, info:'Wine-Down Wednesday: half-price bottles of wine'},
        {days:[4], time:'All evening', start:17, end:23, info:'Ladies Night: same happy hour drink prices extended'},
      ]},
    { name:'South Kitchen + Bar', icon:'\u{1F37D}\uFE0F', type:'food', note:'Check for seasonal menu changes',
      deals:[
        {days:[0,1,2,3,4,5,6], time:'2:00–5:00 PM', start:14, end:17, info:'Daily happy hour with special food selections'},
      ]},
    { name:'The Pine Bar', icon:'\u{1F332}', type:'drink',
      deals:[
        {days:[0,1,2,3,4,5,6], time:'5:00–7:00 PM', start:17, end:19, info:'Daily happy hour'},
        {days:[2], time:'5:00 PM+', start:17, end:22, info:'Tuesday: 50% off all open wine bottles by the glass'},
        {days:[3], time:'5:00 PM+', start:17, end:22, info:'Wednesday: $12 chef selection cheese & charcuterie boards'},
      ]},
    { name:'Trappeze Pub', icon:'\u{1F37A}', type:'drink',
      deals:[
        {days:[1,2,3,4], time:'2:00–5:00 PM', start:14, end:17, info:'Happy hour: $8 signature cocktails, $1 off canned local beers'},
      ]},
    { name:"Pauley's Cr\u00EApe Bar", icon:'\u{1F95E}', type:'food', note:'Menu PDF source — verify on Instagram',
      deals:[
        {days:[1,2,3], time:'All day', start:11, end:23, info:'Mon–Wed: $1 off crafts & drafts, half off all wine'},
        {days:[4], time:'4:00 PM–close', start:16, end:23, info:'Ladies Night: extended drink specials'},
      ]},
    { name:"Ted's Most Best", icon:'\u{1F355}', type:'food',
      deals:[
        {days:[0,1,2,3,4,5,6], time:'3:00–6:00 PM', start:15, end:18, info:'Daily happy hour'},
      ]},
    { name:'Saucehouse BBQ', icon:'\u{1F356}', type:'food', note:'From Visit Athens 2024 roundup — verify',
      deals:[
        {days:[3,4,5], time:'3:00–7:00 PM', start:15, end:19, info:'Wed–Fri happy hour'},
      ]},
    { name:'Blind Pig Tavern', icon:'\u{1F416}', type:'drink', note:'Rotating specials — check their board',
      deals:[
        {days:[1,2,3,4,5], time:'Varies', start:15, end:19, info:'Weekday rotating drink specials'},
      ]},
  ],
  markets: [
    { name:'Athens Farmers Market — Saturday', icon:'\u{1F33D}', type:'market',
      deals:[
        {days:[6], time:'8:00 AM–12:00 PM (winter: 9 AM–1 PM)', start:8, end:12, info:'Bishop Park, 705 Sunset Drive. Fresh produce, baked goods, local vendors.'},
      ]},
    { name:'Athens Farmers Market — Wednesday', icon:'\u{1F966}', type:'market',
      deals:[
        {days:[3], time:'5:00–8:00 PM', start:17, end:20, info:'Creature Comforts, 271 W Hancock Ave. March–November season.'},
      ]},
  ],
  events: [
    { name:'Creature Comforts Brewery', icon:'\u{1F37B}', type:'event',
      deals:[
        {days:[0,1,2,3,4,5,6], time:'Tours available', start:12, end:20, info:'Taproom open daily. Check website for tour schedule and special releases.'},
      ]},
    { name:'Downtown Athens First Friday', icon:'\u{1F3B5}', type:'event', note:'Monthly — first Friday of each month',
      deals:[
        {days:[5], time:'Evening', start:17, end:22, info:'Gallery openings, live music, and extended shop hours along downtown. First Friday of each month.'},
      ]},
    { name:'UGA Campus Events', icon:'\u{1F3C8}', type:'event', note:'Check UGA events calendar',
      deals:[
        {days:[0,1,2,3,4,5,6], time:'Varies', start:9, end:22, info:'Concerts, sports, lectures, and cultural events. Check uga.edu/events.'},
      ]},
  ],
};

const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const DAYS_FULL = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];

function getActiveDeals() {
  const now = new Date();
  const day = now.getDay();
  const hour = now.getHours() + now.getMinutes() / 60;
  const active = [];
  const upcoming = [];
  const allVenues = [...ATHENS.bars, ...ATHENS.markets, ...ATHENS.events];
  allVenues.forEach(venue => {
    venue.deals.forEach(deal => {
      if (!deal.days.includes(day)) return;
      if (hour >= deal.start && hour < deal.end) {
        active.push({venue: venue.name, icon: venue.icon, type: venue.type, deal, live: true});
      } else if (deal.start > hour && deal.start - hour < 4) {
        upcoming.push({venue: venue.name, icon: venue.icon, type: venue.type, deal, live: false});
      }
    });
  });
  return {active, upcoming, day};
}

function renderLocalPage() {
  const {active, upcoming, day} = getActiveDeals();
  const hour = new Date().getHours() + new Date().getMinutes() / 60;

  const todayBox = document.getElementById('local-today-box');
  if (todayBox) {
    let html = `<div class="local-today-title">\u{1F4CD} ${DAYS_FULL[day]} — Right Now</div>`;
    if (active.length > 0) {
      html += active.map(a => `<div class="local-deal local-deal-active"><span class="local-deal-day">LIVE</span><div class="local-deal-info"><b>${a.icon} ${a.venue}</b> — ${a.deal.info}<div class="local-deal-time">${a.deal.time}</div></div></div>`).join('');
    } else {
      html += '<div class="local-today-empty">Nothing active right now.</div>';
    }
    if (upcoming.length > 0) {
      html += `<div style="font-family:var(--mono);font-size:8px;color:var(--t4);margin-top:8px;text-transform:uppercase;letter-spacing:1px">Coming up</div>`;
      html += upcoming.map(u => `<div class="local-deal"><span class="local-deal-day" style="color:var(--amber)">SOON</span><div class="local-deal-info">${u.icon} ${u.venue} — ${u.deal.info}<div class="local-deal-time">${u.deal.time}</div></div></div>`).join('');
    }
    todayBox.innerHTML = html;
  }

  function renderVenueCards(venues, containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = venues.map(v => {
      const typeClass = v.type === 'food' ? 'lct-food' : v.type === 'drink' ? 'lct-drink' : v.type === 'market' ? 'lct-market' : 'lct-event';
      const dealsHtml = v.deals.map(d => {
        const dayNames = d.days.map(n => DAYS[n]).join(', ');
        const isActive = d.days.includes(day) && hour >= d.start && hour < d.end;
        return `<div class="local-deal ${isActive?'local-deal-active':''}"><span class="local-deal-day">${isActive?'LIVE':dayNames}</span><div class="local-deal-info">${d.info}<div class="local-deal-time">${d.time}</div></div></div>`;
      }).join('');
      return `<div class="local-card"><div class="local-card-head"><span class="local-card-icon">${v.icon}</span><span class="local-card-name">${v.name}</span><span class="local-card-type ${typeClass}">${v.type}</span></div>${dealsHtml}${v.note?'<div class="local-note">\u26A0 '+v.note+'</div>':''}</div>`;
    }).join('');
  }

  renderVenueCards(ATHENS.bars, 'local-bars');
  renderVenueCards(ATHENS.markets, 'local-markets');
  renderVenueCards(ATHENS.events, 'local-events');
}

function renderTodaySidebar() {
  const {active, upcoming, day} = getActiveDeals();
  const el = document.getElementById('today-list');
  const dayEl = document.getElementById('today-day');
  if (!el) return;
  if (dayEl) dayEl.textContent = DAYS_FULL[day];

  const items = [...active, ...upcoming];
  if (items.length === 0) {
    el.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t4);font-family:var(--mono);font-size:10px">No deals or events right now.<br>Check the Local page for the full schedule.</div>';
    return;
  }
  el.innerHTML = items.map(a =>
    `<div class="today-item"><div class="today-icon">${a.icon}</div><div class="today-body"><div class="today-name">${a.venue}${a.live?'<span class="today-live">Live</span>':''}</div><div class="today-detail">${a.deal.info}<br>${a.deal.time}</div></div></div>`
  ).join('');
}

renderLocalPage();
renderTodaySidebar();
setInterval(() => { renderLocalPage(); renderTodaySidebar(); }, 60000);

/* ═══ PROCESS DATA (shared by WebSocket + polling) ═══ */
function processData(data) {
  const now = data.timestamp || (Date.now()/1000);
  S.lastDataTs = data.timestamp || 0;
  const currentMACs = new Set;
  S.failCount = 0;
  updateConnHealth();

  if (data.networks) { S.networks.clear(); data.networks.forEach(n => S.networks.set(n.bssid, n)); }

  (data.devices||[]).forEach(d => {
    currentMACs.add(d.mac);
    d._type = classify(d.vendor, d.mac, d.type);
    d._isAssociated = d.associated && isRealBssid(d.bssid);
    if (d._isAssociated && S.networks.has(d.bssid)) {
      const net = S.networks.get(d.bssid);
      d.essid = d.essid || net.essid; d.channel = d.channel || net.channel;
      d.encryption = d.encryption || net.privacy; d.cipher = d.cipher || net.cipher; d.auth = d.auth || net.auth;
    }
    if (!S.firstSeen.has(d.mac)) {
      S.firstSeen.set(d.mac, now);
      if (S.ready) {
        const netLabel = d.essid ? ` on <b>${d.essid}</b>` : '';
        logEv('join', `<b>${d.mac.substring(0,8)}\u2026</b> appeared (${d.signal} dBm, ${d.distance})${netLabel}`);
        if (d.distance === 'close' && d.signal !== -1) playProximityTone();
      }
    }
    d._firstSeen = S.firstSeen.get(d.mac);
    const prevDist = S.prevDistances.get(d.mac);
    if (S.ready && prevDist && prevDist !== d.distance && d.distance !== 'unknown') {
      const closer = (d.distance === 'close' && prevDist !== 'close') || (d.distance === 'medium' && prevDist === 'far');
      if (closer) {
        logEv('move', `<b>${d.mac.substring(0,8)}\u2026</b> moved closer (${prevDist} \u2192 ${d.distance})`);
        if (d.distance === 'close') playProximityTone();
      }
    }
    S.prevDistances.set(d.mac, d.distance);

    const hist = S.pktHist.get(d.mac) || [];
    const prev = S.devices.has(d.mac) ? (S.devices.get(d.mac).packets||0) : 0;
    hist.push(Math.max(0, (d.packets||0) - prev));
    if (hist.length > 8) hist.shift();
    S.pktHist.set(d.mac, hist);

    const sh = S.sigHist.get(d.mac) || [];
    sh.push(d.signal);
    if (sh.length > 30) sh.shift();
    S.sigHist.set(d.mac, sh);

    S.devices.set(d.mac, d);
  });

  S.prevMACs.forEach(mac => {
    if (!currentMACs.has(mac)) {
      const old = S.devices.get(mac);
      logEv('leave', `<b>${mac.substring(0,8)}\u2026</b> dropped${old&&old.essid?' from <b>'+old.essid+'</b>':''}`);
      S.devices.delete(mac); S.pktHist.delete(mac); S.firstSeen.delete(mac); S.prevDistances.delete(mac); S.sigHist.delete(mac);
    }
  });

  const all = [...S.devices.values()];
  const withSignal = all.filter(d => d.signal !== -1);
  document.getElementById('m-total').textContent = data.count || 0;
  document.getElementById('m-active').textContent = data.active_count || 0;
  document.getElementById('m-close').textContent = withSignal.filter(d=>d.distance==='close').length;
  document.getElementById('m-med').textContent = withSignal.filter(d=>d.distance==='medium').length;
  document.getElementById('m-far').textContent = withSignal.filter(d=>d.distance==='far').length;
  document.getElementById('m-rand').textContent = all.filter(d=>d._type==='random').length;

  const highThreats = all.filter(d => d.threat_level === 'high');
  const mThreats = document.getElementById('m-threats');
  if (mThreats) mThreats.textContent = highThreats.length;

  const ps = data.presence_summary || {};
  const mFamiliar = document.getElementById('m-familiar');
  const mUnknown = document.getElementById('m-unknown');
  if (mFamiliar) mFamiliar.textContent = ps.familiar || 0;
  if (mUnknown) mUnknown.textContent = ps.unknown || 0;

  document.getElementById('m-total-sub').textContent = all.length+' MACs';
  document.getElementById('m-active-sub').textContent = (data.count ? Math.round(((data.active_count||0)/data.count)*100) : 0)+'% active';
  S.countHistory.push(data.count || 0);
  if (S.countHistory.length > 60) S.countHistory.shift();

  renderTable(); renderRadar(all); renderNetworks(); renderDensity(data.count||0);
  renderTop(all); renderFeed(); generateInsights(all, S.networks); renderInsights(); renderSparkline();
  renderTypeBreakdown(all); renderQuickStats(all, S.networks); renderThreats(all);

  S._lastServerEvents = data.events || [];
  S._lastHistory = data.history || null;
  S._lastRegistry = data.registry || null;

  if (data.system) renderPiSystem(data.system);
  if (data.rf) renderRfMap(data.rf);
  if (data.history) renderAnalytics(data.history, data.registry);
  if (data.events) renderEventLog(data.events);

  S.prevMACs = currentMACs; S.ready = true; S.tick++;
}

/* ═══ THREAT PANEL ═══ */
function renderThreats(devs) {
  const el = document.getElementById('threats-list');
  if (!el) return;
  const threats = devs.filter(d => d.threat_level && d.threat_level !== 'low')
    .sort((a,b) => (b.threat_score||0) - (a.threat_score||0));

  const reasonLabels = {random_mac:'Random MAC',probing:'Probing',strong_signal:'Strong Signal',high_pkt_rate:'High Traffic',new_device:'New'};
  el.innerHTML = threats.map(d => {
    const alias = getAlias(d.mac);
    const macDisp = alias || d.mac;
    const lvlClass = d.threat_level === 'high' ? 'threat-high' : 'threat-med';
    const reasons = (d.threat_reasons||[]).map(r => reasonLabels[r]||r).join(' \u00B7 ');
    return `<div class="threat-item ${lvlClass}"><div class="threat-head"><span class="threat-mac">${macDisp}</span><span class="threat-badge ${lvlClass}">${d.threat_level.toUpperCase()} (${d.threat_score})</span></div><div class="threat-detail">${d.vendor||'Unknown'} \u00B7 ${d.signal!==-1?d.signal+' dBm':'no sig'} \u00B7 ${reasons}</div></div>`;
  }).join('') || '<div style="padding:16px;text-align:center;color:var(--t4);font-size:10px">No elevated threats</div>';
}

/* ═══ TIMELINE PAGE ═══ */
let tlRange = '24', tlSearch = '';

function initTimelinePage() {
  const rangeEl = document.getElementById('tl-range');
  const searchEl = document.getElementById('tl-search');
  if (rangeEl) rangeEl.addEventListener('change', e => { tlRange = e.target.value; fetchTimeline(); });
  if (searchEl) searchEl.addEventListener('input', e => { tlSearch = e.target.value.toLowerCase(); fetchTimeline(); });
}

async function fetchTimeline() {
  const el = document.getElementById('tl-content');
  if (!el) return;
  try {
    const hours = tlRange === '1h' ? 1 : tlRange === '6h' ? 6 : tlRange === '7d' ? 168 : 24;
    const macParam = tlSearch ? `&mac=${encodeURIComponent(tlSearch.toUpperCase())}` : '';
    const res = await fetch(API_BASE + '/api/timeline?hours=' + hours + macParam);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderTimeline(data.events || []);
  } catch(e) {
    el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t4);font-size:10px">Failed to load timeline</div>';
  }
}

function renderTimeline(evts) {
  const el = document.getElementById('tl-content');
  if (!el) return;
  if (tlSearch) {
    const q = tlSearch;
    evts = evts.filter(e => (e.mac||'').toLowerCase().includes(q) || (getAlias(e.mac)||'').toLowerCase().includes(q));
  }
  const grouped = {};
  evts.forEach(e => {
    if (!grouped[e.mac]) grouped[e.mac] = [];
    grouped[e.mac].push(e);
  });
  const icons = {device_join:'\u{1F4E1}',device_leave:'\u{1F50C}',close_alert:'\u{1F6A8}',new_ap:'\u{1F4E1}',burst:'\u26A1'};
  const labels = {device_join:'Joined',device_leave:'Left',close_alert:'Close Range',new_ap:'New AP',burst:'Packet Burst'};
  const macs = Object.keys(grouped).sort((a,b) => {
    const la = grouped[a][0]?.ts||0, lb = grouped[b][0]?.ts||0;
    return lb - la;
  });
  if (macs.length === 0) {
    el.innerHTML = '<div style="padding:30px;text-align:center;color:var(--t4);font-size:11px">No events in this time range</div>';
    return;
  }
  el.innerHTML = macs.map(mac => {
    const alias = getAlias(mac);
    const devEvents = grouped[mac].slice(0, 30);
    const evHtml = devEvents.map(e => {
      const dt = new Date(e.ts * 1000);
      const t = dt.toLocaleTimeString('en-US',{hour12:true,hour:'numeric',minute:'2-digit'});
      const icon = icons[e.type] || '\u2022';
      const label = labels[e.type] || e.type;
      return `<div class="tl-event"><div class="tl-line"></div><div class="tl-dot tl-${e.type}"></div><div class="tl-ev-body"><span class="tl-time">${t}</span><span class="tl-label">${icon} ${label}</span>${e.detail ? '<span class="tl-detail">' + e.detail + '</span>' : ''}</div></div>`;
    }).join('');
    return `<div class="tl-device"><div class="tl-device-head"><span class="tl-device-mac">${alias ? '<b>' + alias + '</b> <span style="color:var(--t4);font-size:9px">' + mac + '</span>' : mac}</span><span class="tl-device-count">${devEvents.length} events</span></div><div class="tl-events">${evHtml}</div></div>`;
  }).join('');
}

/* ═══ MAIN LOOP ═══ */
async function poll() {
  const t0 = Date.now();
  try {
    const res = await fetch(API_BASE+'/devices?_='+t0);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    S.pollLatency = Date.now() - t0;
    processData(data);
  } catch(e) { console.error('[OW]', e); S.failCount++; S.pollLatency = Date.now() - t0; updateConnHealth(); }
}

/* ═══ WEBSOCKET CONNECTION ═══ */
let _ws = null, _wsOk = false;
function connectWS() {
  try {
    const wsUrl = API_BASE.replace(/^http/, 'ws') + '/ws';
    _ws = new WebSocket(wsUrl);
    _ws.onopen = () => { _wsOk = true; console.log('[OW] WebSocket connected'); };
    _ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        S.pollLatency = 0;
        processData(data);
      } catch(err) { console.error('[OW] WS parse error', err); }
    };
    _ws.onclose = () => { _wsOk = false; setTimeout(connectWS, 3000); };
    _ws.onerror = () => { _wsOk = false; _ws.close(); };
  } catch(e) {
    _wsOk = false;
    setTimeout(connectWS, 5000);
  }
}

connectWS();
setInterval(() => { if (!_wsOk) poll(); }, 5000);
poll();

initTimelinePage();
fetchWeather();
fetchSunData();
setInterval(fetchWeather, 300000);
setInterval(fetchSunData, 3600000);
