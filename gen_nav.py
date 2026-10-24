#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从 Edge 书签文件生成「极简卡片导航」单文件网页。

用法:
    python gen_nav.py                 # 用默认 Edge Default profile
    python gen_nav.py --profile "Profile 1"
    python gen_nav.py --src "D:\\bookmarks.json" --out "D:\\nav.html"
    python gen_nav.py --open          # 生成后直接打开

产物: index.html —— 零依赖、离线可用、书签数据内嵌。
只读 Edge 书签文件，不修改任何东西。
"""
import argparse
import base64
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

EDGE_USER_DATA = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data"
)

# 「常用」区块：最近 N 天打开过的书签才够格，不够 limit 个就放宽到 FALLBACK 天
HOT_DAYS = 7
HOT_FALLBACK_DAYS = 28
HOT_LIMIT = 20
WKE_OFFSET = 11644473600      # WebKit 时间戳(1601起) 与 Unix 时间戳(1970起) 的秒差

ICON_CACHE = os.path.join(HERE, "icons_cache.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MAX_ICON = 40 * 1024          # 单个图标上限，超过就退回字母块
FETCH_TIMEOUT = 6


# ---------------------------------------------------------------- 读取书签

def edge_bookmarks_path(profile: str = "Default") -> str:
    return os.path.join(EDGE_USER_DATA, profile, "Bookmarks")


def load_roots(src: str, with_workspaces: bool = False):
    """读 Edge/Chrome 的 Bookmarks JSON。

    返回「顶级分类节点」列表 —— 注意不是返回 bookmark_bar 容器本身，
    而是把它下面的文件夹提升为顶层，这样侧栏第一级就是用户的
    「事务 / 娱乐 / 学习 / 工具 / ai」这些真实分类。
    """
    with open(src, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    roots = data.get("roots") or {}

    keys = ["bookmark_bar", "other", "synced"]
    if with_workspaces:
        keys.append("workspaces_v2")

    out = []
    for key in keys:
        node = roots.get(key)
        if not isinstance(node, dict):
            continue
        for child in node.get("children") or []:
            if child.get("type") == "folder" and (child.get("children") or []):
                out.append(child)
    if not out:
        raise SystemExit("书签文件中没有找到任何书签。")
    return out


# ---------------------------------------------------------------- 清洗

_WS = re.compile(r"\s+")
_DUP_TITLE = re.compile(r"\s*-{3,}\s*")


def clean_title(name: str, url: str) -> str:
    """清掉 Edge 拼进来的重复标题与多余空白。"""
    title = _DUP_TITLE.split(name or "")[0].strip()
    title = _WS.sub(" ", title)
    if not title:
        host = host_of(url)
        title = host or url
    return title


def host_of(url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/?#]+)", url or "")
    if not m:
        return ""
    host = m.group(1).split("@")[-1].split(":")[0]
    return host.lower()


def pretty_host(host: str) -> str:
    return re.sub(r"^www\.", "", host)


# ---------------------------------------------------------------- 构树

def build_folder(node, path, stats):
    """递归把书签节点转成 {name, path, count, urls, children}。"""
    name = _WS.sub(" ", (node.get("name") or "").strip()) or "(未命名)"
    cur_path = path + [name]
    urls, children = [], []

    for child in node.get("children") or []:
        ctype = child.get("type")
        if ctype == "folder":
            sub = build_folder(child, cur_path, stats)
            if sub["count"]:
                children.append(sub)
        elif ctype == "url":
            u = (child.get("url") or "").strip()
            if not u:
                continue
            urls.append({
                "t": clean_title(child.get("name"), u),
                "u": u,
                "h": host_of(u),
            })
            stats["n"] += 1

    count = len(urls) + sum(c["count"] for c in children)
    return {
        "name": name,
        "path": cur_path,
        "count": count,
        "urls": urls,
        "children": children,
    }


# ---------------------------------------------------------------- 图标抓取

def sniff_mime(data: bytes):
    """只认真正的图片，避免把 404 的 HTML 页面当成图标。"""
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:64].lstrip()
    if head[:4] == b"<svg" or head[:5] == b"<?xml":
        return "image/svg+xml"
    return None


def _try_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        blob = resp.read(MAX_ICON + 1)
    if not blob or len(blob) > MAX_ICON:
        return None
    mime = sniff_mime(blob)
    if not mime:
        return None
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii"))


def fetch_icon(host: str):
    """抓 favicon。公共图标服务成功率最高，放前面；直连站点做补充。"""
    if not host:
        return None
    candidates = [
        "https://api.xinac.net/icon/?url=%s" % host,
        "https://%s/favicon.ico" % host,
        "https://favicon.cccyun.cc/%s" % host,
    ]
    for url in candidates:
        try:
            got = _try_get(url)
            if got:
                return got
        except Exception:
            continue
    return None


def load_cache():
    if os.path.exists(ICON_CACHE):
        try:
            with open(ICON_CACHE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_cache(cache):
    with open(ICON_CACHE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)


def resolve_icons(hosts, refresh=False, jobs=6):
    """并发抓图标，返回 {host: dataURI}。命中的结果会落盘缓存。"""
    cache = {} if refresh else load_cache()
    todo = [h for h in hosts if h and h not in cache]
    if not todo:
        print("  图标: 全部命中缓存 (%d 个)" % len(cache))
        return cache

    print("  图标: 需抓取 %d 个（已缓存 %d 个）..." % (len(todo), len(cache)))
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(fetch_icon, h): h for h in todo}
        for fut in concurrent.futures.as_completed(futures):
            host = futures[fut]
            try:
                got = fut.result()
            except Exception:
                got = None
            cache[host] = got or ""      # 空串表示"抓过但失败"，避免下次重试
            done += 1
            if done % 20 == 0 or done == len(todo):
                print("    %d/%d" % (done, len(todo)), end="\r", flush=True)
    print()
    save_cache(cache)
    hits = sum(1 for h in hosts if cache.get(h))
    print("  图标: 成功 %d / %d" % (hits, len(hosts)))
    return cache


# ---------------------------------------------------------------- 常用（读浏览历史）

def _norm_url(url: str) -> str:
    """把 URL 归一化成可比较的键。

    必须与扩展版 newtab.js 里的 normUrl() **逐字符一致**，
    否则两版算出来的「常用」会不是同一批。改一边要同步另一边。
    """
    u = (url or "").strip()
    for pre in ("https://", "http://"):
        if u.startswith(pre):
            u = u[len(pre):]
            break
    if u.startswith("www."):
        u = u[4:]
    u = u.split("#", 1)[0]
    if u.endswith("/"):
        u = u[:-1]
    return u.lower()


def _webkit(dt: datetime.datetime) -> int:
    """转 Chromium 用的 WebKit 时间戳：1601 起算的微秒。"""
    return int((dt.timestamp() + WKE_OFFSET) * 1_000_000)


def read_history_windows(profile: str = "Default",
                         days: int = HOT_DAYS, fallback: int = HOT_FALLBACK_DAYS,
                         path: str = ""):
    """读 Edge 浏览历史，一次返回两个时间窗口的统计。

    返回 ({归一化URL: (窗口内次数, 最后访问, 累计次数)}, 同结构的放宽窗口)，
    分别对应「最近 days 天」和「最近 fallback 天」。

    三个必须知道的坑：
      1. Edge 运行时 History 文件被独占锁住，直接连会报 database is locked
         —— 必须先复制一份副本出来再读；
      2. **visits.url 存的是 urls.id 这个整数外键，不是 URL 字符串**
         （新版 Chromium schema 改的）。不 JOIN urls 表就一条都对不上，
         而且不报错，只会静默返回 0 结果。
      3. **累计次数要用 visits 的行数，不能用 urls.visit_count。**
         visit_count 是 Chrome 自己维护的计数器，跟实际存了多少条访问记录差很远
         （实测 deepseek 是 24 vs 114 行、bilibili 是 87 vs 103 行），而且它可能
         比"窗口内次数"还小 —— 拿它排序会导出「一周看了 100 次、累计才 50 次」
         这种自相矛盾的结果。扩展版 newtab.js 数的是 visits 行数，
         两边口径必须一致（test_extension.py 的交叉比对就咬这一条）。
    """
    empty = ({}, {})
    # path 给了就直连指定文件（造演示数据 / 用 Chrome 的 History 时用得上）
    src = path or os.path.join(EDGE_USER_DATA, profile, "History")
    if not os.path.exists(src):
        return empty

    tmp = os.path.join(tempfile.gettempdir(), "_edge_nav_history.db")
    try:
        shutil.copy2(src, tmp)
    except Exception:
        return empty
    try:
        con = sqlite3.connect("file:%s?mode=ro" % tmp.replace("\\", "/"), uri=True)
    except Exception:
        return empty

    now = datetime.datetime.now()

    def scan(span_days):
        """窗口内次数 + 最后访问时间。累计次数不在这里算（与窗口无关，只查一次）。"""
        out = {}
        cur = con.cursor()
        cur.execute(
            """SELECT u.url, COUNT(*), MAX(v.visit_time)
                 FROM visits v JOIN urls u ON u.id = v.url
                WHERE v.visit_time >= ?
             GROUP BY v.url""",
            (_webkit(now - datetime.timedelta(days=span_days)),),
        )
        for url, cnt, last in cur.fetchall():
            key = _norm_url(str(url))
            if not key:
                continue
            prev = out.get(key)
            if prev:
                # http/https 或末尾斜杠差异会让两个不同 URL 归一到同一个键，要合并
                out[key] = (prev[0] + cnt, max(prev[1], last or 0))
            else:
                out[key] = (cnt, last or 0)
        return out

    try:
        # 累计次数：全量 visits 行数。建成一张独立表，
        # 免得每个窗口都重复扫一遍（28 天窗口动辄上万行）
        totals = {}
        cur = con.cursor()
        cur.execute("""SELECT u.url, COUNT(*)
                         FROM visits v JOIN urls u ON u.id = v.url
                     GROUP BY v.url""")
        for url, n in cur.fetchall():
            key = _norm_url(str(url))
            if key:
                totals[key] = totals.get(key, 0) + (n or 0)

        out = []
        # 每个窗口都补上同一份累计次数，凑成 (窗口内, 最后访问, 累计)
        for win in (scan(days), scan(fallback)):
            out.append({k: (v[0], v[1], totals.get(k, 0)) for k, v in win.items()})
        return out[0], out[1]
    except Exception:
        return empty
    finally:
        con.close()


def pick_hot(bookmarks, hist_7, hist_28, limit=HOT_LIMIT):
    """挑出「最近一周打开过」的书签，按热度排序取前 limit 个。

    排序键：窗口内次数 → 累计次数 → 最后访问时间（都从大到小）。
    累计次数做第二关键字，是为了让「老牌高频、这周只点了一两次」的站点
    不至于被「这周刚冒头、点了同样次数」的新站挤掉。

    最近 7 天凑不满 limit 个时放宽到 28 天，并把实际用的窗口一起返回，
    好在页面上如实标注（否则"最近 7 天"会变成一句谎话）。
    """
    def key_of(b):
        return _norm_url(b["u"])

    picks = [(b, hist_7[key_of(b)]) for b in bookmarks if key_of(b) in hist_7]
    window = HOT_DAYS
    if len(picks) < limit:
        picks = [(b, hist_28[key_of(b)]) for b in bookmarks if key_of(b) in hist_28]
        window = HOT_FALLBACK_DAYS

    picks.sort(key=lambda x: (-x[1][0], -x[1][2], -x[1][1]))
    return picks[:limit], window


def flatten_bookmarks(tree):
    """平铺整棵树，返回 [(书签dict, 所属路径)]。"""
    out = []

    def walk(node):
        for u in node["urls"]:
            out.append((u, node["path"]))
        for c in node["children"]:
            walk(c)

    for t in tree:
        walk(t)
    return out


# ---------------------------------------------------------------- 页面模板

STATIC_SCRIPT = r"""
const DATA = /*__DATA__*/null;
/*__DATA__*/

const $  = (s,r=document)=>r.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const letter = h => (h.replace(/^www\./,'')[0] || '#').toUpperCase();

/* ---------- 打平所有节点 ---------- */
const NODES = [];                       // 全部节点，含 全部 与各层级
function walk(node, depth, parentId){
  NODES.push({id:node.path.join('/'), name:node.name, path:node.path,
              depth, count:node.count, urls:node.urls,
              children:node.children.map(c=>c.path.join('/'))});
  node.children.forEach(c => walk(c, depth+1, node.path.join('/')));
}
DATA.tree.forEach(t => walk(t, 0, null));
const BY_ID = Object.fromEntries(NODES.map(n => [n.id, n]));

/* ---------- 每个书签的所属路径 ---------- */
const ALL = [];
function flatten(node){
  const p = node.path;
  node.urls.forEach(u => ALL.push({...u, path:p}));
  node.children.forEach(flatten);
}
DATA.tree.forEach(flatten);

/* ---------- 常用 ----------
 * 生成时读了一次浏览历史，把「最近一周打开过」的书签 URL 存进 DATA.hot。
 * 这里只存了 URL，图标标题都从 ALL 里回查，避免 base64 被重复塞一份。
 * normUrl 必须与 gen_nav.py 的 _norm_url() 逐字符一致。
 */
function normUrl(u){
  let s = String(u||'').trim();
  if (s.indexOf('https://') === 0) s = s.slice(8);
  else if (s.indexOf('http://') === 0) s = s.slice(7);
  if (s.indexOf('www.') === 0) s = s.slice(4);
  s = s.split('#')[0];
  if (s.slice(-1) === '/') s = s.slice(0, -1);
  return s.toLowerCase();
}
const HOT_ID = '__hot__';
const HOT = (() => {
  const byKey = new Map();
  ALL.forEach(u => { const k = normUrl(u.u); if (!byKey.has(k)) byKey.set(k, u); });
  return (DATA.hot || []).map(u => byKey.get(normUrl(u))).filter(Boolean);
})();
const HOT_WINDOW = DATA.hotWindow || 7;
/* 这次没算出来时的原因说明。**不能静默少一块** —— 用户只会觉得"怎么没了"。 */
const HOT_REASON = DATA.hotReason || '';

/* ---------- 状态 ----------
 * 打开就落在「常用」上：导航页存在的意义就是"我常去的那几个地方"。
 * 一条都没算出来时才退回「全部书签」（这时候首屏会有说明条，不至于空着）。
 */
const state = { id: HOT.length ? HOT_ID : null, q:'' };

/* ---------- 一级分类色相 ---------- */
/* 侧栏分组和主区标题共用一套色相 → 颜色本身就是"这是哪一类"的线索。
   用 i%6 而不是写死，是为了用户以后加分类也能自动循环到色板。
   ⚠️ 惰性查表，不在顶层缓存：扩展版的 DATA 是异步来的，顶层算会静默拿到空表。 */
const CAT_N = 6;
/* 按路径取色号：分组路径的第一段永远是所属的一级分类 */
function ciAttr(path){
  const top = String(path).split(' / ')[0];
  const i = DATA.tree.findIndex(t => t.name === top);
  return i < 0 ? '' : ` data-ci="${i % CAT_N}"`;
}

/* ---------- 侧栏 ---------- */
function renderTree(){
  const box = $('#tree');
  let html = `<button class="item top ${state.id===null?'on':''}" data-id="">
      <span class="n">全部书签</span><span class="c">${DATA.total}</span></button>`;
  if (HOT.length || HOT_REASON){
    html += `<button class="item top star ${state.id===HOT_ID?'on':''}" data-id="${HOT_ID}">
        <span class="n">常用</span><span class="c">${HOT.length || '—'}</span></button>`;
  }
  DATA.tree.forEach(t => {
    const id = t.path.join('/');
    html += `<button class="grp ${state.id===id?'on':''}"${ciAttr(id)} data-id="${esc(id)}">
        <span class="d"></span><span class="n">${esc(t.name)}</span><span class="c">${t.count}</span></button>`;
    t.children.forEach(c => {
      html += row(c.path.join('/'), c.name, c.count, true);
    });
  });
  box.innerHTML = html;
  box.querySelectorAll('[data-id]').forEach(el => {
    el.onclick = () => { state.id = el.dataset.id || null; render(); };
  });
}
function row(id, name, count, ind){
  const on = state.id === id ? ' on' : '';
  return `<button class="item${ind?' ind':''}${on}" data-id="${esc(id)}">
    <span class="n">${esc(name)}</span><span class="c">${count}</span></button>`;
}

/* ---------- 主区 ---------- */
function match(u, q){
  if (!q) return true;
  const s = (u.t + ' ' + u.u + ' ' + u.path.join(' ')).toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(w => s.includes(w));
}

function collect(){
  /* 侧栏点分类 = 在同一条滚动线上**就地换内容**，不是跳到另一页：
     滚动位置接着走（render() 里的 restoreScroll），列表本身永远是
     「按当前范围筛出来的全部书签」首尾相接的一条流水线，
     分类只决定这条流水线里放哪些书签、以及中间插哪些标题。
     所以 .main 始终是**唯一**的滚动容器，不存在两套滑动。 */
  const node = state.id ? BY_ID[state.id] : null;
  const depth = node ? node.depth : -1;          // -1 表示「全部」，按一级分类分组
  const prefixLen = depth + 2;
  const items = ALL.filter(u => {
    // 归属判断：书签路径以选中节点路径开头
    if (node){
      if (u.path.length < node.path.length) return false;
      for (let i=0;i<node.path.length;i++) if (u.path[i] !== node.path[i]) return false;
    }
    return match(u, state.q);
  });
  // 按「选中节点下再深一层」的路径前缀分组
  const groups = new Map();
  items.forEach(u => {
    const key = u.path.slice(0, Math.min(prefixLen, u.path.length)).join(' / ');
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(u);
  });
  return {items, groups};
}

function render(){
  const keep = $('#main').scrollTop;      // 整页重渲染会重置滚动，先存着
  renderTree();
  const q = state.q.trim();

  /* 常用是个虚拟分类，不占 DATA.total / folders 的计数 */
  if (state.id === HOT_ID){
    $('#title').textContent = '常用';
    if (HOT_REASON){
      $('#count').textContent = '暂时算不出来';
      $('#body').innerHTML = hotHint();
      restoreScroll(keep);
      return;
    }
    const items = HOT.filter(u => match(u, q));
    $('#count').textContent = `${items.length} 项 · 最近 ${HOT_WINDOW} 天打开过`;
    $('#body').innerHTML = items.length
      ? hotSection(items, q)
      : `<div class="empty">没有匹配的常用书签</div>`;
    restoreScroll(keep);
    return;
  }

  const {items, groups} = collect();

  const title = state.id ? BY_ID[state.id].path.join(' / ') : '全部书签';
  $('#title').textContent = title;
  $('#count').textContent = q ? `${items.length} 项匹配` : `${items.length} 项`;

  if (!items.length){
    $('#body').innerHTML = `<div class="empty">没有匹配的书签</div>`;
    restoreScroll(keep);
    return;
  }

  let html = '';
  /* 「全部」首屏把常用顶在最前面；一旦开始搜索或选中了分类，就让位给内容本身。
     算不出来时也要顶一条说明，不能凭空少一块。 */
  if (state.id === null && !q){
    html += HOT.length ? hotSection(HOT, '') : (HOT_REASON ? hotHint() : '');
  }
  for (const [name, list] of groups){
    // 「全部」视图下，一级分组标题只取末段，避免过长
    const label = state.id ? name.split(' / ').slice(-1)[0] : name.split(' / ')[0];
    // 分组路径的第一段永远是它所属的一级分类 → 取同一个色相，跟侧栏呼应
    html += `<section class="sec"${ciAttr(name)}><h2>${esc(label)}<span class="c">${list.length}</span></h2><div class="grid">`;
    html += list.map(u => card(u, q)).join('');
    html += `</div></section>`;
  }
  $('#body').innerHTML = html;
  restoreScroll(keep);
}

/* 就地换内容后把滚动位置接回去 —— 这是「接着往上滑」的关键：
   切分类不是跳页，滚动位置不该被重置。内容变短时浏览器会把 scrollTop
   自动夹到上限，所以直接赋值即可，不用额外夹。 */
function restoreScroll(top){
  const m = $('#main');
  if (top && m) m.scrollTop = top;
}

function hotSection(list, q){
  return `<section class="sec hot"><h2><span class="star">★</span>常用 · 最近 ${HOT_WINDOW} 天<span class="c">${list.length}</span></h2>
    <div class="grid">${list.map(u => card(u, q)).join('')}</div></section>`;
}

/* 「常用」的失败提示。宁可难看一点，也不要让一整块内容无声消失。 */
function hotHint(){
  return `<div class="hint">${esc(HOT_REASON)}，「常用」暂时算不出来。<br>
    双击「刷新导航」可以重算。</div>`;
}

function card(u, q){
  const host = u.h.replace(/^www\./,'');
  const hl = t => {
    if (!q) return esc(t);
    let out = esc(t);
    q.split(/\s+/).filter(Boolean).forEach(w => {
      out = out.replace(new RegExp('(' + w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + ')','gi'), '<mark>$1</mark>');
    });
    return out;
  };
  return `<a class="card" href="${esc(u.u)}" target="_blank" rel="noopener" title="${esc(u.t)}&#10;${esc(u.u)}">
    <div class="ico"><span>${esc(letter(host||u.u))}</span>
      ${u.i?`<img alt="" src="${esc(u.i)}" onload="this.classList.add('ok')" onerror="this.remove()">`:''}
    </div>
    <div class="txt"><div class="tt">${hl(u.t)}</div><div class="hh">${esc(host)}</div></div>
  </a>`;
}

/* ---------- 交互 ---------- */
$('#stat').innerHTML = `共 ${DATA.total} 条 · ${DATA.folders} 个文件夹<br>生成于 ${esc(DATA.generated)}`;

/* 主题：苹果官网风格固定浅色，不再提供深色切换。 */

/* 顶部网页搜索：默认 Bing，可换引擎并记住。
   走原生 form 提交（method=get），不自己拼跳转；换引擎只改 action 与输入框的 name
   （百度是 wd、B 站是 keyword，其余是 q）。 */
const ENGINES = [
  { id:'bing',   name:'Bing',   action:'https://www.bing.com/search',      param:'q'       },
  { id:'google', name:'Google', action:'https://www.google.com/search',    param:'q'       },
  { id:'baidu',  name:'百度',   action:'https://www.baidu.com/s',          param:'wd'      },
  { id:'zhihu',  name:'知乎',   action:'https://www.zhihu.com/search',     param:'q'       },
  { id:'github', name:'GitHub', action:'https://github.com/search',        param:'q'       },
  { id:'bili',   name:'B站',    action:'https://search.bilibili.com/all',  param:'keyword' },
];
const ENGINE_KEY = 'nav-engine';
let engineInp = null;

/* ---------- 搜索历史 ----------
   为什么要自己拦提交：原来是原生 form 提交（method=get），一点「搜索」页面就走掉了，
   **没有任何 JS 能记录这一次**。所以改成先 preventDefault、写进历史、再用
   location.assign 跳同一串地址 —— 展示出来的行为一模一样，但中间多了一次记账。

   存 localStorage（两版都可用；扩展页是普通页面上下文，不是 service worker）。
   分两类：s=搜索词（按引擎记），u=直接打开的网址。 */
const HIST_KEY  = 'nav-search-hist';
const HIST_MAX  = 60;
let wHist = [];          // [{v, t, k}]  t=时间戳  k=引擎 id（网址为空）
let histSel = -1;        // 键盘当前落在第几项，-1 = 没选
/* 下拉"这一帧"正在展示的行。上下键必须对着它走 —— 不能每按一次都重新按
   input.value 算：上下键会把选中项填回输入框，一填回去过滤条件就变了。 */
let histShown = [];
/* 页面启动时会把光标自动放进搜索框（"打开就能直接打字"）。那一下也会触发
   focus → showHist，结果是**打开页面一进来就被历史面板糊住**，落在「常用」上
   却看见一堆搜索记录。所以程序化聚焦时压掉这一次弹窗，
   只有用户自己点击 / 打字 / 按 ↓ 才弹。 */
let histAutoFocus = false;

function loadHist(){
  try {
    const raw = localStorage.getItem(HIST_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    wHist = Array.isArray(arr)
      ? arr.filter(x => x && typeof x.v === 'string' && x.v)
             .map(x => ({ v:x.v, t:+x.t || 0, k:x.k || '' }))
      : [];
  } catch(e){ wHist = []; }
}
function saveHist(){
  try { localStorage.setItem(HIST_KEY, JSON.stringify(wHist.slice(0, HIST_MAX))); }
  catch(e){}
}
/* 同一个词再搜一次 = 提到最前并刷新时间，而不是存两条 */
function pushHist(v, k){
  const s = String(v || '').trim();
  if (!s) return;
  const key = k || '';
  wHist = wHist.filter(x => !(x.v === s && (x.k || '') === key));
  wHist.unshift({ v:s, t:Date.now(), k:key });
  if (wHist.length > HIST_MAX) wHist.length = HIST_MAX;
  saveHist();
}
function dropHist(v, k){
  const key = k || '';
  wHist = wHist.filter(x => !(x.v === v && (x.k || '') === key));
  saveHist();
}
function clearHist(){ wHist = []; saveHist(); }

/* 怎么跳过去：网址直接开；搜索词交给所选引擎 —— 跟原来原生 form 提交的地址一致 */
function histTarget(it){
  if (!it.k){
    const host = /^(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?/i.test(it.v.split('/')[0])
      ? 'http://' + it.v : it.v;
    return /^https?:\/\//i.test(host) ? host : 'https://' + it.v;
  }
  const en = ENGINES.find(e => e.id === it.k) || ENGINES[0];
  return en.action + '?' + en.param + '=' + encodeURIComponent(it.v);
}
const isUrlItem = it => !it.k;
function actOn(it){
  pushHist(it.v, it.k);                  // 重搜一次也要置顶
  hideHist();
  location.assign(histTarget(it));
}

/* 模糊匹配：命令面板式 —— 按字符顺序出现即可（"gh" 能命中 "github"） */
function fuzzy(needle, hay){
  const n = needle.toLowerCase(), h = hay.toLowerCase();
  if (!n) return true;
  let i = 0;
  for (const c of h){ if (c === n[i]) i++; if (i === n.length) return true; }
  return false;
}

function showHist(){
  const box = $('#whist'); if (!box) return;
  /* 每次都是重新渲染一整份列表，旧的选中下标作废 —— 不重置的话，
     方向键选过某项后再点输入框，会被筛成更短的列表，histSel 就越界了。 */
  histSel = -1;
  const q = engineInp ? engineInp.value.trim() : '';
  const rows = wHist.filter(it => fuzzy(q, it.v)).slice(0, 9);
  histShown = rows.slice();                 // 键盘上下键对着这份快照走（见 moveHist）

  if (!wHist.length){
    histShown = [];
    box.innerHTML = '<div class="we">还没有搜索记录 —— 搜一次或直接打开一个网址，这里就会记住。</div>';
    box.hidden = false;
    return;
  }
  if (!rows.length){
    histShown = [];
    box.innerHTML = '<div class="we">历史里没有匹配「' + esc(q) + '」的记录</div>';
    box.hidden = false;
    return;
  }

  /* 分组：搜索词在上，网址在下（各自内部按时间倒序）。
     序号贯穿两组连续编号，键盘上下键可以跨组走。 */
  let idx = 0, html = '';
  const group = (label, list) => {
    if (!list.length) return '';
    let s = '<div class="wl">' + label + '</div>';
    for (const it of list){
      const k = idx++;                       // 闭包捕获当前值
      const isU = isUrlItem(it);
      s += '<button type="button" class="wh" data-i="' + k + '">'
        + '<span class="k">' + (isU ? '↗' : '⌕') + '</span>'
        + '<span class="n">' + esc(it.v) + '</span>'
        + '<span class="m">' + (isU ? '打开' : '搜索 · ' + engineName(it.k)) + '</span>'
        + '<span class="x" data-x="' + k + '" role="button" title="从历史里删掉">×</span>'
        + '</button>';
    }
    return s;
  };
  html += group('搜过', rows.filter(x => !isUrlItem(x)));
  html += group('打开过', rows.filter(isUrlItem));
  html += '<button type="button" class="wx" id="whclr">清空搜索历史</button>';
  box.innerHTML = html;
  box.hidden = false;

  /* 序号映射回条目：pushHist 之后 wHist 会重排，所以每次点击都用当时的数组取 */
  const at = {};
  { let i = 0;
    for (const it of rows){ at[i] = it; i++; }
  }
  box.querySelectorAll('.wh').forEach(el => {
    el.addEventListener('mousedown', ev => {   // mousedown：赶在 input 失焦之前
      if (ev.target.closest('.x')) return;
      ev.preventDefault();
      const it2 = at[+el.dataset.i]; if (it2) actOn(it2);
    });
  });
  box.querySelectorAll('.x').forEach(el => {
    el.addEventListener('mousedown', ev => {
      ev.preventDefault(); ev.stopPropagation();
      const it2 = at[+el.parentElement.dataset.i];
      if (it2){ dropHist(it2.v, it2.k); histSel = -1; showHist(); }
    });
  });
  const clr = $('#whclr');
  if (clr) clr.addEventListener('mousedown', ev => {
    ev.preventDefault(); clearHist(); histSel = -1; showHist();
  });
}
function hideHist(){
  const box = $('#whist');
  if (box) box.hidden = true;
  histSel = -1;
  histShown = [];
}
function engineName(id){
  return (ENGINES.find(e => e.id === id) || ENGINES[0]).name;
}
function moveHist(d){
  const box = $('#whist'); if (!box) return;
  const items = box.querySelectorAll('.wh');
  if (!items.length) return;
  histSel += d;
  if (histSel < 0) histSel = items.length - 1;
  if (histSel >= items.length) histSel = -1;      // -1 回到"什么都不选"= 输入框内容
  items.forEach((el, i) => el.classList.toggle('sel', i === histSel));
  if (histSel >= 0) items[histSel].scrollIntoView({ block:'nearest' });
  /* 像浏览器地址栏一样：上下键走过一条就把它的内容**填回输入框**，
     回车直接执行所见即所得；回到 -1 清空回原样。 */
  const it = histSel >= 0 ? histShown[histSel] : null;
  if (engineInp) engineInp.value = it ? it.v : '';
}
function curHistItem(){
  const box = $('#whist');
  if (histSel < 0 || !box) return null;
  return histShown[histSel] || null;
}

/* 中文名不加空格（"用百度搜索"），英文名加（"用 Bing 搜索"）。
   charCodeAt 判非 ASCII，避开正则在字符串模板里的转义麻烦 */
const phText = n => (n.charCodeAt(0) > 127 ? '用' + n + '搜索' : '用 ' + n + ' 搜索');

(function initSearch(){
  const form = $('#ws'); if (!form) return;
  const sel = $('#engine'); const inp = $('#wq');
  ENGINES.forEach(e => {
    const o = document.createElement('option');
    o.value = e.id; o.textContent = e.name;
    sel.appendChild(o);
  });
  let cur = ENGINES[0];
  try {
    const saved = localStorage.getItem(ENGINE_KEY);
    if (saved) cur = ENGINES.find(e => e.id === saved) || cur;
  } catch(e){}
  sel.value = cur.id;

  const apply = e => {
    form.setAttribute('action', e.action);
    inp.setAttribute('name', e.param);
    inp.setAttribute('placeholder', phText(e.name));
  };
  apply(cur);

  sel.onchange = () => {
    cur = ENGINES.find(e => e.id === sel.value) || ENGINES[0];
    apply(cur);
    try { localStorage.setItem(ENGINE_KEY, cur.id); } catch(e){}
    inp.focus();
  };

  /* 输入的是网址就直接打开，不走搜索。
     判断：整串没有空白 + 长得像域名 / IP / localhost。
     实现上**不写 window.open** —— 只把 form 的 action 就地换成这个网址、
     同时摘掉输入框的 name，提交仍交给浏览器原生处理。
     好处是判定结果能直接被断言（读 form.action 即可），不用真跳走。 */
  const URL_RE = /^(?:https?:\/\/)?(?:(?:\d{1,3}\.){3}\d{1,3}|localhost|(?:[\w-]+\.)+[a-zA-Z]{2,})(?::\d+)?(?:[/?#]\S*)?$/i;
  const asUrl = v => {
    const s = String(v||'').trim();
    if (!s || /\s/.test(s) || !URL_RE.test(s)) return null;
    if (/^https?:\/\//i.test(s)) return s;
    // 本机地址（IP / localhost）一律走 http —— 本地服务几乎没有 https
    const host = s.split('/')[0];
    const local = /^(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?$/i.test(host);
    return (local ? 'http://' : 'https://') + s;
  };
  const sync = () => {
    const url = asUrl(inp.value);
    if (url){
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
    if (!val) return;                      // 空提交交给浏览器（有 required 挡着）
    const url = asUrl(val);
    if (url){
      e.preventDefault();
      pushHist(val, '');                   // 空 k = 网址
      location.assign(url);
    } else {
      e.preventDefault();
      pushHist(val, cur.id);
      location.assign(cur.action + '?' + cur.param + '=' + encodeURIComponent(val));
    }
  });

  inp.addEventListener('focus', () => {
    if (histAutoFocus){ histAutoFocus = false; return; }   // 启动那一下，不弹
    showHist();
  });
  inp.addEventListener('click', showHist);   // 已聚焦时再点不会触发 focus，得单独接
  inp.addEventListener('input', showHist);
  /* 收起用 mousedown 判断：点击发生在页面别处才收起。
     下拉里的项用 mousedown + preventDefault 处理，所以点它会先于这里生效。 */
  document.addEventListener('mousedown', e => {
    if (!$('#whist').contains(e.target) && e.target !== inp && e.target !== sel) hideHist();
  });

  inp.addEventListener('keydown', e => {
    if (e.key === 'Escape'){ inp.value = ''; sync(); hideHist(); inp.blur(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp'){
      const box = $('#whist');
      if (box && !box.hidden || e.key === 'ArrowDown'){
        e.preventDefault();
        if (box && box.hidden) showHist();
        else moveHist(e.key === 'ArrowDown' ? 1 : -1);
      }
      return;
    }
    if (e.key === 'Enter' && histSel >= 0){
      const it = curHistItem();
      if (it){ e.preventDefault(); actOn(it); }
    }
  });
  engineInp = inp;
})();

const isTyping = el => !!el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);

$('#q').oninput = e => {
  state.q = e.target.value;
  /* 在「常用」里打字就自动跳回「全部书签」。
     搜索天生是全库动作 —— 只在那 20 条里找，搜不到会被当成"书签丢了" */
  if (state.id === HOT_ID && state.q.trim()) state.id = null;
  render();
};
document.addEventListener('keydown', e => {
  /* 正在输入框里打字时不抢 / 键，否则搜 "a/b" 这种词会被弹走 */
  if (e.key === '/' && !isTyping(document.activeElement)){ e.preventDefault(); $('#q').focus(); }
  if (e.key === 'Escape' && document.activeElement === $('#q')){ $('#q').value=''; state.q=''; $('#q').blur(); render(); }
});
render();
if (engineInp){ histAutoFocus = true; engineInp.focus(); }   // 打开就能直接打字搜索（但不弹历史）
"""

def page_template():
    """两种版本共用界面与样式，离线导出时把资源内嵌成单文件。"""
    ext = os.path.join(HERE, "extension")
    def read(name):
        with open(os.path.join(ext, name), encoding="utf-8") as fh:
            return fh.read()
    shell = read("newtab.html")
    shell = shell.replace('<link rel="stylesheet" href="newtab.css">',
                          '<style>\n' + read("newtab.css") + '\n</style>')
    shell = shell.replace('<script src="ui.js" defer></script>',
                          '<script>\n' + read("ui.js") + '\n</script>')
    shell = shell.replace('<script src="newtab.js"></script>', '')
    return shell.replace('</body>', '<script>' + STATIC_SCRIPT + '</script>\n</body>')


TEMPLATE = page_template()


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="从 Edge 书签生成极简卡片导航页")
    ap.add_argument("--src", help="书签 JSON 路径（默认自动定位 Edge）")
    ap.add_argument("--profile", default="Default", help='Edge profile 目录名，如 "Profile 1"')
    ap.add_argument("--history", help="浏览历史 sqlite 路径（默认自动定位 Edge；配合 --src 可完全脱离 Edge 生成）")
    ap.add_argument("--with-workspaces", action="store_true",
                    help="一并收录 Edge「工作区」里的书签")
    ap.add_argument("--no-icons", action="store_true", help="不抓图标，只用字母色块")
    ap.add_argument("--no-history", action="store_true",
                    help="不读浏览历史，「常用」区块留空")
    ap.add_argument("--refresh-icons", action="store_true", help="忽略缓存，重新抓取全部图标")
    ap.add_argument("--jobs", type=int, default=6, help="图标抓取并发数（默认 6，过高会被图标服务限流）")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"), help="输出 HTML 路径")
    ap.add_argument("--open", action="store_true", help="生成后打开")
    args = ap.parse_args()

    src = args.src or edge_bookmarks_path(args.profile)
    if not os.path.exists(src):
        sys.exit(f"找不到书签文件: {src}\n请用 --src 指定，或检查 --profile 名称。")

    roots = load_roots(src, args.with_workspaces)
    stats = {"n": 0}
    tree = []
    for r in roots:
        node = build_folder(r, [], stats)
        if node["count"]:
            tree.append(node)

    folders = 0
    def cnt(n):
        nonlocal folders
        folders += 1                      # 自己也算一个，和扩展版口径保持一致
        for c in n["children"]:
            cnt(c)
    for t in tree:
        cnt(t)

    # 抓取图标并内嵌成 data URI，让页面完全离线自足
    if not args.no_icons:
        hosts, seen = [], set()

        def collect(n):
            for u in n["urls"]:
                h = u["h"]
                if h and h not in seen:
                    seen.add(h)
                    hosts.append(h)
            for c in n["children"]:
                collect(c)

        for t in tree:
            collect(t)

        icons = resolve_icons(hosts, refresh=args.refresh_icons, jobs=args.jobs)

        def attach(n):
            for u in n["urls"]:
                u["i"] = icons.get(u["h"]) or ""
            for c in n["children"]:
                attach(c)

        for t in tree:
            attach(t)

    # 「常用」：读浏览历史，挑出最近一周打开过的书签。
    # 只往页面里写 URL 列表（不复制书签对象），免得图标 base64 被重复存一份；
    # 页面拿 URL 去 ALL 里回查即可。
    hot_urls, hot_window, hot_reason = [], HOT_DAYS, ""
    if args.no_history:
        hot_reason = "这次生成时跳过了浏览历史（--no-history）"
    else:
        hist_now, hist_wide = read_history_windows(args.profile, path=args.history or "")
        if hist_now or hist_wide:
            flat = flatten_bookmarks(tree)
            picked, hot_window = pick_hot([u for u, _ in flat], hist_now, hist_wide)
            hot_urls = [u["u"] for u, _ in picked]
            if not hot_urls:
                hot_reason = "浏览历史里找不到这些书签的访问记录（可能刚清过历史，或都超过了 28 天）"
        else:
            hot_reason = "没能读到 Edge 的浏览历史"

    data = {
        "total": stats["n"],
        "folders": folders,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "hot": hot_urls,
        "hotWindow": hot_window,
        "hotReason": hot_reason,
        "tree": [{"name": t["name"], "path": t["path"], "count": t["count"],
                  "urls": t["urls"], "children": t["children"]} for t in tree],
    }

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("/*__DATA__*/null;\n/*__DATA__*/", payload + ";")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    size = os.path.getsize(args.out) / 1024
    print(f"已生成: {args.out}")
    print(f"  书签 {data['total']} 条 / 文件夹 {folders} 个 / 页面 {size:.0f} KB")
    if hot_urls:
        print(f"  常用 {len(hot_urls)} 个（最近 {hot_window} 天打开过）")
    elif hot_reason:
        print(f"  常用: 没算出来 —— {hot_reason}")
    if args.open:
        os.startfile(args.out)  # noqa: S606


def _headless() -> bool:
    """双击 .lnk（走 pythonw）时没有控制台窗口；从命令行跑则有。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow() == 0
    except Exception:
        return False


def _alert(title: str, text: str) -> None:
    """无控制台场景下，给失败留一个看得见的信号。

    带 15 秒超时（MessageBoxTimeoutW），避免没人理会时把进程永久挂住。
    设环境变量 NAV_NO_ALERT=1 可关掉弹窗（自动化 / 测试场景用）。
    """
    if os.environ.get("NAV_NO_ALERT"):
        return
    try:
        import ctypes
        flags = 0x10 | 0x40000 | 0x10000          # ICONERROR | TOPMOST | SETFOREGROUND
        u = ctypes.windll.user32
        try:
            u.MessageBoxTimeoutW(None, text, title, flags, 0, 15000)
        except Exception:
            u.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        # pythonw 下 stdout/stderr 可能不可用，一句 print 就能把整个流程炸掉
        for _name in ("stdout", "stderr"):
            if getattr(sys, _name, None) is None:
                setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        # 失败必须留下痕迹：写日志，没控制台再弹窗，绝不静默消失
        try:
            with open(os.path.join(HERE, "refresh-error.log"), "w",
                      encoding="utf-8") as fh:
                fh.write(datetime.datetime.now().isoformat() + "\n" + tb)
        except Exception:
            pass
        if _headless():
            _alert("书签导航：刷新失败",
                   "生成 index.html 时出错，页面仍是上一次的版本。\n\n"
                   "详情见 refresh-error.log\n\n"
                   + tb.strip().splitlines()[-1])
        sys.exit(1)
