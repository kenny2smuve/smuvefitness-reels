#!/usr/bin/env node
// SMUVE Fitness reel renderer.
// Usage: node render.mjs reels/<id>.json [more.json ...]   or   node render.mjs --all
// Output: out/<id>.mp4 (1080x1920, 30fps, H.264 + silent AAC) and out/<id>.json (post metadata).
// Needs ffmpeg + ffprobe on PATH. Paths are passed to ffmpeg relative to this folder,
// because Windows drive colons break ffmpeg filter arguments.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
process.chdir(ROOT);
const W = 1080, H = 1920, FPS = 30;

const BROLL = [
  'assets/photos/back-pullup-38641901.jpg', 'assets/photos/back-row-17210045.jpg',
  'assets/photos/chest-press-11432959.jpg', 'assets/photos/gym-background.jpg',
  'assets/photos/hero-training.jpg', 'assets/photos/legs-press-18060020.jpg',
  'assets/photos/legs-press-19254709.jpg', 'assets/photos/reel-chest-1.jpg',
  'assets/photos/back-pulldown-29218860.jpg', 'assets/photos/back-pullup-36389483.jpg',
];
const KENNY = 'assets/photos/kenny-gym-real.jpg';
const CARDS = { 'card-black': '0x0b0b0b', 'card-white': '0xf4f4f4', 'card-red': '0xc8102e', 'card-green': '0x13804a' };

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
const CHAR_W = FONT.includes('DejaVu') ? 0.62 : 0.47; // average glyph width / font size

function ff(args) {
  const r = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...args], { encoding: 'utf8', maxBuffer: 64 << 20 });
  if (r.status !== 0) throw Error(`ffmpeg failed (${r.status}): ${(r.stderr || '').slice(-2000)}`);
}

function wrap(text, size, maxW = 940) {
  const maxChars = Math.max(6, Math.floor(maxW / (size * CHAR_W)));
  const out = [];
  for (const para of String(text).split('\n')) {
    let cur = '';
    for (const word of para.split(/\s+/).filter(Boolean)) {
      if (!cur) cur = word;
      else if ((cur + ' ' + word).length <= maxChars) cur += ' ' + word;
      else { out.push(cur); cur = word; }
    }
    if (cur) out.push(cur);
  }
  return out;
}

const STYLES = {
  meme:  { size: 92, color: 'white', border: 7, box: false, upper: true },
  big:   { size: 130, color: 'white', border: 9, box: false, upper: true },
  label: { size: 84, color: 'black', border: 0, box: 'white', upper: false },
  small: { size: 62, color: 'white', border: 5, box: false, upper: false },
  brand: { size: 70, color: 'white', border: 0, box: false, upper: true },
};

// Normalise a scene line: string | {text, at, style}
function normLines(scene, idx) {
  return (scene.lines || []).map((l, i) => {
    const o = typeof l === 'string' ? { text: l } : { ...l };
    if (o.at === undefined) o.at = scene.reveal === false ? 0 : Math.min(i * (scene.revealGap ?? 0.9), scene.seconds - 0.6);
    if (!o.style) o.style = (scene.bg === 'card-white') ? 'label' : 'meme';
    if (/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(o.text)) throw Error(`Scene ${idx}: emoji are not renderable on screen: "${o.text}"`);
    return o;
  });
}

function textFilters(scene, idx, tmp) {
  const lines = normLines(scene, idx);
  // Lay out every wrapped row of every line as one vertical stack.
  const rows = [];
  for (const [li, l] of lines.entries()) {
    const st = STYLES[l.style] || STYLES.meme;
    let txt = st.upper ? l.text.toUpperCase() : l.text;
    // Brand lines like "SMUVE Training Club - $9.99/mo - link in bio" break after the name, never mid-phrase.
    if (l.style === 'brand') txt = txt.replace(/\s+-\s+/, '\n');
    for (const row of wrap(txt, st.size)) rows.push({ row, st, at: l.at, gapBefore: rows.length && li !== rows.at(-1).li ? 34 : 0, li });
  }
  const lineH = r => Math.round(r.st.size * 1.18) + (r.st.box ? 26 : 0);
  const total = rows.reduce((s, r) => s + lineH(r) + r.gapBefore, 0);
  const pos = scene.pos || 'center';
  // Safe zone: platform UI covers ~the top 300px and bottom ~650px.
  let y = pos === 'top' ? 400 : pos === 'bottom' ? H - 680 - total : Math.round((H - total) / 2);
  const fontColor = scene.bg === 'card-white' ? 'black' : null;
  return rows.map((r, ri) => {
    y += r.gapBefore;
    const file = path.posix.join(tmp, `s${idx}_r${ri}.txt`);
    fs.writeFileSync(file, r.row);
    const color = r.st.box ? r.st.color : (fontColor && r.st.border === 0 ? fontColor : r.st.color);
    const parts = [
      `fontfile=${FONT}`, `textfile=${file}`, 'expansion=none', `fontsize=${r.st.size}`,
      `fontcolor=${color}`, `x=(w-text_w)/2`, `y=${y}`,
      `enable='gte(t,${r.at.toFixed(2)})'`, `alpha='min(1,(t-${r.at.toFixed(2)})/0.12)'`,
    ];
    if (r.st.border) parts.push(`borderw=${r.st.border}`, 'bordercolor=black');
    if (r.st.box) parts.push('box=1', `boxcolor=${r.st.box}`, 'boxborderw=18');
    y += lineH(r);
    return 'drawtext=' + parts.join(':');
  });
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
  const secs = Number(scene.seconds) || 2.5;
  const frames = Math.round(secs * FPS);
  const bg = pickBg(scene, i, reelIndex);
  const text = textFilters({ ...scene, seconds: secs }, i, tmp);
  let input, chain;
  if (bg.photo) {
    if (!fs.existsSync(bg.photo)) throw Error(`Missing photo ${bg.photo}`);
    const zoomIn = (scene.zoom || (i % 2 ? 'out' : 'in')) === 'in';
    const z = zoomIn ? `1+0.10*on/${frames}` : `1.10-0.10*on/${frames}`;
    input = ['-loop', '1', '-framerate', String(FPS), '-t', secs.toFixed(3), '-i', bg.photo];
    chain = [
      `scale=${W * 1.25}:${H * 1.25}:force_original_aspect_ratio=increase`, `crop=${W * 1.25}:${H * 1.25}`,
      `zoompan=z='${z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=${W}x${H}:fps=${FPS}`,
      `drawbox=x=0:y=0:w=iw:h=ih:color=black@${scene.dim ?? 0.38}:t=fill`,
    ];
  } else {
    input = ['-f', 'lavfi', '-i', `color=c=${bg.color}:s=${W}x${H}:r=${FPS}:d=${secs.toFixed(3)}`];
    // Light cards get a dark header band so the white watermark stays visible.
    chain = bg.color === CARDS['card-white'] ? ['drawbox=x=0:y=120:w=iw:h=240:color=0x0b0b0b:t=fill'] : [];
  }
  chain.push(...text, 'setsar=1', 'format=yuv420p');
  ff([...input, '-vf', chain.join(','), '-frames:v', String(frames), '-r', String(FPS),
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', out]);
  return out;
}

function renderEndCard(spec, tmp) {
  const secs = spec.endCardSeconds ?? 1.6;
  const out = path.posix.join(tmp, 'endcard.mp4');
  const l1 = path.posix.join(tmp, 'end1.txt'), l2 = path.posix.join(tmp, 'end2.txt');
  fs.writeFileSync(l1, spec.endLine1 || 'SMUVE TRAINING CLUB');
  fs.writeFileSync(l2, spec.endLine2 || '$9.99/MO  -  LINK IN BIO');
  const dt = (f, size, y, color) => `drawtext=fontfile=${FONT}:textfile=${f}:expansion=none:fontsize=${size}:fontcolor=${color}:x=(w-text_w)/2:y=${y}`;
  ff(['-f', 'lavfi', '-i', `color=c=0x0b0b0b:s=${W}x${H}:r=${FPS}:d=${secs}`, '-i', 'assets/brand/smuvefitness-logo-square.png',
    '-filter_complex', `[1:v]scale=620:620,${KEY}[lg];[0:v][lg]overlay=(W-w)/2:430,${dt(l1, 84, 1130, 'white')},${dt(l2, 64, 1250, '0xd4af37')},setsar=1,format=yuv420p`,
    '-frames:v', String(Math.round(secs * FPS)), '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', out]);
  return out;
}

export function renderReel(specPath, reelIndex = 0) {
  const spec = JSON.parse(fs.readFileSync(specPath, 'utf8'));
  const id = spec.id || path.basename(specPath, '.json');
  const tmp = path.posix.join('.tmp', id);
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.mkdirSync(tmp, { recursive: true });
  fs.mkdirSync('out', { recursive: true });
  const parts = spec.scenes.map((s, i) => renderScene(s, i, reelIndex, tmp));
  if (spec.endCard !== false) parts.push(renderEndCard(spec, tmp));
  const list = path.posix.join(tmp, 'list.txt');
  fs.writeFileSync(list, parts.map(p => `file '${path.posix.basename(p)}'`).join('\n'));
  const joined = path.posix.join(tmp, 'joined.mp4');
  ff(['-f', 'concat', '-safe', '0', '-i', list, '-c', 'copy', joined]);
  const dur = parts.reduce((s, p) => s + Number(spawnSync('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', p], { encoding: 'utf8' }).stdout), 0);
  // Watermark: wide logo keyed by brightness (white on black source) + handle, then silent audio.
  const handle = path.posix.join(tmp, 'handle.txt');
  fs.writeFileSync(handle, spec.handle || '@kenny2smuve');
  const out = path.posix.join('out', `${id}.mp4`);
  ff(['-i', joined, '-loop', '1', '-framerate', String(FPS), '-i', 'assets/brand/smuvefitness-logo.png', '-f', 'lavfi', '-t', dur.toFixed(3), '-i', 'anullsrc=r=44100:cl=stereo',
    '-filter_complex',
    `[1:v]scale=420:-1,${KEY},colorchannelmixer=aa=0.85[lg];` +
    `[0:v][lg]overlay=x=(W-w)/2:y=150:shortest=1:enable='lt(t,${(dur - (spec.endCard !== false ? (spec.endCardSeconds ?? 1.6) : 0)).toFixed(2)})',` +
    `drawtext=fontfile=${FONT}:textfile=${handle}:expansion=none:fontsize=40:fontcolor=white@0.85:borderw=3:bordercolor=black@0.6:x=(w-text_w)/2:y=300:enable='lt(t,${(dur - (spec.endCard !== false ? (spec.endCardSeconds ?? 1.6) : 0)).toFixed(2)})',format=yuv420p[v]`,
    '-map', '[v]', '-map', '2:a', '-shortest', '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-profile:v', 'high',
    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', out]);
  const meta = {
    id, file: out, seconds: Number(dur.toFixed(2)), format: spec.format, hook: spec.hook,
    caption: spec.caption, youtubeTitle: spec.youtubeTitle, audioVibe: spec.audioVibe,
    status: 'draft-awaiting-kenny-approval',
  };
  fs.writeFileSync(path.posix.join('out', `${id}.json`), JSON.stringify(meta, null, 2));
  fs.rmSync(tmp, { recursive: true, force: true });
  return meta;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let files = process.argv.slice(2);
  if (files.includes('--all')) files = fs.readdirSync('reels').filter(f => f.endsWith('.json')).map(f => path.posix.join('reels', f));
  if (!files.length) { console.error('Usage: node render.mjs reels/<id>.json | --all'); process.exit(2); }
  let failed = 0;
  files.forEach((f, i) => {
    try { const m = renderReel(f, i); console.log(`ok   ${m.file}  ${m.seconds}s`); }
    catch (e) { failed++; console.error(`FAIL ${f}: ${e.message}`); }
  });
  process.exitCode = failed ? 1 : 0;
}
