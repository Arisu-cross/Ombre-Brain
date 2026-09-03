"""
Memory Flux page / 记忆乱流页面

这一页是什么
------------------------------------------------------------------
银河(/galaxy)讲的是**时间**:一条记忆一颗星,越早越靠银心。
乱流(/flux)讲的是**关系**:一条记忆铺成好几个字符,漂在字流里;静的时候它只是流,
点一个字才把它牵着的那些记忆拉出来。

它顶掉的是 2026-08-31 砍掉的那个又丑又空的「记忆网络」——同一件事,
换个好看的说法。数据走 /api/flux(三种关联:related / tag / vector)。

字为什么是字母和数字
------------------------------------------------------------------
和参考效果一样,全部用拉丁字母 / 数字 / 符号,不用汉字。
我一开始自作主张改成「记忆标题的第一个字」,想着能认出是哪条 —— 但汉字笔画重、
字宽也宽,几百个撒下去是一片黑压压的方块,不是流。栖栖对着原效果指出来了,
改回字母数字。想知道是哪条,点它,卡片里有中文标题。

每条记忆的「主字」由它的 id 定死(同一条记忆每次打开都是同一个字母),
比投影字符略大略深,是这条记忆在流里的锚点。

位置是真随机的
------------------------------------------------------------------
第一版用黄金角均匀铺,结果排出了肉眼可见的列和斜带 —— 太整齐,不像乱流。
现在用一个按下标定死的伪随机数发生器(seeded PRNG):看着是乱的,但每次打开
位置一样,不会每次刷新都跳。

为什么这一页是 .py 而不是 .html
------------------------------------------------------------------
和 galaxy_page.py 同一个理由:Zeabur 自动部署会沿用**缓存的旧构建计划**,
新增的 .html 进不了镜像,线上就是 404。`COPY *.py .` 新旧计划都有。
(2026-08-29 那次 galaxy.html 排查见 ob-backup/SYSTEM-HANDBOOK.md §10。)

配色
------------------------------------------------------------------
跟面板同一套蓝白液态玻璃(#F4F8FF 底、#2B5BC4 主色),不是银河那套深色。
域的颜色也和面板列表里的小标签一致。
"""

FLUX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>记忆乱流</title>

<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="乱流">
<meta name="theme-color" content="#F4F8FF">

<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,600;1,400&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">

<style>
:root{
  --bg:#F4F8FF;
  --text:#12244C;
  --dim:#4A6294;
  --light:#7C93BF;
  --accent:#2B5BC4;
  --border:rgba(70,110,180,.24);
  --glass:rgba(255,255,255,.56);
  --glass-edge:rgba(255,255,255,.78);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',sans-serif;
  color:var(--text);
  background:
    radial-gradient(54% 40% at 96% 14%,rgba(210,232,255,.62) 0%,rgba(210,232,255,0) 62%),
    radial-gradient(78% 46% at 40% 106%,rgba(176,208,250,.46) 0%,rgba(176,208,250,0) 66%),
    linear-gradient(180deg,#FBFCFF 0%,#F4F8FF 52%,#EEF4FE 100%);
  background-attachment:fixed;
}
#cv{position:fixed;inset:0;display:block;touch-action:none}

/* ── 顶部 ── */
.head{
  position:fixed;top:0;left:0;right:0;z-index:10;pointer-events:none;
  padding:calc(env(safe-area-inset-top) + 14px) 18px 0;
  display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
}
.head h1{
  font-family:'Cormorant Garamond',serif;font-weight:600;font-size:25px;
  color:var(--accent);letter-spacing:.3px;line-height:1.15;
}
.head .zh{font-size:11.5px;color:var(--light);margin-top:2px;letter-spacing:.02em}
.back{
  pointer-events:auto;flex-shrink:0;text-decoration:none;
  border:1px solid var(--border);background:rgba(255,255,255,.78);
  backdrop-filter:blur(18px) saturate(1.6);-webkit-backdrop-filter:blur(18px) saturate(1.6);
  border-radius:99px;padding:6px 13px;font-size:12px;color:var(--dim);white-space:nowrap;
}

/* ── 图例 ── */
.legend{
  position:fixed;left:18px;top:calc(env(safe-area-inset-top) + 62px);z-index:10;
  display:flex;flex-direction:column;gap:5px;pointer-events:none;
  font-size:11px;color:var(--light);transition:opacity .3s ease;
}
.legend.hide{opacity:0}
.legend i{display:inline-block;width:26px;height:0;vertical-align:middle;margin-right:7px}
.legend .l1{border-top:1.6px solid rgba(43,91,196,.75)}
.legend .l2{border-top:1.6px dashed rgba(138,123,208,.8)}
.legend .l3{border-top:1.6px dotted rgba(78,154,154,.9)}
.hint{
  position:fixed;left:0;right:0;bottom:calc(env(safe-area-inset-bottom) + 18px);z-index:9;
  text-align:center;font-size:12px;color:var(--light);pointer-events:none;
  transition:opacity .5s ease;
}
.hint.gone{opacity:0}

/* ── 详情卡:面板同款液态玻璃 ── */
#card{
  position:fixed;left:12px;right:12px;bottom:calc(env(safe-area-inset-bottom) + 12px);
  z-index:20;max-height:56vh;overflow-y:auto;-webkit-overflow-scrolling:touch;
  background:var(--glass);
  backdrop-filter:blur(56px) saturate(1.9);-webkit-backdrop-filter:blur(56px) saturate(1.9);
  border:1px solid var(--glass-edge);border-radius:22px;
  box-shadow:0 -10px 40px rgba(30,60,120,.16);
  padding:16px 16px 18px;
  transform:translateY(130%);transition:transform .34s cubic-bezier(.4,0,.2,1);
}
#card.open{transform:translateY(0)}
.c-head{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px}
.c-title{
  flex:1;min-width:0;font-size:17px;font-weight:600;line-height:1.4;
  font-family:'Inter','PingFang SC',sans-serif;
}
.c-x{
  border:none;background:transparent;color:var(--light);font-size:20px;line-height:1;
  cursor:pointer;padding:0 2px;flex-shrink:0;
}
.c-meta{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:11px}
.c-dom{font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px}
.c-day{font-size:11.5px;color:var(--light)}
.c-body{
  background:rgba(255,255,255,.56);border:1px solid rgba(255,255,255,.72);
  border-radius:13px;padding:12px 13px;font-size:13.5px;line-height:1.82;
  white-space:pre-wrap;color:var(--text);max-height:30vh;overflow-y:auto;
}
.c-rel{margin-top:11px;font-size:12px;color:var(--dim);line-height:1.9}
.c-rel b{color:var(--text);font-weight:600}
.c-rel .row{display:flex;gap:8px;align-items:baseline}
.c-rel .row span:first-child{color:var(--light);flex-shrink:0}
.c-open{
  display:inline-block;margin-top:12px;text-decoration:none;
  background:linear-gradient(135deg,#2B5BC4,#4E7BD8);color:#fff;
  border-radius:99px;padding:8px 18px;font-size:12.5px;
}

/* ── 加载 / 出错 ── */
#msg{
  position:fixed;inset:0;z-index:30;display:flex;align-items:center;justify-content:center;
  text-align:center;padding:32px;font-size:13.5px;color:var(--dim);line-height:2;
}
#msg a{color:var(--accent)}
#msg.gone{display:none}
</style>
</head>
<body>

<canvas id="cv"></canvas>

<div class="head">
  <div>
    <h1>Memory Flux</h1>
    <div class="zh" id="zh">记忆乱流</div>
  </div>
  <a class="back" href="dashboard">← 面板</a>
</div>

<div class="legend hide" id="legend">
  <div><i class="l1"></i>存入时关联到一起</div>
  <div><i class="l2"></i>共享同一个标签</div>
  <div><i class="l3"></i>说的是相近的事</div>
</div>

<div class="hint" id="hint">点一个字，看它牵着谁</div>

<div id="card">
  <div class="c-head">
    <div class="c-title" id="cTitle"></div>
    <button class="c-x" id="cX" aria-label="关闭">×</button>
  </div>
  <div class="c-meta" id="cMeta"></div>
  <div class="c-body" id="cBody"></div>
  <div class="c-rel" id="cRel"></div>
  <a class="c-open" id="cOpen" href="dashboard">在面板里打开</a>
</div>

<div id="msg">正在把记忆铺开…</div>

<script>
/* ═══════════════════════════════════════════════════════════════
   记忆乱流
   一条记忆铺成好几个字母/数字,漂在一层噪音字符里。
   点一个字 → 锁定它,把一跳/二跳的关联画出来。
   拖拽平移、双指捏合缩放、双击复位。
   ═══════════════════════════════════════════════════════════════ */

// 域的颜色:和面板列表里的小标签同一套(记忆银河的颜色语言压到白底上)
var DOMAIN_COLOR = {
  '恋爱':'#C9548F','关系核心':'#C9548F','婚恋':'#C9548F','我们的故事':'#B85BAE',
  '情感':'#C9548F','感情':'#C9548F','关系':'#BE5C9A','心情':'#C4708E',
  '亲密':'#C9548F','亲密关系':'#C9548F','友谊':'#4E93B4','性':'#C25084',
  '自省':'#6E5EBE','心理':'#6E5EBE','情绪':'#7F5EBE','内心':'#7F5EBE','AI':'#7B5EC4',
  '编程':'#5566C4','网络':'#4E6EC0','数字':'#4E6EC0','学习':'#8A7B3E','阅读':'#8A7B3E',
  '家庭':'#B8813A','宠物':'#B8763A','居家':'#B08A46','回忆':'#A8813A',
  '工作':'#9A8534','求职':'#9A8534','创作':'#9A55BE','写作':'#8A5EC0','游戏':'#B84EAE',
  '社交':'#4E7BC4','人际':'#5A76B4','日常':'#5F739C','出行':'#4B87A4','健康':'#5A9060',
  '饮食':'#B0803E','归档':'#7286A8','沉淀物':'#7B6EA8','未分类':'#7C8AA4',
  '计划':'#96863E','音乐':'#A05EB4','影视':'#8E6EB0','财务':'#9A8542','购物':'#B0765E','兴趣':'#9A6EC0'
};
function domColor(d){ return DOMAIN_COLOR[d] || '#5F739C'; }
function hexA(hex,a){ var n=parseInt(hex.slice(1),16);
  return 'rgba('+(n>>16)+','+((n>>8)&255)+','+(n&255)+','+a+')'; }

var LINK_STYLE = {
  related: { color:'43,91,196',   dash:[],      label:'存入时关联到一起' },
  tag:     { color:'138,123,208', dash:[5,4],   label:'共享同一个标签' },
  vector:  { color:'78,154,154',  dash:[1.5,4], label:'说的是相近的事' }
};

// 铺底的噪音字。它们不是记忆,不可点,只负责让画面像一片流动的字。
// 投影字符集:和参考效果一样,拉丁字母 + 数字 + 符号。
// 每条记忆除了一个「主字」(它标题的第一个字),还会铺出好几个这样的投影字符 ——
// 密度是这一屏的命根子:一条记忆只画一个字的话,几百条撒在屏幕上就是一片空白,
// 根本不像「流」。
var GLYPHS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%*()[]{}';

var canvas = document.getElementById('cv');
var ctx = canvas.getContext('2d');
var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

var W = 0, H = 0, DPR = 1;
var worldW = 0, worldH = 0;
var scale = 1, panX = 0, panY = 0;
var nodes = [], noise = [], traces = [], links = [], adj = {};
var selected = null, hop1 = {}, hop2 = {};
var attractT = 0, attractTo = 0;   // 0=各漂各的,1=已经聚到选中的那条身边
var panAnim = null;                // 把选中的那条挪到屏幕上三分之一处
var pulses = [];
var startedAt = performance.now();

function resize(){
  DPR = Math.min(2, window.devicePixelRatio || 1);
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
  canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
window.addEventListener('resize', function(){ resize(); layoutWorld(); });

// 世界只比屏幕大一点点。
// 之前按记忆条数放大世界,结果 250 条记忆摊在三四屏里,一屏就剩几十个字 = 一片空白。
// 密度该靠字的数量堆出来,不是靠把它们摊开。
function layoutWorld(){
  worldW = W * 1.25;
  worldH = H * 1.45;
}

// 一个按种子定死的伪随机数发生器(mulberry32)。
// 位置用它算 —— 看着乱,但每次打开都一样,刷新不会满屏跳。
function rnd(seed){
  var t = (seed + 0x6D2B79F5) | 0;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}
function hashText(s){
  var h = 2166136261;
  for (var i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}

// 每条记忆的主字:由 id 定死的一个字母/数字,同一条记忆永远是同一个。
function glyphOf(item){
  return GLYPHS[hashText(String(item.id)) % GLYPHS.length];
}

// 位置:真随机撒。
// 第一版用黄金角均匀铺,排出了看得见的列和斜带,整整齐齐的一点也不「乱流」。
function place(i){
  return { x: rnd(i * 3 + 1), y: rnd(i * 3 + 2) };
}

function buildScene(data){
  var items = data.nodes || [];
  var coarse = window.matchMedia('(pointer: coarse)').matches;

  // 总共要铺多少个字。手机上少一点,免得每帧画一千多个字把电烧了。
  var target = coarse ? 900 : 1300;
  var perMemory = Math.max(1, Math.min(8, Math.round(target / Math.max(1, items.length))));

  // 每条记忆 = 一个主字(它标题的第一个字,有颜色、能点) + 若干投影字符(灰的,也能点,
  // 点到哪个都算点到同一条记忆)。投影就是参考效果里那些满屏的字母数字。
  nodes = [];
  var total = items.length * perMemory;
  var k = 0;
  items.forEach(function(it, i){
    for (var j = 0; j < perMemory; j++){
      var p = place(k);
      nodes.push({
        item: it,
        primary: j === 0,
        // 投影字符也由 (id, 第几个投影) 定死,不是每次刷新重掷
        glyph: j === 0 ? glyphOf(it)
                       : GLYPHS[hashText(it.id + ':' + j) % GLYPHS.length],
        x: p.x, y: p.y,
        depth: 0.32 + rnd(k * 7 + 3) * 0.68,      // 远近:大小、飘速都跟着它
        speed: 0.6 + rnd(k * 7 + 5) * 1.1,        // 每个字自己的速度,不齐步走
        sway: rnd(k * 7 + 9) * Math.PI * 2,
        color: domColor(it.domain)
      });
      k++;
    }
  });

  // 再撒一层纯噪音字符:它们不是记忆,不可点,只负责把画面填满
  var noiseCount = coarse ? 260 : 380;
  noise = [];
  for (var n = 0; n < noiseCount; n++){
    noise.push({
      ch: GLYPHS[Math.floor(Math.random() * GLYPHS.length)],
      x: Math.random(), y: Math.random(),
      depth: 0.3 + Math.random() * 0.7,
      speed: 0.6 + Math.random() * 1.1,
      a: 0.10 + Math.random() * 0.18
    });
  }

  // 光迹:向上流的细线。没有这一层,画面只是一把字撒在那儿,不像「流」。
  traces = [];
  var traceCount = window.matchMedia('(pointer: coarse)').matches ? 90 : 130;
  for (var t = 0; t < traceCount; t++){
    traces.push({
      x: Math.random(), y: Math.random(),
      len: 0.05 + Math.random() * 0.16,      // 归一化世界高度
      depth: 0.35 + Math.random() * 0.65,
      speed: 0.6 + Math.random() * 1.4,
      sway: Math.random() * Math.PI * 2,
      a: 0.05 + Math.random() * 0.11
    });
  }

  links = data.links || [];
  adj = {};
  var byId = {};
  nodes.forEach(function(n, i){ if (n.primary) byId[n.item.id] = i; });   // 线连主字
  links.forEach(function(l){
    l.ia = byId[l.a]; l.ib = byId[l.b];
    if (l.ia === undefined || l.ib === undefined) return;
    (adj[l.a] = adj[l.a] || []).push(l);
    (adj[l.b] = adj[l.b] || []).push(l);
  });
  links = links.filter(function(l){ return l.ia !== undefined && l.ib !== undefined; });

  layoutWorld();
  document.getElementById('zh').textContent =
    '记忆乱流 · ' + items.length + ' 条记忆 · ' + links.length + ' 条关联';
}

// 记忆自己漂到哪儿了(归一化世界坐标)
function naturalOf(n, now){
  var drift = reduceMotion ? 0 : (now - startedAt) / 1000 * 0.012 * n.depth * (n.speed || 1);
  var sway = reduceMotion ? 0 : Math.sin((now - startedAt) / 2600 + n.sway) * 0.006;
  return { x: n.x + sway, y: ((n.y - drift) % 1 + 1) % 1 };
}

// 世界坐标 → 屏幕坐标。
// 选中一条记忆时,它牵着的那些会被「吸」到它身边围成一圈(attractT 是过渡量)——
// 不这么做的话,关联线会横穿整屏拉到看不见的地方,等于告诉你「有关系」却不告诉你
// 「跟谁」。吸过来之后一眼就看得见对面是哪个字。
function screenOf(n, now){
  var nat = naturalOf(n, now);
  if (n.attract && attractT > 0.001 && selected){
    var base = naturalOf(selected, now);
    var t = attractT;
    nat = {
      x: nat.x + (base.x + n.attract.dx - nat.x) * t,
      y: nat.y + (base.y + n.attract.dy - nat.y) * t
    };
  }
  var wx = (nat.x - 0.5) * worldW;
  var wy = (nat.y - 0.5) * worldH;
  return { x: wx * scale + W / 2 + panX, y: wy * scale + H / 2 + panY };
}

function fontSize(n){
  if (!n.primary) return (7 + n.depth * 6) * scale;   // 投影字符:小、退到后面去
  var imp = n.item.importance == null ? 5 : n.item.importance;
  var base = 9 + imp * 0.55;                          // 主字:重要度越高越大
  if (n.item.pinned) base += 2;
  return base * (0.8 + n.depth * 0.3) * scale;
}

function draw(){
  var now = performance.now();

  // 吸附与镜头的过渡:每帧往目标走一小步,不用动画库
  attractT += (attractTo - attractT) * 0.14;
  if (panAnim){
    panX += (panAnim.x - panX) * 0.16;
    panY += (panAnim.y - panY) * 0.16;
    if (Math.abs(panAnim.x - panX) < 0.5 && Math.abs(panAnim.y - panY) < 0.5) panAnim = null;
  }

  ctx.clearRect(0, 0, W, H);

  // ① 光迹:一条条向上流的细线,给这一屏「流」的感觉
  var dim = selected ? 0.4 : 1;
  for (var t = 0; t < traces.length; t++){
    var tr = traces[t];
    var pt = screenOf(tr, now);
    var lenPx = tr.len * worldH * scale;
    if (pt.x < -20 || pt.x > W + 20 || pt.y + lenPx < -20 || pt.y > H + 20) continue;
    var g = ctx.createLinearGradient(pt.x, pt.y, pt.x, pt.y + lenPx);
    g.addColorStop(0, 'rgba(70,110,180,' + (tr.a * dim) + ')');
    g.addColorStop(1, 'rgba(70,110,180,0)');
    ctx.strokeStyle = g;
    ctx.lineWidth = tr.a > 0.11 ? 1.1 : 0.7;
    ctx.beginPath(); ctx.moveTo(pt.x, pt.y); ctx.lineTo(pt.x, pt.y + lenPx); ctx.stroke();
  }

  // ② 噪音字
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  for (var i = 0; i < noise.length; i++){
    var m = noise[i];
    var p = screenOf(m, now);
    if (p.x < -40 || p.x > W + 40 || p.y < -40 || p.y > H + 40) continue;
    ctx.font = (7 + m.depth * 6) * scale + 'px Inter, "SF Mono", ui-monospace, monospace';
    ctx.fillStyle = 'rgba(90,125,190,' + (m.a * dim) + ')';
    ctx.fillText(m.ch, p.x, p.y);
  }

  // ③ 关联线:只画选中那条牵着的
  var pos = new Array(nodes.length);
  for (var k = 0; k < nodes.length; k++) pos[k] = screenOf(nodes[k], now);

  if (selected !== null){
    links.forEach(function(l){
      var inHop1 = (l.a === selected.item.id || l.b === selected.item.id);
      var other = l.a === selected.item.id ? l.b : l.a;
      var inHop2 = !inHop1 && (hop1[l.a] || hop1[l.b]);
      if (!inHop1 && !inHop2) return;
      var st = LINK_STYLE[l.kind] || LINK_STYLE.tag;
      var pa = pos[l.ia], pb = pos[l.ib];
      ctx.save();
      ctx.setLineDash(st.dash.map(function(v){ return v * scale; }));
      ctx.strokeStyle = 'rgba(' + st.color + ',' + (inHop1 ? 0.62 : 0.13) + ')';
      ctx.lineWidth = (inHop1 ? 1.4 : 1) * Math.min(1.6, scale);
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
      ctx.restore();
    });
  } else {
    // 没选中时,偶尔让一对有关联的记忆之间闪过一道光 —— 提示「这里是有线的」
    if (!reduceMotion && links.length && Math.random() < 0.02){
      pulses.push({ l: links[Math.floor(Math.random() * links.length)], t: now });
    }
    pulses = pulses.filter(function(p){ return now - p.t < 1500; });
    pulses.forEach(function(p){
      var age = (now - p.t) / 1500;
      var st = LINK_STYLE[p.l.kind] || LINK_STYLE.tag;
      var pa = pos[p.l.ia], pb = pos[p.l.ib];
      ctx.save();
      ctx.strokeStyle = 'rgba(' + st.color + ',' + (Math.sin(age * Math.PI) * 0.22) + ')';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
      ctx.restore();
    });
  }

  // ④ 记忆的字
  for (var j = 0; j < nodes.length; j++){
    var n = nodes[j], p = pos[j];
    if (p.x < -60 || p.x > W + 60 || p.y < -60 || p.y > H + 60) continue;
    var fs = fontSize(n);
    var sameMem = selected && selected.item.id === n.item.id;
    var isSel = sameMem && n.primary;          // 只有主字放大发光,它的投影跟着亮
    var lit = sameMem || (selected && hop1[n.item.id]);
    var near = selected && hop2[n.item.id];
    var alpha;
    if (!selected) alpha = n.primary ? (0.30 + n.depth * 0.26) : (0.14 + n.depth * 0.18);
    else if (isSel) alpha = 1;
    else if (lit) alpha = n.primary ? 0.92 : 0.5;
    else if (near) alpha = n.primary ? 0.4 : 0.18;
    else alpha = n.primary ? 0.13 : 0.07;

    ctx.font = (n.primary ? (isSel ? 700 : 600) + ' ' : '') +
      (isSel ? fs * 1.7 : fs) + 'px "SF Mono", ui-monospace, Menlo, Inter, monospace';

    // 没选中的时候整片是灰的 —— 域的颜色只在「被点到的那一簇」上出现。
    // 一上来就满屏彩色中文会花得像糖果,也就看不出哪儿才是重点了。
    var col = (selected && (sameMem || hop1[n.item.id])) ? n.color : '#5A78AF';
    if (isSel){
      ctx.save();
      // 身后垫一团淡光,免得被围过来的那一圈字盖住
      var halo = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, fs * 1.9);
      halo.addColorStop(0, hexA(col, .2));
      halo.addColorStop(1, hexA(col, 0));
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(p.x, p.y, fs * 1.9, 0, Math.PI * 2); ctx.fill();
      ctx.shadowColor = hexA(col, .5); ctx.shadowBlur = 16 * Math.min(2, scale);
      ctx.fillStyle = hexA(col, 1);
      ctx.fillText(n.glyph, p.x, p.y);
      ctx.restore();
    } else {
      ctx.fillStyle = hexA(col, alpha);
      ctx.fillText(n.glyph, p.x, p.y);
    }
  }

  requestAnimationFrame(draw);
}

// ── 点中哪个字 ──
function pick(sx, sy){
  var now = performance.now(), best = null, bestScore = 1e9;
  for (var i = 0; i < nodes.length; i++){
    var n = nodes[i];
    var p = screenOf(n, now);
    var d = Math.hypot(p.x - sx, p.y - sy);
    var r = n.primary ? Math.max(20, fontSize(n) * 1.0) : Math.max(11, fontSize(n) * 0.85);
    if (d > r) continue;
    // 主字优先:它更大、更好认,手指落在两者之间时该选它
    var score = d - (n.primary ? 14 : 0);
    if (score < bestScore){ bestScore = score; best = n; }
  }
  if (!best) return null;
  if (best.primary) return best;
  // 点到的是投影 —— 换成那条记忆的主字,后面的吸附和高亮都以主字为准
  for (var j = 0; j < nodes.length; j++){
    if (nodes[j].primary && nodes[j].item.id === best.item.id) return nodes[j];
  }
  return best;
}

function fmtDay(v){
  if (!v) return '';
  var d = new Date(v);
  if (isNaN(d)) return String(v).slice(0, 10);
  var now = new Date();
  var y = d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + ' 年 ';
  return y + (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
}

var card = document.getElementById('card');
function select(n){
  selected = n;
  hop1 = {}; hop2 = {};
  nodes.forEach(function(x){ x.attract = null; });
  if (!n){
    attractTo = 0; panAnim = null;
    card.classList.remove('open');
    document.getElementById('legend').classList.add('hide');
    return;
  }

  (adj[n.item.id] || []).forEach(function(l){
    hop1[l.a === n.item.id ? l.b : l.a] = l;
  });

  // 把一跳的记忆围成一圈放到它身边。半径先按屏幕算(约屏幕短边的四分之一),
  // 再换算回世界坐标 —— 这样不管缩放到多少、记忆库多大,圈看上去都一样大。
  var ring = Object.keys(hop1);
  var byId = {};
  nodes.forEach(function(x){ if (x.primary) byId[x.item.id] = x; });   // 只吸主字
  var radiusPx = Math.min(W, H) * (ring.length > 8 ? 0.30 : 0.24);
  var rx = radiusPx / Math.max(1, worldW * scale);
  var ry = radiusPx / Math.max(1, worldH * scale);
  ring.forEach(function(id, i){
    var node = byId[id];
    if (!node) return;
    var a = (i / ring.length) * Math.PI * 2 - Math.PI / 2;
    node.attract = { dx: Math.cos(a) * rx, dy: Math.sin(a) * ry };
  });
  attractTo = 1;

  // 镜头:把选中的那条挪到屏幕横向居中、纵向三分之一处,给底下的卡片让位置
  var nat = naturalOf(n, performance.now());
  panAnim = {
    x: -((nat.x - 0.5) * worldW * scale),
    y: H * 0.3 - H / 2 - ((nat.y - 0.5) * worldH * scale)
  };
  Object.keys(hop1).forEach(function(id){
    (adj[id] || []).forEach(function(l){
      var other = l.a === id ? l.b : l.a;
      if (other !== n.item.id && !hop1[other]) hop2[other] = true;
    });
  });

  var it = n.item;
  document.getElementById('cTitle').textContent = it.name || (it.content || '').slice(0, 18) || '（没有标题）';
  var meta = document.getElementById('cMeta');
  meta.innerHTML = '';
  var dom = document.createElement('span');
  dom.className = 'c-dom';
  dom.style.color = domColor(it.domain);
  dom.style.background = hexA(domColor(it.domain), .14);
  dom.textContent = it.domain;
  meta.appendChild(dom);
  var day = document.createElement('span');
  day.className = 'c-day';
  day.textContent = fmtDay(it.created);
  meta.appendChild(day);

  document.getElementById('cBody').textContent = it.content || '（这条记忆没有正文）';

  var rel = document.getElementById('cRel');
  var ls = adj[it.id] || [];
  if (!ls.length){
    rel.innerHTML = '<span style="color:var(--light)">还没有牵到别的记忆</span>';
  } else {
    var byId = {};
    nodes.forEach(function(x){ byId[x.item.id] = x.item; });
    var rows = ls.slice(0, 8).map(function(l){
      var other = byId[l.a === it.id ? l.b : l.a];
      var st = LINK_STYLE[l.kind] || LINK_STYLE.tag;
      var nm = other ? (other.name || (other.content || '').slice(0, 12)) : '（已不在流里）';
      return '<div class="row"><span>' + esc(st.label) + '</span><b>' + esc(nm) + '</b></div>';
    }).join('');
    rel.innerHTML = '<div style="margin-bottom:4px">牵着 ' + ls.length + ' 条记忆' +
      (ls.length > 8 ? '（列前 8 条）' : '') + '</div>' + rows;
  }
  document.getElementById('cOpen').href = 'dashboard';
  card.classList.add('open');
  document.getElementById('legend').classList.remove('hide');
  document.getElementById('hint').classList.add('gone');
}

function esc(s){
  var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML;
}

document.getElementById('cX').addEventListener('click', function(){ select(null); });

// ── 手势:拖拽平移 / 双指捏合 / 双击复位 ──
var pointers = new Map(), dragged = false, downAt = 0, pinch = null;

canvas.addEventListener('pointerdown', function(e){
  canvas.setPointerCapture(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  dragged = false; downAt = performance.now();
  if (pointers.size === 2){
    var pts = [...pointers.values()];
    pinch = { d: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y), scale: scale };
  }
});

canvas.addEventListener('pointermove', function(e){
  if (!pointers.has(e.pointerId)) return;
  var prev = pointers.get(e.pointerId);
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (pointers.size === 2 && pinch){
    var pts = [...pointers.values()];
    var d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
    scale = Math.max(0.45, Math.min(4, pinch.scale * (d / pinch.d)));
    dragged = true;
    return;
  }
  panX += e.clientX - prev.x;
  panY += e.clientY - prev.y;
  if (Math.abs(e.clientX - prev.x) + Math.abs(e.clientY - prev.y) > 1) dragged = true;
});

function up(e){
  if (!pointers.has(e.pointerId)) return;
  var wasSingle = pointers.size === 1;
  pointers.delete(e.pointerId);
  if (pointers.size < 2) pinch = null;
  if (wasSingle && !dragged && performance.now() - downAt < 500){
    var n = pick(e.clientX, e.clientY);
    select(n);           // 点空白处 = 取消选择
  }
}
canvas.addEventListener('pointerup', up);
canvas.addEventListener('pointercancel', up);

canvas.addEventListener('wheel', function(e){
  e.preventDefault();
  scale = Math.max(0.45, Math.min(4, scale * (e.deltaY > 0 ? 0.92 : 1.08)));
}, { passive: false });

canvas.addEventListener('dblclick', function(){
  scale = 1; panX = 0; panY = 0; select(null);
});

// ── 取数据 ──
resize();
fetch('api/flux', { credentials: 'same-origin' })
  .then(function(r){
    if (r.status === 401) throw new Error('unauth');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  })
  .then(function(data){
    if (data.error) throw new Error(data.error);
    if (!data.nodes || !data.nodes.length){
      document.getElementById('msg').innerHTML = '记忆库还是空的。<br>先去<a href="dashboard">面板</a>存点什么。';
      return;
    }
    buildScene(data);
    document.getElementById('msg').classList.add('gone');
    requestAnimationFrame(draw);
    setTimeout(function(){ document.getElementById('hint').classList.add('gone'); }, 6000);
  })
  .catch(function(e){
    var msg = document.getElementById('msg');
    if (e.message === 'unauth'){
      msg.innerHTML = '要先在<a href="dashboard">面板</a>登录，<br>乱流才看得到你的记忆。';
    } else {
      msg.innerHTML = '没读到记忆：' + esc(e.message) + '<br>过一会儿再刷新试试。';
    }
  });
</script>
</body>
</html>
"""
