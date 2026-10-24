/* 书签导航 · 扩展版
 *
 * 跟静态版的区别只有一处：数据不再是"生成时烧进页面"的，而是每次打开直接问浏览器要。
 * 所以收藏夹一改，页面立刻是最新的 —— 没有生成、没有刷新、没有中间步骤。
 *
 * 图标用浏览器自己的 favicon 缓存（chrome-extension://.../_favicon/），
 * 不联网、不抓取，也不用往页面里塞 base64。
 */
'use strict';

/* ---------- 1. 工具 ---------- */
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const letter = h => ((h || '#').replace(/^www\./, '')[0] || '#').toUpperCase();

const hostOf = u => { try { return new URL(u).hostname; } catch (e) { return ''; } };

function cleanTitle(title, url) {
  const t = (title || '').trim();
  if (t) return t;
  return hostOf(url).replace(/^www\./, '') || url;
}

/* 浏览器本地 favicon：离线可用，跟着浏览器自己的图标缓存走 */
function faviconUrl(u) {
  try {
    const url = new URL(chrome.runtime.getURL('/_favicon/'));
    url.searchParams.set('pageUrl', u);
    url.searchParams.set('size', '32');
    return url.toString();
  } catch (e) { return ''; }
}

/* ---------- 2. 把浏览器书签树转成页面要的结构 ---------- */

function folderFrom(bm, path) {
  const node = { name: (bm.title || '').trim() || '(未命名)', path, count: 0, urls: [], children: [] };
  for (const c of (bm.children || [])) {
    if (c.url) {
      node.urls.push({ t: cleanTitle(c.title, c.url), u: c.url, h: hostOf(c.url) });
    } else {
      const sub = folderFrom(c, [...path, (c.title || '').trim() || '(未命名)']);
      if (sub.count > 0) node.children.push(sub);      // 空文件夹不进导航页
    }
  }
  node.count = node.urls.length + node.children.reduce((s, c) => s + c.count, 0);
  return node;
}

/*
 * 浏览器给的树长这样：
 *   [{ id:"0", title:"", children:[ {id:"1", title:"收藏夹栏"}, {id:"2", title:"其他收藏夹"} ] }]
 * 「收藏夹栏」本身只是个容器，必须把它的 children 提升成真正的顶层，
 * 否则侧栏第一级会显示成一个叫"收藏夹栏"的节点，而不是 事务/娱乐/学习… 这些真实分类。
 */
function buildData(roots) {
  const root = roots && roots[0];
  const tops = [];

  for (const c of ((root && root.children) || [])) {
    if (c.id === '1') {
      const bar = c.children || [];
      for (const g of bar) {
        if (g.url) continue;                            // 裸书签在下面单独收
        const n = folderFrom(g, [(g.title || '').trim() || '(未命名)']);
        if (n.count > 0) tops.push(n);
      }
      // 直接挂在收藏夹栏下的书签，单独归一组，避免丢
      const loose = bar.filter(g => g.url);
      if (loose.length) {
        const box = { name: '收藏夹栏', path: ['收藏夹栏'], count: 0, urls: [], children: [] };
        loose.forEach(g => box.urls.push({
          t: cleanTitle(g.title, g.url), u: g.url, h: hostOf(g.url),
        }));
        box.count = box.urls.length;
        tops.push(box);
      }
    } else if (c.children && c.children.length) {
      const n = folderFrom(c, [(c.title || '').trim() || '(未命名)']);
      if (n.count > 0) tops.push(n);
    }
  }

  let total = 0, folders = 0;
  const countAll = n => { folders++; total += n.urls.length; n.children.forEach(countAll); };
  tops.forEach(countAll);
  return { total, folders, tree: tops };
}

/* ---------- 3. 索引 ---------- */
let DATA = { total: 0, folders: 0, tree: [] };
let NODES = [], BY_ID = {}, ALL = [];

function indexData() {
  NODES = [];
  ALL = [];
  const walk = (node, depth) => {
    NODES.push({
      id: node.path.join('/'), name: node.name, path: node.path, depth,
      count: node.count, urls: node.urls,
      children: node.children.map(c => c.path.join('/')),
    });
    node.children.forEach(c => walk(c, depth + 1));
  };
  DATA.tree.forEach(t => walk(t, 0));
  BY_ID = Object.fromEntries(NODES.map(n => [n.id, n]));

  const flat = node => {
    node.urls.forEach(u => ALL.push({ t: u.t, u: u.u, h: u.h, path: node.path }));
    node.children.forEach(flat);
  };
  DATA.tree.forEach(flat);
}

/* ---------- 3.5 常用（实时读浏览历史算出来） ----------
 * 扩展里没有"生成时快照"这一说，所以每次打开都实时问一次浏览历史。
 * 口径必须与静态版 gen_nav.py 的 pick_hot() 完全一致，否则两版给出的
 * 「常用」不是同一批：窗口 = 最近 7 天（凑不满 20 个就放宽到 28 天），
 * 排序 = 窗口内次数 → 累计次数 → 最后访问时间。
 */
const HOT_ID = '__hot__';
const HOT_DAYS = 7, HOT_FALLBACK_DAYS = 28, HOT_LIMIT = 20;
/* 每次 search() 翻页拉多少条。真实 Chrome 对 search() 有条数上限，
   一次拉不全整个窗口，所以必须靠 endTime 翻页（见 loadHot 里的 urlsInWindow）。 */
const SEARCH_PAGE = 5000;
let HOT = [], HOT_WINDOW = HOT_DAYS, HOT_OK = false;
/* 算不出来时**必须留个说法**。以前这里是静默隐藏，结果是扩展少一个权限，
   页面上就凭空少一整块，用户只会觉得"怎么没了"，根本猜不到原因。 */
let HOT_ERR = '';

/* 必须与 gen_nav.py 的 _norm_url() 逐字符一致，改一边要同步另一边 */
function normUrl(u) {
  let s = String(u || '').trim();
  if (s.indexOf('https://') === 0) s = s.slice(8);
  else if (s.indexOf('http://') === 0) s = s.slice(7);
  if (s.indexOf('www.') === 0) s = s.slice(4);
  s = s.split('#')[0];
  if (s.slice(-1) === '/') s = s.slice(0, -1);
  return s.toLowerCase();
}

async function loadHot() {
  HOT = [];
  HOT_WINDOW = HOT_DAYS;
  HOT_OK = false;
  HOT_ERR = '';
  if (typeof chrome === 'undefined' || !chrome.history) {
    HOT_ERR = '扩展还没拿到「浏览记录」权限';
    return;
  }

  const DAY = 24 * 60 * 60 * 1000;
  const now = Date.now();

  /* 拉出窗口内出现过的全部 URL，按归一化键分组（含同站点的各 hash 路由）。
     ⚠️ **不能只调一次 search()**：它有 maxResults 上限（真实 Chrome 内部也有），
     本机 28 天内不同 URL 已近 6000 个，一次拉不完，超出的会被**静默丢掉**。
     后果实测过：pan.quark 明明有 10 次访问却进不了常用 —— 那 10 次散在 7 个
     hash 变体上（`#/list/share/<id>`），变体一丢就只剩 1 次。
     所以用 endTime 不断往前翻页，一直翻到窗口左端为止，变体一个都不漏。 */
  const urlsInWindow = async since => {
    const byKey = new Map();
    let end = now + 60 * 1000;             // 放宽一点，免得漏掉"刚刚"那次访问
    for (let guard = 0; guard < 64; guard++) {
      let chunk;
      try {
        chunk = await chrome.history.search({
          text: '', startTime: since, endTime: end, maxResults: SEARCH_PAGE,
        });
      } catch (e) { return null; }
      if (!chunk || !chunk.length) break;
      let oldest = end;
      for (const r of chunk) {
        if (!r.url) continue;
        const k = normUrl(r.url);
        if (!byKey.has(k)) byKey.set(k, new Set());
        byKey.get(k).add(r.url);
        const t = r.lastVisitTime || 0;
        if (t && t < oldest) oldest = t;
      }
      if (oldest <= since) break;          // 已经翻到窗口左端
      if (oldest >= end) break;            // 没往前挪，防死循环
      /* ⚠️ 用 oldest 而不是 oldest-1：真实 Chrome 的 endTime 是**半开区间**
         （只取 t < endTime），减 1 会漏掉恰好落在 oldest-1 那一毫秒的记录。
         用 oldest 时边界那条会被重复取一次，但 Set 会去重，无害。 */
      end = oldest;
    }
    return byKey;
  };

  /* 算一个窗口内每个书签的热度。
     ⚠️ 候选集**必须包含书签自己的 URL**（哪怕 search 没返回它）——
     getVisits 是按 URL 精确查、没有条数上限，用它兜底。
     另外 search() 返回的 visitCount 是**累计**值、不是窗口内的，
     想按窗口内次数排序，只能逐个 getVisits 再按时间过滤。 */
  const scan = async days => {
    const since = now - days * DAY;
    const byKey = await urlsInWindow(since);
    if (!byKey) return null;

    const scored = [];
    for (const b of ALL) {
      const k = normUrl(b.u);
      let urls = byKey.get(k);
      if (!urls) { urls = new Set(); byKey.set(k, urls); }
      urls.add(b.u);                    // 书签自己的 URL 永远在候选里，不靠 search 兜底
      let cnt = 0, total = 0, last = 0;
      for (const url of urls) {
        let visits;
        try { visits = await chrome.history.getVisits({ url }); }
        catch (e) { continue; }
        for (const v of (visits || [])) {
          total++;
          if (v.visitTime >= since) cnt++;
          if (v.visitTime > last) last = v.visitTime;
        }
      }
      /* 窗口内一次都没打开过 → 不算「常用」。必须判 cnt 而不是 total：
         判 total 会把"只在很久以前访问过"的书签也放进来，跟静态版口径就分叉了。 */
      if (!cnt) continue;
      scored.push({ b, cnt, total, last });
    }
    scored.sort((x, y) => (y.cnt - x.cnt) || (y.total - x.total) || (y.last - x.last));
    return scored;
  };

  const first = await scan(HOT_DAYS);
  if (!first) { HOT_ERR = '读取浏览记录失败'; return; }
  HOT_OK = true;
  let scored = first;
  if (scored.length < HOT_LIMIT) {
    const wide = await scan(HOT_FALLBACK_DAYS);
    if (wide && wide.length > scored.length) {
      scored = wide;
      HOT_WINDOW = HOT_FALLBACK_DAYS;
    }
  }
  HOT = scored.slice(0, HOT_LIMIT).map(x => x.b);
}

/* ---------- 4. 渲染 ----------
 * 默认视图 = 「常用」：打开新标签页先看见最近在用的那批，而不是几百条全量。
 * 初值只能等 loadHot() 跑完再定（那一刻才知道常用有没有内容），见 refresh()。
 */
const state = { id: HOT_ID, q: '' };
let landed = false;        // 首次加载是否已经落过默认视图

/* ---------- 一级分类色相 ---------- */
/* 侧栏分组和主区标题共用一套色相 → 颜色本身就是"这是哪一类"的线索。
   用 i%6 而不是写死，是为了用户以后加分类也能自动循环到色板。
   ⚠️ 必须**惰性查表**：扩展版的 DATA 是异步从 chrome.bookmarks 拿的，
   在顶层写 `DATA.tree.forEach` 会在数据到位前就执行，静默拿到空表 →
   data-ci 全空、颜色全退化成灰色。 */
const CAT_N = 6;
/* 按路径取色号：分组路径的第一段永远是所属的一级分类 */
function ciAttr(path) {
  const top = String(path).split(' / ')[0];
  const i = DATA.tree.findIndex(t => t.name === top);
  return i < 0 ? '' : ` data-ci="${i % CAT_N}"`;
}

function renderTree() {
  const box = $('#tree');
  let html = `<button class="item top ${state.id === null ? 'on' : ''}" data-id="">
      <span class="n">全部书签</span><span class="c">${DATA.total}</span></button>`;
  if (HOT.length || HOT_ERR) {
    html += `<button class="item top star ${state.id === HOT_ID ? 'on' : ''}" data-id="${HOT_ID}">
        <span class="n">常用</span><span class="c">${HOT.length || '—'}</span></button>`;
  }
  for (const t of DATA.tree) {
    const id = t.path.join('/');
    html += `<button class="grp ${state.id === id ? 'on' : ''}"${ciAttr(id)} data-id="${esc(id)}">
        <span class="d"></span><span class="n">${esc(t.name)}</span><span class="c">${t.count}</span></button>`;
    for (const c of t.children) html += row(c.path.join('/'), c.name, c.count, true);
  }
  box.innerHTML = html;
  box.querySelectorAll('[data-id]').forEach(el => {
    el.addEventListener('click', () => { state.id = el.dataset.id || null; render(); });
  });
}

function row(id, name, count, ind) {
  const on = state.id === id ? ' on' : '';
  return `<button class="item${ind ? ' ind' : ''}${on}" data-id="${esc(id)}">
    <span class="n">${esc(name)}</span><span class="c">${count}</span></button>`;
}

const match = (u, q) => {
  if (!q) return true;
  const s = (u.t + ' ' + u.u + ' ' + u.path.join(' ')).toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(w => s.includes(w));
};

function collect() {
  /* 侧栏点分类 = 在同一条滚动线上**就地换内容**，不是跳到另一页：
     滚动位置接着走（render() 里的 restoreScroll），列表本身永远是
     「按当前范围筛出来的全部书签」首尾相接的一条流水线，
     分类只决定这条流水线里放哪些书签、以及中间插哪些标题。
     所以 .main 始终是**唯一**的滚动容器，不存在两套滑动。 */
  const node = state.id ? BY_ID[state.id] : null;
  const prefixLen = (node ? node.depth : -1) + 2;
  const items = ALL.filter(u => {
    if (node) {
      if (u.path.length < node.path.length) return false;
      for (let i = 0; i < node.path.length; i++) {
        if (u.path[i] !== node.path[i]) return false;
      }
    }
    return match(u, state.q);
  });
  const groups = new Map();
  for (const u of items) {
    const key = u.path.slice(0, Math.min(prefixLen, u.path.length)).join(' / ');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(u);
  }
  return { items, groups };
}

function card(u, q) {
  const host = u.h.replace(/^www\./, '');
  const hl = t => {
    if (!q) return esc(t);
    let out = esc(t);
    for (const w of q.split(/\s+/).filter(Boolean)) {
      out = out.replace(new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'),
                        '<mark>$1</mark>');
    }
    return out;
  };
  const ico = faviconUrl(u.u);
  return `<a class="card" href="${esc(u.u)}" rel="noopener" title="${esc(u.t)}\n${esc(u.u)}">
    <div class="ico"><span>${esc(letter(host || u.u))}</span>
      ${ico ? `<img alt="" src="${esc(ico)}">` : ''}
    </div>
    <div class="txt"><div class="tt">${hl(u.t)}</div><div class="hh">${esc(host)}</div></div>
  </a>`;
}

/* MV3 的 CSP 禁掉内联事件处理器，图标成败只能在这里用 addEventListener 绑 */
function bindIcons() {
  document.querySelectorAll('.ico img').forEach(img => {
    const ok = () => img.classList.add('ok');
    img.addEventListener('load', ok);
    img.addEventListener('error', () => img.remove());
    if (img.complete && img.naturalWidth > 0) ok();     // 命中缓存时 load 可能已经过了
  });
}

function render() {
  const keep = $('#main').scrollTop;      // 整页重渲染会重置滚动，先存着
  renderTree();
  const q = state.q.trim();

  /* 常用是虚拟分类，不占 DATA.total / folders 的计数 */
  if (state.id === HOT_ID) {
    $('#title').textContent = '常用';
    if (HOT_ERR) {
      $('#count').textContent = '暂时算不出来';
      $('#body').innerHTML = hotHint(HOT_ERR);
      restoreScroll(keep);
      return;
    }
    const items = HOT.filter(u => match(u, q));
    $('#count').textContent = `${items.length} 项 · 最近 ${HOT_WINDOW} 天打开过`;
    $('#body').innerHTML = items.length
      ? hotSection(items, q)
      : '<div class="empty">没有匹配的常用书签</div>';
    bindIcons();
    restoreScroll(keep);
    return;
  }

  const { items, groups } = collect();

  $('#title').textContent = state.id ? BY_ID[state.id].path.join(' / ') : '全部书签';
  $('#count').textContent = q ? `${items.length} 项匹配` : `${items.length} 项`;

  if (!items.length) {
    $('#body').innerHTML = '<div class="empty">没有匹配的书签</div>';
    restoreScroll(keep);
    return;
  }

  let html = '';
  /* 「全部」首屏把常用顶在最前面；一旦开始搜索或选中了分类，就让位给内容本身。
     算不出来时也要顶一条说明，不能凭空少一块。 */
  if (state.id === null && !q) {
    html += HOT.length ? hotSection(HOT, '') : (HOT_ERR ? hotHint(HOT_ERR) : '');
  }
  for (const [name, list] of groups) {
    const label = state.id ? name.split(' / ').slice(-1)[0] : name.split(' / ')[0];
    // 分组路径的第一段永远是它所属的一级分类 → 取同一个色相，跟侧栏呼应
    html += `<section class="sec"${ciAttr(name)}><h2>${esc(label)}<span class="c">${list.length}</span></h2><div class="grid">`;
    html += list.map(u => card(u, q)).join('');
    html += '</div></section>';
  }
  $('#body').innerHTML = html;
  bindIcons();
  restoreScroll(keep);
}

/* 就地换内容后把滚动位置接回去 —— 这是「接着往上滑」的关键：
   切分类不是跳页，滚动位置不该被重置。内容变短时浏览器会把 scrollTop
   自动夹到上限，所以直接赋值即可，不用额外夹。 */
function restoreScroll(top) {
  const m = $('#main');
  if (top && m) m.scrollTop = top;
}

function hotSection(list, q) {
  return `<section class="sec hot"><h2><span class="star">★</span>常用 · 最近 ${HOT_WINDOW} 天<span class="c">${list.length}</span></h2>
    <div class="grid">${list.map(u => card(u, q)).join('')}</div></section>`;
}

/* 「常用」的失败提示。宁可难看一点，也不要让一整块内容无声消失。 */
function hotHint(reason) {
  const fix = (typeof chrome === 'undefined' || !chrome.history)
    ? '到 <code>edge://extensions</code> 点一次「重新加载」（加了新权限后必须重载才生效）。'
    : '稍后重新打开这个页面会自动重试。';
  return `<div class="hint">${esc(reason)}，「常用」暂时算不出来。<br>${fix}</div>`;
}

/* ---------- 5. 数据加载 & 实时同步 ---------- */

async function refresh() {
  let roots;
  try {
    roots = await chrome.bookmarks.getTree();
  } catch (e) {
    // 常见于没装成扩展、直接用浏览器打开了 newtab.html
    $('#body').innerHTML = '<div class="empty">读不到收藏夹：' + esc(e.message || e)
      + '<br><span style="font-size:11px">这个页面要作为扩展加载才能用，见 README 的安装步骤</span></div>';
    return;
  }
  DATA = buildData(roots);
  indexData();
  // 选中的文件夹被删了就退回全部；HOT_ID 是虚拟分类，不在 BY_ID 里，要排除
  if (state.id && state.id !== HOT_ID && !BY_ID[state.id]) state.id = null;

  await loadHot();
  if (!landed) {
    // 打开就落在「常用」；一条都没算出来时才退回「全部书签」
    landed = true;
    state.id = HOT.length ? HOT_ID : null;
  } else if (!HOT.length && !HOT_ERR && state.id === HOT_ID) {
    // 后续刷新时算空了就退回全部；但**出错了要保留**，让用户点得到那条说明
    state.id = null;
  }

  render();
  $('#stat').innerHTML = `共 ${DATA.total} 条 · ${DATA.folders} 个文件夹<br>`
    + (HOT_OK ? `常用按最近 ${HOT_WINDOW} 天访问统计` : '实时同步自收藏夹');
}

function watchBookmarks() {
  if (typeof chrome === 'undefined' || !chrome.bookmarks) return;   // 非扩展环境下静默跳过
  let timer = null;
  const kick = () => { clearTimeout(timer); timer = setTimeout(refresh, 250); };
  for (const ev of ['onCreated', 'onRemoved', 'onChanged', 'onMoved',
                    'onChildrenReordered', 'onImportEnded']) {
    if (chrome.bookmarks[ev]) chrome.bookmarks[ev].addListener(kick);
  }
}

/* ---------- 6. 交互 ---------- */
/* 主题：苹果官网风格固定浅色，不再提供深色切换。 */

/* 顶部网页搜索：默认 Bing，可换引擎并记住。
   用原生 form 提交（method=get），所以不写导航代码，浏览器自己拼 URL；
   换引擎时改 action 和输入框的 name —— 百度用的是 wd，其余是 q。 */
const ENGINES = [
  { id: 'bing',   name: 'Bing',   action: 'https://www.bing.com/search',   param: 'q'  },
  { id: 'google', name: 'Google', action: 'https://www.google.com/search', param: 'q'  },
  { id: 'baidu',  name: '百度',   action: 'https://www.baidu.com/s',       param: 'wd' },
  { id: 'zhihu',  name: '知乎',   action: 'https://www.zhihu.com/search',  param: 'q'  },
  { id: 'github', name: 'GitHub', action: 'https://github.com/search',     param: 'q'  },
  { id: 'bili',   name: 'B站',    action: 'https://search.bilibili.com/all', param: 'keyword' },
];
const ENGINE_KEY = 'nav-engine';

/* ---------- 搜索历史 ----------
   为什么要自己拦提交：原来是原生 form 提交（method=get），一点「搜索」页面就走掉了，
   **没有任何 JS 能记录这一次**。所以改成先 preventDefault、写进历史、再用
   location.assign 跳同一串地址 —— 展示出来的行为一模一样，但中间多了一次记账。

   存 localStorage（newtab.html 是普通页面上下文，不是 service worker，没有
   chrome.storage 的限制），跟主题/引擎的选择放同一处。 */
const HIST_KEY = 'nav-search-hist';
const HIST_MAX = 60;
let wHist = [];          // [{v, t, k}]  t=时间戳  k=引擎 id（网址为空）
let histSel = -1;        // 键盘当前落在第几项，-1 = 没选
/* 搜索输入框的引用。showHist/histRows 都要读它的 value 做模糊筛选，
   但它们可能在 initSearch 之前（或无 form 时）被调用，所以先占个 null 位。 */
let engineInp = null;

function loadHist() {
  try {
    const raw = localStorage.getItem(HIST_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    wHist = Array.isArray(arr)
      ? arr.filter(x => x && typeof x.v === 'string' && x.v)
           .map(x => ({ v: x.v, t: +x.t || 0, k: x.k || '' }))
      : [];
  } catch (e) { wHist = []; }
}
function saveHist() {
  try { localStorage.setItem(HIST_KEY, JSON.stringify(wHist.slice(0, HIST_MAX))); }
  catch (e) { /* 忽略 */ }
}
/* 同一个词再搜一次 = 提到最前并刷新时间，而不是存两条 */
function pushHist(v, k) {
  const s = String(v || '').trim();
  if (!s) return;
  const key = k || '';
  wHist = wHist.filter(x => !(x.v === s && (x.k || '') === key));
  wHist.unshift({ v: s, t: Date.now(), k: key });
  if (wHist.length > HIST_MAX) wHist.length = HIST_MAX;
  saveHist();
}
function dropHist(v, k) {
  const key = k || '';
  wHist = wHist.filter(x => !(x.v === v && (x.k || '') === key));
  saveHist();
}
function clearHist() { wHist = []; saveHist(); }

/* 怎么跳过去：网址直接开；搜索词交给所选引擎 —— 跟原来原生 form 提交的地址一致 */
function histTarget(it) {
  if (!it.k) {
    const host = /^(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?/i.test(it.v.split('/')[0])
      ? 'http://' + it.v : it.v;
    return /^https?:\/\//i.test(host) ? host : 'https://' + it.v;
  }
  const en = ENGINES.find(e => e.id === it.k) || ENGINES[0];
  return en.action + '?' + en.param + '=' + encodeURIComponent(it.v);
}
const isUrlItem = it => !it.k;
function actOn(it) {
  pushHist(it.v, it.k);                  // 重搜一次也要置顶
  hideHist();
  location.assign(histTarget(it));
}

/* 模糊匹配：命令面板式 —— 按字符顺序出现即可（"gh" 能命中 "github"） */
function fuzzy(needle, hay) {
  const n = needle.toLowerCase(), h = hay.toLowerCase();
  if (!n) return true;
  let i = 0;
  for (const c of h) { if (c === n[i]) i++; if (i === n.length) return true; }
  return false;
}
function engineName(id) {
  return (ENGINES.find(e => e.id === id) || ENGINES[0]).name;
}
/* 当前筛选结果：showHist 和键盘取项必须用同一套顺序，抽出来避免两边算歪 */
function histRows() {
  const q = engineInp ? engineInp.value.trim() : '';
  return wHist.filter(it => fuzzy(q, it.v)).slice(0, 9);
}

/* 下拉"这一帧"正在展示的行。键盘上下键必须对着它走 ——
   不能每按一次都重新按 input.value 算一遍：上下键会把选中项填回输入框，
   一填回去过滤条件就变了，列表会被筛剩一条，第二下就动不了。 */
let histShown = [];
/* 页面启动时会把光标自动放进搜索框（"打开就能直接打字"）。那一下也会触发
   focus → showHist，结果是**新标签页一进来就被历史面板糊住**，落在「常用」上
   却看见一堆搜索记录 —— 用户反馈过这个。所以程序化聚焦时压掉这一次弹窗，
   只有用户自己点击 / 打字 / 按 ↓ 才弹。 */
let histAutoFocus = false;

function showHist() {
  const box = $('#whist'); if (!box) return;
  /* 每次都是重新渲染一整份列表，旧的选中下标作废 —— 不重置的话，
     方向键选过某项后再点输入框，会被筛成更短的列表，histSel 就越界了。 */
  histSel = -1;
  const q = engineInp ? engineInp.value.trim() : '';

  if (!wHist.length) {
    histShown = [];
    box.innerHTML = '<div class="we">还没有搜索记录 —— 搜一次或直接打开一个网址，这里就会记住。</div>';
    box.hidden = false;
    return;
  }
  const rows = histRows();
  if (!rows.length) {
    histShown = [];
    box.innerHTML = '<div class="we">历史里没有匹配「' + esc(q) + '」的记录</div>';
    box.hidden = false;
    return;
  }
  histShown = rows.slice();

  /* 分组：搜索词在上，网址在下（各自内部按时间倒序）。
     序号贯穿两组连续编号，键盘上下键可以跨组走。 */
  let idx = 0;
  const group = (label, list) => {
    if (!list.length) return '';
    let s = '<div class="wl">' + label + '</div>';
    for (const it of list) {
      const k = idx++;
      const isU = isUrlItem(it);
      s += '<button type="button" class="wh" data-i="' + k + '">'
        + '<span class="k">' + (isU ? '↗' : '⌕') + '</span>'
        + '<span class="n">' + esc(it.v) + '</span>'
        + '<span class="m">' + (isU ? '打开' : '搜索 · ' + engineName(it.k)) + '</span>'
        + '<span class="x" data-x="' + k + '" title="从历史里删掉">×</span>'
        + '</button>';
    }
    return s;
  };
  let html = group('搜过', rows.filter(x => !isUrlItem(x)));
  html += group('打开过', rows.filter(isUrlItem));
  html += '<button type="button" class="wx" id="whclr">清空搜索历史</button>';
  box.innerHTML = html;
  box.hidden = false;

  /* 一律用 mousedown（不是 click）：点下拉会先让输入框失焦，
     失焦那一刻历史就收了，click 根本不会触发。preventDefault 保住焦点。 */
  box.querySelectorAll('.wh').forEach(el => {
    el.addEventListener('mousedown', ev => {
      if (ev.target.closest('.x')) return;
      ev.preventDefault();
      const it = rows[+el.dataset.i]; if (it) actOn(it);
    });
  });
  box.querySelectorAll('.x').forEach(el => {
    el.addEventListener('mousedown', ev => {
      ev.preventDefault(); ev.stopPropagation();
      const it = rows[+el.parentElement.dataset.i];
      if (it) { dropHist(it.v, it.k); histSel = -1; showHist(); }
    });
  });
  const clr = $('#whclr');
  if (clr) clr.addEventListener('mousedown', ev => {
    ev.preventDefault(); clearHist(); histSel = -1; showHist();
  });
}
function hideHist() {
  const box = $('#whist');
  if (box) box.hidden = true;
  histSel = -1;
  histShown = [];
}
function moveHist(d) {
  const box = $('#whist'); if (!box) return;
  const items = box.querySelectorAll('.wh');
  if (!items.length) return;
  histSel += d;
  if (histSel < 0) histSel = items.length - 1;
  if (histSel >= items.length) histSel = -1;    // -1 回到"什么都不选"= 输入框内容
  items.forEach((el, i) => el.classList.toggle('sel', i === histSel));
  if (histSel >= 0) items[histSel].scrollIntoView({ block: 'nearest' });
  /* 像浏览器地址栏一样：上下键走过一条就把它的内容**填回输入框**，
     这样回车直接执行所见即所得；回到 -1 时清空回原样。
     用 histShown 这份"当前一帧"的快照，别重算（改了 input.value 会重算歪）。 */
  const it = histSel >= 0 ? histShown[histSel] : null;
  if (engineInp) engineInp.value = it ? it.v : '';
}

/* 中文名不加空格（"用百度搜索"），英文名加（"用 Bing 搜索"）。
   charCodeAt 判非 ASCII，避开正则在字符串模板里的转义麻烦 */
const phText = n => (n.charCodeAt(0) > 127 ? '用' + n + '搜索' : '用 ' + n + ' 搜索');

function initSearch() {
  const form = $('#ws');
  if (!form) return;
  const sel = $('#engine');
  const inp = $('#wq');
  engineInp = inp;                       // 供 showHist / histRows 读取当前输入

  for (const e of ENGINES) {
    const o = document.createElement('option');
    o.value = e.id;
    o.textContent = e.name;
    sel.appendChild(o);
  }

  let cur = ENGINES[0];
  try {
    const saved = localStorage.getItem(ENGINE_KEY);
    if (saved) cur = ENGINES.find(e => e.id === saved) || cur;
  } catch (e) { /* 忽略 */ }
  sel.value = cur.id;

  const apply = e => {
    form.setAttribute('action', e.action);
    inp.setAttribute('name', e.param);
    inp.setAttribute('placeholder', phText(e.name));
  };
  apply(cur);

  sel.addEventListener('change', () => {
    cur = ENGINES.find(e => e.id === sel.value) || ENGINES[0];
    apply(cur);
    try { localStorage.setItem(ENGINE_KEY, cur.id); } catch (e) { /* 忽略 */ }
    inp.focus();
  });

  /* 输入的是网址就直接打开，不走搜索。
     判断：整串没有空白 + 长得像域名 / IP / localhost。
     实现上**不写 window.open** —— 只把 form 的 action 就地换成这个网址、
     同时摘掉输入框的 name，提交仍交给浏览器原生处理。
     好处是判定结果能直接被断言（读 form.action 即可），不用真跳走。 */
  const URL_RE = /^(?:https?:\/\/)?(?:(?:\d{1,3}\.){3}\d{1,3}|localhost|(?:[\w-]+\.)+[a-zA-Z]{2,})(?::\d+)?(?:[/?#]\S*)?$/i;
  const asUrl = v => {
    const s = String(v || '').trim();
    if (!s || /\s/.test(s) || !URL_RE.test(s)) return null;
    if (/^https?:\/\//i.test(s)) return s;
    // 本机地址（IP / localhost）一律走 http —— 本地服务几乎没有 https
    const host = s.split('/')[0];
    const local = /^(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?$/i.test(host);
    return (local ? 'http://' : 'https://') + s;
  };
  const sync = () => {
    const url = asUrl(inp.value);
    if (url) {
      form.setAttribute('action', url);
      inp.removeAttribute('name');          // 摘掉 name，免得被拼上 ?q=
      inp.placeholder = '直接打开 ' + url;
    } else {
      apply(cur);                           // 不是网址就还原成当前引擎的搜索
    }
  };
  inp.addEventListener('input', sync);

  /* ---------- 搜索历史的挂载 ---------- */
  loadHist();

  /* 提交时才记账：拦下原生跳转 → 写历史 → 再跳同一串地址。
     不拦的话页面直接走了，历史根本没机会写。 */
  form.addEventListener('submit', e => {
    const val = inp.value.trim();
    if (!val) return;                      // 空提交交给浏览器（required 挡着）
    const url = asUrl(val);
    e.preventDefault();
    if (url) {
      pushHist(val, '');                   // 空 k = 网址
      location.assign(url);
    } else {
      pushHist(val, cur.id);
      location.assign(cur.action + '?' + cur.param + '=' + encodeURIComponent(val));
    }
  });

  inp.addEventListener('focus', () => {
    if (histAutoFocus) { histAutoFocus = false; return; }   // 启动那一下，不弹
    showHist();
  });
  inp.addEventListener('click', showHist);   // 已聚焦时再点不会触发 focus，得单独接
  inp.addEventListener('input', showHist);
  document.addEventListener('mousedown', e => {
    const box = $('#whist');
    if (box && !box.contains(e.target) && e.target !== inp && e.target !== sel) hideHist();
  });

  /* Esc 撤出搜索框；上下键在历史里走；回车执行选中项 */
  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape') { inp.value = ''; sync(); hideHist(); inp.blur(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const box = $('#whist');
      if ((box && !box.hidden) || e.key === 'ArrowDown') {
        e.preventDefault();
        if (box && box.hidden) showHist();
        else moveHist(e.key === 'ArrowDown' ? 1 : -1);
      }
      return;
    }
    if (e.key === 'Enter' && histSel >= 0) {
      const it = histShown[histSel];
      if (it) { e.preventDefault(); actOn(it); }
    }
  });

  return inp;
}

const isTyping = el => !!el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);

function boot() {
  const wq = initSearch();

  $('#q').addEventListener('input', e => {
    state.q = e.target.value;
    /* 在「常用」里打字就自动跳回「全部书签」。
       搜索天生是全库动作 —— 只在那 20 条里找，搜不到会被当成"书签丢了" */
    if (state.id === HOT_ID && state.q.trim()) state.id = null;
    render();
  });
  document.addEventListener('keydown', e => {
    // 正在输入框里打字时别抢 / 键，否则搜 "a/b" 这类词会被弹走
    if (e.key === '/' && !isTyping(document.activeElement)) {
      e.preventDefault();
      $('#q').focus();
    }
    if (e.key === 'Escape' && document.activeElement === $('#q')) {
      $('#q').value = ''; state.q = ''; $('#q').blur(); render();
    }
  });

  if (wq) { histAutoFocus = true; wq.focus(); }   // 打开就能直接打字搜索（但不弹历史）

  refresh();
  watchBookmarks();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
