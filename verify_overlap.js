global.window = global;
global.document = {
  documentElement: { style: {} },
  getElementById: () => ({}),
  createElement: (tag) => {
    if (tag === 'canvas') {
      let _font = '12px sans-serif';
      return {
        style: {}, width: 0, height: 0, setAttribute() {},
        getContext: () => ({
          set font(v) { _font = v; }, get font() { return _font; },
          measureText: (s) => {
            const fm = /(\d+(?:\.\d+)?)px/.exec(_font); const fs = fm ? parseFloat(fm[1]) : 12;
            let w = 0; for (const ch of String(s)) { w += ch.codePointAt(0) > 0x2e80 ? fs * 1.0 : fs * 0.56; }
            return { width: w };
          },
          fillText() {}, save() {}, restore() {}, scale() {}, clearRect() {},
        }),
      };
    }
    return { style: {}, setAttribute() {}, appendChild() {} };
  },
  body: { appendChild() {}, style: {} },
  addEventListener() {},
};
global.window.addEventListener = () => {};
global.window.devicePixelRatio = 1;
global.navigator = { userAgent: 'node', platform: 'node' };
const fs = require('fs');
const path = require('path');
const vm = require('vm');

// R166: 让门禁在 echarts 未置于脚本目录 node_modules 时仍能解析(从 NODE_PATH / 工作区兜底)。
// 此前在 chanlun/ 下 require('echarts') 解析不到 managed 工作区的 echarts, 致门禁崩溃且被 `|| true` 静默吞掉。
function loadEcharts() {
  const candidates = ['echarts'];
  if (process.env.NODE_PATH) candidates.push(path.join(process.env.NODE_PATH, 'echarts'));
  const wb = process.env.WORKBUDDY_NODE_MODULES ||
    path.join(process.env.USERPROFILE || process.env.HOME || '', '.workbuddy', 'binaries', 'node', 'workspace', 'node_modules');
  if (wb) candidates.push(path.join(wb, 'echarts'));
  for (const c of candidates) {
    try { const e = require(c); if (e) return e; } catch (e) {}
  }
  return null;
}
const echarts = loadEcharts();
if (!echarts) {
  console.error('[verify_overlap] SKIP: echarts 不可用 (请 npm install echarts 或设置 NODE_PATH)。');
  process.exit(0);
}
global.echarts = echarts;

// R476: 加 OV_HTML 覆盖口子 —— 便于对拍「改动前 / 改动后」两份产物（此前只能就地替换
// report.html，A/B 时容易把构建产物搞坏）。缺省行为不变 = __dirname/report.html。
const html = fs.readFileSync(process.env.OV_HTML || path.join(__dirname, 'report.html'), 'utf8');
const realInit = echarts.init;
global.echarts = echarts;

// R476: 把某个 option 里的 markPoint 标签值并入集合（供运行时视口扫描判定「哪个框是我们
// 的标注」）。label.show===false 的条目跳过 —— 它在图上根本不渲染，不该再算作"我们的"。
function collectMarkValues(o, into) {
  try {
    for (const one of ((o && o.series) || [])) {
      const mp = one && one.markPoint;
      for (const d of ((mp && mp.data) || [])) {
        if (d && d.value != null && (!d.label || d.label.show !== false)) into.add(String(d.value));
      }
    }
  } catch (e) {}
}

function renderChart(code, idx) {
  let captured = null, chartRef = null;
  // R476: 累积所有 markPoint 的 label.value —— 用来结构化判定「哪个框是我们的标注」。
  // 不用文本正则猜（轴标签的日期可能长得像标签），也不用「最后一次 setOption 的 series」
  // （recomputeY 会再 setOption 一次且不带 series，会把 captured 冲掉）。
  const mkSet = new Set();
  // R476: 捕获 chart.on('dataZoom') 的 handler，供运行时视口扫描逐个触发（含前端 relayout）。
  const handlers = {};
  echarts.init = function (dom, theme, opts) {
    const isFc = /echart-forecast-/.test(code);
    const h = (opts && opts._h) || (isFc ? 440 : 640);
    const w = (opts && opts._w) || 1100;
    // animation:false 关键：SSR 默认开动画会启动内部 timer，使 Node 事件循环不空、
    // 进程跑完不退出（CI 下卡死 job）。关掉 + 渲染后 dispose 彻底释放资源。
    const chart = realInit(null, null, { renderer: 'svg', ssr: true, width: w, height: h, animation: false });
    chartRef = chart;
    const origSet = chart.setOption.bind(chart);
    chart.setOption = (o) => { captured = o; collectMarkValues(o, mkSet); origSet(o); };
    const origOn = chart.on.bind(chart);
    chart.on = (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); return origOn(ev, fn); };
    return chart;
  };
  // 每个图表块在独立 VM 上下文执行，避免跨块顶层 const/let 重名（如多图都用
  // `var chart`）在全局 (0,eval) 下累积声明导致后续块 SyntaxError 静默失效。
  const sandbox = {
    echarts: echarts,
    window: global.window,
    document: global.document,
    navigator: global.navigator,
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
  };
  try {
    vm.runInNewContext(code, sandbox, { filename: 'chart-block-' + idx + '.js' });
  } catch (e) { echarts.init = realInit; return { err: e.message }; }
  if (!chartRef) return { err: 'no chart' };
  try {
    const svg = chartRef.renderToSVGString();
    // R354: 调试 SVG 改经 env OV_SVG_DIR 重定向(CI 设 $RUNNER_TEMP, publish 目录外零污染)。
    // 旧版硬编码 [0,1,5,6,8] 写 CWD —— 与 report.html 现 12 块(main/forecast 交替)布局脱节,
    // 且真正报重叠的 forecast 块(3/5/7/11)反而不写盘, 诊断回溯失效; 现 OV_SVG_DIR 设置时全量写。
    // 无 env(本地跑)不写盘, 不再往仓库工作树丢 _c*.svg 调试残留。
    const svgDir = process.env.OV_SVG_DIR;
    if (svgDir) {
      try {
        if (!fs.existsSync(svgDir)) fs.mkdirSync(svgDir, { recursive: true });
        fs.writeFileSync(path.join(svgDir, '_c' + idx + '.svg'), svg);
      } catch (e) {}
    }
    // R476: 不在这里 dispose —— 运行时视口扫描需要 chart 存活（见主循环的 SWEEP 段），
    // 由调用方在扫描结束后统一释放。render 失败时仍就地释放，避免泄漏。
    return { svg, chart: chartRef, mkSet, handlers };
  } catch (e) {
    try { if (chartRef) chartRef.dispose(); } catch (e2) {}
    return { err: 'render: ' + e.message };
  }
}

// parse <text> boxes from SVG
function textBoxes(svg) {
  const boxes = [];
  const re = /<text\b([^>]*)>([\s\S]*?)<\/text>/gi;
  let m;
  while ((m = re.exec(svg))) {
    const attrs = m[1];
    let tx = 0, ty = 0, fs = 12, anchor = 'start';
    const gx = /transform="translate\(([-\d.]+)[ ,]([-\d.]+)\)"/.exec(attrs);
    const gm = /matrix\(0,-1,1,0,([-\d.]+),([-\d.]+)\)/.exec(attrs);
    const ax = /x="([-\d.]+)"/.exec(attrs);
    const ay = /y="([-\d.]+)"/.exec(attrs);
    const af = /font-size="([-\d.]+)"/.exec(attrs) || /font-size:\s*([\d.]+)px/.exec(attrs);
    const aa = /text-anchor="(\w+)"/.exec(attrs);
    if (af) fs = parseFloat(af[1]);
    if (aa) anchor = aa[1];
    if (gx) { tx = parseFloat(gx[1]); ty = parseFloat(gx[2]); }
    else if (gm) { tx = parseFloat(gm[1]); ty = parseFloat(gm[2]); }
    if (ax) tx += parseFloat(ax[1]);
    if (ay) ty += parseFloat(ay[1]);
    let content = m[2].replace(/<[^>]+>/g, '').trim();
    if (!content) continue;
    const rot = /rotate\(/.test(attrs) || /matrix\(0,-1,1,0/.test(attrs);
    // width estimate
    let w = 0;
    for (const ch of content) {
      const code = ch.codePointAt(0);
      w += (code > 0x2e80 ? fs * 1.0 : fs * 0.56);
    }
    w += fs * 0.2;
    let left = tx, top, right, bottom;
    if (rot) {
      // vertical text: visual width ~ line height, visual height ~ text length
      const wv = fs * 1.25, hv = w;
      left = tx - wv / 2; right = tx + wv / 2; top = ty - hv / 2; bottom = ty + hv / 2;
    } else {
      if (anchor === 'middle') left = tx - w / 2;
      else if (anchor === 'end') left = tx - w;
      top = ty - fs * 0.8; bottom = ty + fs * 0.3; right = left + w;
    }
    boxes.push({ left, top, right, bottom, text: content, fs });
  }
  return boxes;
}

function overlaps(a, b) {
  const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return ox > 1 && oy > 1 ? Math.min(ox, oy) : 0;
}

// R476: 运行时视口档位 —— 门禁此前只验「生成期 + 初始视口」，而实测表明主图的真实碰撞
// **只出现在缩放后**（初始视口由 report.py 的 dedup_mark_labels 决定并在上面已验；缩放后
// 由前端 relayout() 接管）。这里把同一套判据搬到运行时：改视口 → 逐个触发
// chart.on('dataZoom') 的 handler（含 relayout）→ 再真渲染 → 重新量框（同一个 textBoxes）。
// ⚠ 视口列表与 _dbg/r473/verify_relayout_real.js 的 SWEEP 逐档一致（0~96 共 16 档，含 R473
//   实测出问题的 10 / 90 / 93 / 96），便于两侧互相印证。
const SWEEP = [0, 10, 20, 30, 40, 50, 60, 70, 75, 80, 82, 85, 88, 90, 93, 96];
const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m, idx = 0, totalOverlap = 0, sweepTotal = 0, sweepOurs = 0;
const out = [];
const sweepOut = [];
while ((m = re.exec(html))) {
  const code = m[1];
  if (!/echarts\.init/.test(code)) continue;
  const isFc = /echart-forecast-/.test(code);
  const r = renderChart(code, idx);
  if (r.err) { out.push(`#${idx} (${isFc ? 'forecast' : 'main'}) ERROR: ${r.err}`); idx++; continue; }
  const boxes = textBoxes(r.svg);
  const ov = [];
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const o = overlaps(boxes[i], boxes[j]);
      if (o) ov.push(`"${boxes[i].text}" ✕ "${boxes[j].text}" (ov ${o.toFixed(0)}px)`);
    }
  totalOverlap += ov.length;
  out.push(`#${idx} (${isFc ? 'forecast' : 'main'}) texts=${boxes.length} overlaps=${ov.length}` +
    (ov.length ? '\n   ' + ov.slice(0, 12).join('\n   ') : ''));

  // ---- 运行时视口扫描（只对含 dataZoom handler 的**主图**做；预测图无缩放重排）----
  const hz = (r.handlers && r.handlers.dataZoom) || [];
  if (!isFc && hz.length && r.mkSet.size) {
    const rows = [];
    for (const st of SWEEP) {
      try { r.chart.setOption({ dataZoom: [{ start: st, end: 100 }] }); } catch (e) {}
      for (const fn of hz) { try { fn({}); } catch (e) {} }
      let s2;
      try { s2 = r.chart.renderToSVGString(); }
      catch (e) { rows.push(`  start=${st} ERR ${e.message}`); continue; }
      const b2 = textBoxes(s2);
      const ours = [];
      let all = 0;
      for (let i = 0; i < b2.length; i++)
        for (let j = i + 1; j < b2.length; j++) {
          const o = overlaps(b2[i], b2[j]);
          if (!o) continue;
          all++;
          // 「我们的标签」= markPoint 的 label.value（结构化判定）。轴刻度↔轴刻度之间的重叠
          // 归 ECharts 的 axisLabel.hideOverlap 管、不是本门禁的判据；但凡有一侧是我们的标签
          // 就算 —— 只有一侧也算（标注撞刻度、标注撞筹码条都属此类）。
          if (r.mkSet.has(b2[i].text) || r.mkSet.has(b2[j].text))
            ours.push(`"${b2[i].text}" ✕ "${b2[j].text}" (ov ${o.toFixed(0)}px)`);
        }
      sweepTotal += all;
      sweepOurs += ours.length;
      rows.push(`  start=${String(st).padStart(2)} texts=${String(b2.length).padStart(3)} ` +
        `全部重叠=${String(all).padStart(2)} 我方标签重叠=${String(ours.length).padStart(2)}` +
        (ours.length ? '  ✕ ' + ours.slice(0, 6).join(' ; ') : ''));
    }
    sweepOut.push(`#${idx} (main) 运行时视口扫描 ${SWEEP.length} 档：我方标签重叠 ${rows.filter(x => /✕/.test(x)).length} 档有重叠`);
    sweepOut.push(...rows);
  }
  try { r.chart.dispose(); } catch (e) {}
  idx++;
}
console.log(out.join('\n'));
console.log('\n=== TOTAL OVERLAPPING LABEL PAIRS: ' + totalOverlap + ' ===');
console.log('\n=== R476 运行时视口扫描（16 档 start=0..96，真渲染 + 触发 relayout）===');
console.log(sweepOut.length ? sweepOut.join('\n') : '（无主图 / 无 dataZoom handler，跳过）');
console.log('\n=== 运行时扫描合计：全部重叠 ' + sweepTotal + ' 对，其中涉及我方标注 ' + sweepOurs + ' 对 ===');
// 保留中文标签（原 replace(/[^\x00-\x7F]/g,'') 会把重叠的中文标签清空成 ""，无法定位）
// R354: 报告路径经 env OV_OUT 重定向 —— 旧版写 CWD/_ov.txt 会被 deploy publish path:. 上传到
// Pages 根(线上 HTTP 200 实证污染, 多轮互相覆盖); CI 设 OV_OUT=$RUNNER_TEMP/_ov.txt 出发布目录。
// 无 env(本地跑)回退 CWD 写盘保持向后兼容。
const ovPath = process.env.OV_OUT || '_ov.txt';
try {
  fs.writeFileSync(ovPath, 'total=' + totalOverlap + '\nsweep_total=' + sweepTotal +
    '\nsweep_ours=' + sweepOurs + '\n' + out.join('\n') + '\n--- R476 运行时视口扫描 ---\n' +
    sweepOut.join('\n') + '\n');
} catch (e) { console.error('[verify_overlap] WARN 写报告失败: ' + e.message); }
// 兜底退出：即使 echarts 残留句柄也确保进程干净结束（CI 不卡死）。
process.exit(0);
