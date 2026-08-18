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
global.echarts = undefined;
const echarts = require('echarts');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync('report.html', 'utf8');
const realInit = echarts.init;
global.echarts = echarts;

function renderChart(code, idx) {
  let captured = null, chartRef = null;
  echarts.init = function (dom, theme, opts) {
    const isFc = /echart-forecast-/.test(code);
    const h = (opts && opts._h) || (isFc ? 440 : 640);
    const w = (opts && opts._w) || 1100;
    // animation:false 关键：SSR 默认开动画会启动内部 timer，使 Node 事件循环不空、
    // 进程跑完不退出（CI 下卡死 job）。关掉 + 渲染后 dispose 彻底释放资源。
    const chart = realInit(null, null, { renderer: 'svg', ssr: true, width: w, height: h, animation: false });
    chartRef = chart;
    const origSet = chart.setOption.bind(chart);
    chart.setOption = (o) => { captured = o; origSet(o); };
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
    if ([0,1,5,6,8].includes(idx)) fs.writeFileSync('_c' + idx + '.svg', svg);
    try { chartRef.dispose(); } catch (e) {}
    return { svg };
  } catch (e) { return { err: 'render: ' + e.message }; }
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

const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m, idx = 0, totalOverlap = 0;
const out = [];
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
  idx++;
}
console.log(out.join('\n'));
console.log('\n=== TOTAL OVERLAPPING LABEL PAIRS: ' + totalOverlap + ' ===');
// 保留中文标签（原 replace(/[^\x00-\x7F]/g,'') 会把重叠的中文标签清空成 ""，无法定位）
fs.writeFileSync('_ov.txt', 'total=' + totalOverlap + '\n' + out.join('\n') + '\n');
// 兜底退出：即使 echarts 残留句柄也确保进程干净结束（CI 不卡死）。
process.exit(0);
