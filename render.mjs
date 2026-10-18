#!/usr/bin/env node
// SMUVE Fitness text-meme reel renderer (photo / color-card reels).
// For reels cut from real video clips, use Kenny's engine: tools/smuve_reel_engine.py.
//
// Usage: node render.mjs reels/<id>.json [more.json ...]   or   node render.mjs --all
// Output: out/<id>.mp4, out/<id>.mp4.meta.json (text boxes for tools/smuve_audit.py) and out/<id>.json (post metadata).
// Every output is built to pass tools/smuve_audit.py: 1080x1920, 30fps, H.264 High yuv420p bt709 tv-range,
// AAC 48kHz stereo at -14 LUFS / -1 dBTP, 5-12s (15s for long formats), no near-black frames,
// a high-contrast first frame, and no text in the bottom 20% or right 15%.
// Needs ffmpeg + ffprobe on PATH. Paths are passed to ffmpeg relative to this folder,
// because Windows drive colons break ffmpeg filter arguments.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
process.chdir(ROOT);
const W = 1080, H = 1920, FPS = 30;
const SAFE_RIGHT_X = Math.floor(W * 0.85) - 12;   // audit: no text right of 85% width
const SAFE_BOTTOM_Y = Math.floor(H * 0.80) - 12;  // audit: no text below 80% height
const MAX_TEXT_W = 2 * (SAFE_RIGHT_X - W / 2);    // centred text must stay inside the right band
const LONG_FORMATS = new Set(['split', 'product', 'product_forward', 'debate']);

const BROLL = [
  'assets/photos/back-pullup-38641901.jpg', 'assets/photos/back-row-17210045.jpg',
  'assets/photos/chest-press-11432959.jpg', 'assets/photos/gym-background.jpg',
  'assets/photos/hero-training.jpg', 'assets/photos/legs-press-18060020.jpg',
  'assets/photos/legs-press-19254709.jpg', 'assets/photos/reel-chest-1.jpg',
  'assets/photos/back-pulldown-29218860.jpg', 'assets/photos/back-pullup-36389483.jpg',
];
const KENNY = 'assets/photos/kenny-gym-real.jpg';
// card-black is charcoal, not black: the audit's blackdetect (pix_th 0.10) flags near-black frames.
const CARDS = { 'card-black': '0x262626', 'card-white': '0xf4f4f4', 'card-red': '0xc8102e', 'card-green': '0x13804a' };
const HEADER_BAND = '0x161616';

function findFont() {
  const candidates = [
    'assets/fonts/Anton-Regular.ttf', 'assets/fonts/impact.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
  ];
  const f = candidates.find(p => fs.existsSync(p));
  if (!f) throw Error('No font found. Put Anton-Regular.ttf (OFL) in assets/fonts/.');
  return f;
}
const FONT = findFont();
// Logos are white-on-black with no alpha: use brightness as alpha so they sit on any background.
const KEY = 'format=gray,split[ka][kb];[ka]lut=c0=255,format=rgba[kw];[kw][kb]alphamerge';
// Measured average glyph width / font size (Impact ~0.45-0.47; DejaVu Sans Bold is much wider).
const CHAR_W = FONT.includes('DejaVu') ? 0.68 : 0.48;
const V_ENC = ['-c:v', 'libx264', '-profile:v', 'high', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p',
  '-r', String(FPS), '-color_range', 'tv', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709'];
const TO_TV = `scale=out_range=tv:out_color_matrix=bt709,format=yuv420p`;

function ff(args) {
  const r = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...args], { encoding: 'utf8', maxBuffer: 64 << 20 });
  if (r.status !== 0) throw Error(`ffmpeg failed (${r.status}): ${(r.stderr || '').slice(-2000)}`);
  return r;
}
const probeDur = p => Number(spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p], { encoding: 'utf8' }).stdout);
const textW = (s, size) => Math.ceil(s.length * size * CHAR_W);

function wrap(text, size, maxW) {
  const out = [];
  for (const para of String(text).split('\n')) {
    let cur = '';
    for (const word of para.split(/\s+/).filter(Boolean)) {
      if (!cur) cur = word;
      else if (textW(cur + ' ' + word, size) <= maxW) cur += ' ' + word;
      else { out.push(cur); cur = word; }
    }
    if (cur) out.push(cur);
  }
  return out;
}

const STYLES = {
  meme:  { size: 88, color: 'white', border: 7, box: false, upper: true },
  big:   { size: 122, color: 'white', border: 9, box: false, upper: true },
  label: { size: 78, color: 'black', border: 0, box: 'white', upper: false },
  small: { size: 58, color: 'white', border: 5, box: false, upper: false },
  brand: { size: 66, color: 'white', border: 0, box: false, upper: true },
};

// Normalise a scene line: string | {text, at, style}. Lines reveal one by one, and the
// gap shrinks on short scenes so the last line still gets >= ~1.1s on screen.
function normLines(scene, idx) {
  const n = (scene.lines || []).length;
  const gap = scene.revealGap ?? Math.min(0.9, Math.max(0.4, (scene.seconds - 1.2) / Math.max(1, n - 1)));
  return (scene.lines || []).map((l, i) => {
    const o = typeof l === 'string' ? { text: l } : { ...l };
    if (o.at === undefined) o.at = scene.reveal === false ? 0 : i * gap;
    o.at = Math.max(0, Math.min(o.at, scene.seconds - 0.8));
    if (!o.style) o.style = (scene.bg === 'card-white') ? 'label' : 'meme';
    if (/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(o.text)) throw Error(`Scene ${idx}: emoji are not renderable on screen: "${o.text}"`);
    return o;
  });
}

function layoutText(scene, idx, tmp, hookBar = false) {
  const lines = normLines(scene, idx);
  // Hook bar: the opening line sits black-on-white like a classic meme caption (Kenny's caption_bar look).
  // It also gives frame 1 the contrast the audit demands (luma std >= 50).
  const bar = hookBar && scene.bg !== 'card-white' && lines.length > 0;
  const rows = [];
  for (const [li, l] of lines.entries()) {
    const st = STYLES[l.style] || STYLES.meme;
    let txt = st.upper ? l.text.toUpperCase() : l.text;
    // Brand lines like "SMUVE Training Club - $9.99/mo - link in bio" break after the name, never mid-phrase.
    if (l.style === 'brand') txt = txt.replace(/\s+-\s+/, '\n');
    const maxW = MAX_TEXT_W - (st.box ? 36 : 2 * st.border);
    const rst = bar && li === 0 ? { ...st, color: 'black', border: 0, box: false, bar: true } : st;
    for (const row of wrap(txt, st.size, maxW)) rows.push({ row, st: rst, at: l.at, gapBefore: rows.length && li !== rows.at(-1).li ? (bar && li === 1 ? 70 : 30) : 0, li });
  }
  const lineH = r => Math.round(r.st.size * 1.18) + (r.st.box ? 24 : 0);
  const total = rows.reduce((s, r) => s + lineH(r) + r.gapBefore, 0);
  const pos = scene.pos || 'center';
  // Top ~380px holds the watermark and platform header; keep the block above the bottom 20% band.
  let y = pos === 'top' ? 400 : pos === 'bottom' ? SAFE_BOTTOM_Y - 140 - total : Math.round((H - total) / 2) - 40;
  if (y + total > SAFE_BOTTOM_Y) y = SAFE_BOTTOM_Y - total;
  if (y < 380) y = 380;
  if (y + total > SAFE_BOTTOM_Y) throw Error(`Scene ${idx}: too much text to fit the safe area; cut lines or split the scene`);
  const boxes = [];
  const barRows = rows.filter(r => r.st.bar);
  let barFilter = null;
  if (barRows.length) {
    const bh = barRows.reduce((a, r) => a + lineH(r), 0);
    barFilter = `drawbox=x=0:y=${y - 34}:w=iw:h=${bh + 56}:color=white:t=fill`;
  }
  const filters = rows.map((r, ri) => {
    y += r.gapBefore;
    const file = path.posix.join(tmp, `s${idx}_r${ri}.txt`);
    fs.writeFileSync(file, r.row);
    const color = r.st.box ? r.st.color : (scene.bg === 'card-white' && r.st.border === 0 ? 'black' : r.st.color);
    const parts = [
      `fontfile=${FONT}`, `textfile=${file}`, 'expansion=none', `fontsize=${r.st.size}`,
      `fontcolor=${color}`, `x=(w-text_w)/2`, `y=${y}`,
    ];
    // Lines that open the scene appear instantly (the audit samples frame 1); later lines pop in fast.
    if (r.at > 0) parts.push(`enable='gte(t,${r.at.toFixed(2)})'`, `alpha='min(1,(t-${r.at.toFixed(2)})/0.10)'`);
    if (r.st.border) parts.push(`borderw=${r.st.border}`, 'bordercolor=black');
    if (r.st.box) parts.push('box=1', `boxcolor=${r.st.box}`, 'boxborderw=18');
    const w = textW(r.row, r.st.size) + (r.st.box ? 36 : 2 * r.st.border);
    boxes.push({ x0: Math.round((W - w) / 2), y0: y, x1: Math.round((W + w) / 2), y1: y + lineH(r), label: r.row.slice(0, 24) });
    y += lineH(r);
    return 'drawtext=' + parts.join(':');
  });
  if (barFilter) filters.unshift(barFilter);
  return { filters, boxes };
}

function pickBg(scene, i, reelIndex) {
  if (scene.bg === 'kenny') return { photo: KENNY };
  if (scene.bg in CARDS) return { color: CARDS[scene.bg] };
  if (scene.bg && scene.bg.startsWith('photo:')) return { photo: scene.bg.slice(6) };
  if (scene.bg && scene.bg.startsWith('#')) return { color: '0x' + scene.bg.slice(1) };
  return { photo: BROLL[(reelIndex * 3 + i) % BROLL.length] }; // broll-neutral / default
}

function renderScene(scene, i, reelIndex, tmp) {
  const out = path.posix.join(tmp, `scene${i}.mp4`);
  const secs = scene.seconds;
  const frames = Math.round(secs * FPS);
  const bg = pickBg(scene, i, reelIndex);
  const { filters: text, boxes } = layoutText(scene, i, tmp, i === 0 && scene.hookBar !== false);
  let input, chain;
  if (bg.photo) {
    if (!fs.existsSync(bg.photo)) throw Error(`Missing photo ${bg.photo}`);
    const zoomIn = (scene.zoom || (i % 2 ? 'out' : 'in')) === 'in';
    const z = zoomIn ? `1+0.10*on/${frames}` : `1.10-0.10*on/${frames}`;
    input = ['-loop', '1', '-framerate', String(FPS), '-t', secs.toFixed(3), '-i', bg.photo];
    chain = [
      `scale=${W * 1.25}:${H * 1.25}:force_original_aspect_ratio=increase`, `crop=${W * 1.25}:${H * 1.25}`,
      `zoompan=z='${z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=${W}x${H}:fps=${FPS}`,
      `drawbox=x=0:y=0:w=iw:h=ih:color=black@${scene.dim ?? 0.34}:t=fill`,
    ];
  } else {
    input = ['-f', 'lavfi', '-i', `color=c=${bg.color}:s=${W}x${H}:r=${FPS}:d=${secs.toFixed(3)}`];
    // Light cards get a dark header band so the white watermark stays visible.
    chain = bg.color === CARDS['card-white'] ? [`drawbox=x=0:y=120:w=iw:h=240:color=${HEADER_BAND}:t=fill`] : [];
  }
  chain.push(...text, 'setsar=1', TO_TV);
  ff([...input, '-vf', chain.join(','), '-frames:v', String(frames), ...V_ENC, '-crf', '17', out]);
  return { file: out, boxes };
}

function renderEndCard(spec, secs, tmp) {
  const out = path.posix.join(tmp, 'endcard.mp4');
  const l1 = path.posix.join(tmp, 'end1.txt'), l2 = path.posix.join(tmp, 'end2.txt');
  fs.writeFileSync(l1, spec.endLine1 || 'SMUVE TRAINING CLUB');
  fs.writeFileSync(l2, spec.endLine2 || '$9.99/MO  -  LINK IN BIO');
  const dt = (f, size, y, color) => `drawtext=fontfile=${FONT}:textfile=${f}:expansion=none:fontsize=${size}:fontcolor=${color}:x=(w-text_w)/2:y=${y}`;
  ff(['-f', 'lavfi', '-i', `color=c=${CARDS['card-black']}:s=${W}x${H}:r=${FPS}:d=${secs}`, '-i', 'assets/brand/smuvefitness-logo-square.png',
    '-filter_complex', `[1:v]scale=600:600,${KEY}[lg];[0:v][lg]overlay=(W-w)/2:420,${dt(l1, 80, 1080, 'white')},${dt(l2, 62, 1195, '0xd4af37')},setsar=1,${TO_TV}`,
    '-frames:v', String(Math.round(secs * FPS)), ...V_ENC, '-crf', '17', out]);
  const box = (txtPath, size, y) => { const s = fs.readFileSync(txtPath, 'utf8'); const w = textW(s, size); return { x0: Math.round((W - w) / 2), y0: y, x1: Math.round((W + w) / 2), y1: y + Math.round(size * 1.18), label: 'endcard' }; };
  return { file: out, boxes: [box(l1, 80, 1080), box(l2, 62, 1195)] };
}

// Original, royalty-free drum bed synthesised from scratch (kick, snare, hats). It only exists so the
// file is never silent; trending audio is added in-app (Metricool audioConfiguration or the app itself).
function renderAudio(spec, dur, tmp, seed) {
  const bpm = spec.bpm || [92, 96, 100, 104, 88][seed % 5];
  const B = (60 / bpm).toFixed(5), HB = (30 / bpm).toFixed(5);
  const tb = `mod(t,${B})`, th = `mod(t,${HB})`, bi = `floor(t/${B})`;
  const kick = `sin(2*PI*(46*${tb}+3.6*(1-exp(-25*${tb}))))*exp(-7*${tb})`;
  const snare = `if(mod(${bi},2),(random(0)*2-1)*exp(-18*${tb})*0.55+sin(2*PI*185*${tb})*exp(-22*${tb})*0.25,0)`;
  const hat = `(random(1)*2-1)*exp(-85*${th})*0.16`;
  const expr = `0.8*${kick}+${snare}+${hat}`;
  const raw = path.posix.join(tmp, 'bed.wav'), out = path.posix.join(tmp, 'bed.m4a');
  ff(['-f', 'lavfi', '-i', `aevalsrc='${expr}|${expr}':s=48000:d=${dur.toFixed(3)}`, '-af', `acompressor=threshold=0.25:ratio=4:attack=3:release=80,alimiter=limit=0.5:level=false,afade=t=out:st=${Math.max(0, dur - 0.35).toFixed(3)}:d=0.35`, raw]);
  // Two-pass loudnorm to -14 LUFS / -1 dBTP (the audit window is -16.5..-11.5).
  const m = spawnSync('ffmpeg', ['-hide_banner', '-v', 'info', '-i', raw, '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json', '-f', 'null', '-'], { encoding: 'utf8' });
  const j = JSON.parse((m.stderr.match(/\{[^{}]*"input_i"[^{}]*\}/s) || ['{}'])[0]);
  const ln = j.input_i ? `loudnorm=I=-14:TP=-1.5:LRA=11:linear=true:measured_I=${j.input_i}:measured_TP=${j.input_tp}:measured_LRA=${j.input_lra}:measured_thresh=${j.input_thresh}` : 'loudnorm=I=-14:TP=-1.5:LRA=11';
  ff(['-i', raw, '-af', `${ln},alimiter=limit=0.70:level=false,aresample=48000`, '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-ac', '2', out]);
  return { file: out, bpm };
}

export function renderReel(specPath, reelIndex = 0) {
  const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
  const id = spec.id || path.basename(specPath, '.json');
  const tmp = path.posix.join('.tmp', id);
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });
  fs.mkdirSync('out', { recursive: true });

  // Fit to the audit's length window: 5-12s (15s for long formats), end card included.
  const auditFormat = spec.auditFormat || 'overlay';
  const maxSec = spec.maxSeconds ?? (LONG_FORMATS.has(auditFormat) ? 15 : 12);
  const endSecs = spec.endCard === false ? 0 : (spec.endCardSeconds ?? 1.0);
  const scenes = spec.scenes.map(s => ({ ...s, seconds: Number(s.seconds) || 2.5 }));
  const sum = scenes.reduce((a, s) => a + s.seconds, 0);
  const budget = maxSec - 0.2 - endSecs;
  let fitted = false;
  if (sum > budget) {
    const f = budget / sum;
    scenes.forEach(s => { s.seconds = Math.max(1.3, +(s.seconds * f).toFixed(2)); });
    fitted = true;
  }
  if (scenes.reduce((a, s) => a + s.seconds, 0) + endSecs > maxSec) throw Error(`Too many scenes to fit ${maxSec}s; cut a scene`);

  const parts = scenes.map((s, i) => renderScene(s, i, reelIndex, tmp));
  if (endSecs) parts.push(renderEndCard(spec, endSecs, tmp));
  const list = path.posix.join(tmp, 'list.txt');
  fs.writeFileSync(list, parts.map(p => `file '${path.posix.basename(p.file)}'`).join('\n'));
  const joined = path.posix.join(tmp, 'joined.mp4');
  ff(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', joined]);
  const dur = parts.reduce((s, p) => s + probeDur(p.file), 0);
  const audio = renderAudio(spec, dur, tmp, reelIndex + id.length);

  // Watermark (logo keyed by brightness + handle) on every scene except the end card.
  const handle = path.posix.join(tmp, 'handle.txt');
  fs.writeFileSync(handle, spec.handle || '@kenny2smuve');
  const wmEnd = (dur - endSecs).toFixed(2);
  const out = path.posix.join('out', `${id}.mp4`);
  ff(['-i', joined, '-loop', '1', '-framerate', String(FPS), '-i', 'assets/brand/smuvefitness-logo.png', '-i', audio.file,
    '-filter_complex',
    `[1:v]scale=420:-1,${KEY},colorchannelmixer=aa=0.85[lg];` +
    `[0:v][lg]overlay=x=(W-w)/2:y=150:shortest=1:enable='lt(t,${wmEnd})',` +
    `drawtext=fontfile=${FONT}:textfile=${handle}:expansion=none:fontsize=40:fontcolor=white@0.85:borderw=3:bordercolor=black@0.6:x=(w-text_w)/2:y=300:enable='lt(t,${wmEnd})',${TO_TV}[v]`,
    '-map', '[v]', '-map', '2:a', '-shortest', ...V_ENC, '-c:a', 'copy', '-movflags', '+faststart', out]);

  const g = spawnSync('ffmpeg', ['-v', 'error', '-ss', '0.03', '-i', out, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'], { maxBuffer: 16 << 20 });
  const px = g.stdout; let mean = 0; for (const v of px) mean += v; mean /= px.length || 1;
  let vr = 0; for (const v of px) vr += (v - mean) ** 2; const frame1Std = Math.sqrt(vr / (px.length || 1));
  const boxes = parts.flatMap(p => p.boxes);
  fs.writeFileSync(`${out}.meta.json`, JSON.stringify({ file: path.posix.basename(out), format: auditFormat, duration: +dur.toFixed(2), text_boxes: boxes }, null, 2));
  const meta = {
    id, file: out, seconds: Number(dur.toFixed(2)), fittedToMax: fitted, frame1Std: +frame1Std.toFixed(1), format: spec.format, auditFormat, hook: spec.hook,
    caption: spec.caption, youtubeTitle: spec.youtubeTitle, audioVibe: spec.audioVibe, audioBed: `synth drums ${audio.bpm} bpm`,
    status: 'draft-awaiting-kenny-approval',
  };
  fs.writeFileSync(path.posix.join('out', `${id}.json`), JSON.stringify(meta, null, 2));
  fs.rmSync(tmp, { recursive: true, force: true });
  return meta;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let files = process.argv.slice(2);
  if (files.includes('--all')) files = fs.readdirSync('reels').filter(f => f.endsWith('.json')).sort().map(f => path.posix.join('reels', f));
  if (!files.length) { console.error('Usage: node render.mjs reels/<id>.json | --all'); process.exit(2); }
  let failed = 0;
  files.forEach((f, i) => {
    try { const m = renderReel(f, i); console.log(`ok   ${m.file}  ${m.seconds}s${m.fittedToMax ? '  (scenes shortened to fit)' : ''}${m.frame1Std < 52 ? `  WARN frame-1 contrast ${m.frame1Std} (audit needs 50)` : ''}`); }
    catch (e) { failed++; console.error(`FAIL ${f}: ${e.message}`); }
  });
  process.exitCode = failed ? 1 : 0;
}
