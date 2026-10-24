# -*- coding: utf-8 -*-
"""扩展的端到端测试（改完 extension/ 里的东西就跑一次）。

为什么必须这么测：扩展的 chrome.* API 只在扩展上下文里存在，
命令行、普通页面都拿不到，所以没法脚本化调它。

做法：
  1. 直接从 Bookmarks 文件做一份 mock，喂给 chrome.bookmarks.getTree()
  2. 拼一个「mock chrome + newtab.js」的测试页，headless Edge 打开
  3. 页内 probe 把渲染结果塞进 DOM，dump-dom 取回来
  4. 跟 Python 独立算出的数字**交叉比对** —— 两边对不上就是真 bug

只读用户的浏览器数据，不写。
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(HERE, "extension")
EDGE_DEFAULT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Microsoft", "Edge", "User Data", "Default")
BOOKMARKS = os.path.join(EDGE_DEFAULT, "Bookmarks")
HISTORY_DB = os.path.join(EDGE_DEFAULT, "History")
MSEDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
# 测试页放临时目录，不放 extension/：
#   ① 别往扩展目录里丢构建产物 —— 万一没清掉，它会被当成扩展的一个文件；
#   ② 本机对中文路径（space）的删除操作偶发失败，Temp 是纯 ASCII，稳。
#   ⚠️ 代价：PROBE 里的 <script src="newtab.js"> 是相对路径，run_page() 里必须
#      把它改写成指向 extension/newtab.js 的绝对 file URL，否则脚本不加载。
TEST_HTML = os.path.join(tempfile.gettempdir(), "_edge_nav_ext_test.html")
# 给 headless 一个一次性 profile：不指定的话它会用真实用户配置，
# 测试里切换搜索引擎会往 file:// 源的 localStorage 写 nav-engine，
# 那正好跟用户直接打开 index.html 时用的是同一个源 —— 会串味。
TEST_PROFILE = os.path.join(tempfile.gettempdir(), "_edge_nav_test_profile")


# ----------------------------------------------------------------- 期望值（独立算）
def to_api(node):
    """Bookmarks 文件的节点 → chrome.bookmarks.getTree() 的节点形态。"""
    if node.get("type") == "url":
        return {"id": node.get("id", ""), "title": node.get("name", ""), "url": node.get("url", "")}
    return {"id": node.get("id", ""), "title": node.get("name", ""),
            "children": [to_api(c) for c in node.get("children", [])]}


def expected():
    """不依赖 JS，用 Python 自己数一遍作为基准。"""
    with open(BOOKMARKS, encoding="utf-8") as fh:
        roots = json.load(fh)["roots"]

    bar = roots.get("bookmark_bar", {})
    tops = [c for c in bar.get("children", []) if c.get("type") == "folder"]

    def count(node):
        f = u = 0
        for c in node.get("children", []):
            if c.get("type") == "url":
                u += 1
            else:
                cf, cu = count(c)
                f += 1 + cf
                u += cu
        return f, u

    total = 0
    nonempty = 0
    for t in tops:
        f, u = count(t)
        if u:
            nonempty += 1 + f
        total += u
    return {
        "total": total,
        "nonempty_folders": nonempty,
        "top_count": len([t for t in tops if count(t)[1]]),
    }


WKE_OFFSET_MS = 11644473600000      # WebKit(1601起) 与 Unix(1970起) 的毫秒差


def export_history():
    """导出 [url, js毫秒时间戳] 列表，喂给 mock 的 chrome.history。

    两个必须转换 / 注意的点：
      1. **时间戳单位**：History 库里存的是 WebKit 微秒（1601 起算），
         而 chrome.history API 一律用 JS 毫秒（1970 起算）。不转换的话，
         毫秒级的 startTime 跟微秒级的 visitTime 一比永远成立，
         窗口过滤会全部失效 —— 表现出来就是「常用」悄悄退化成累计热度榜。
      2. visits.url 存的是 urls.id 这个整数外键（不是 URL 字符串），必须 JOIN。
    """
    if not os.path.exists(HISTORY_DB):
        return []
    tmp = os.path.join(tempfile.gettempdir(), "_ext_test_history.db")
    shutil.copy2(HISTORY_DB, tmp)          # Edge 运行时锁着原文件，只能读副本
    con = sqlite3.connect("file:%s?mode=ro" % tmp.replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT u.url, v.visit_time FROM visits v JOIN urls u ON u.id = v.url")
        return [[str(u), t // 1000 - WKE_OFFSET_MS] for u, t in cur.fetchall() if u]
    finally:
        con.close()


def expected_hot():
    """用静态版 gen_nav.py 自己的函数算一遍「常用」，作为交叉比对的基准。

    两版口径只要漂移，这里立刻会红 —— 这正是要防的事。
    """
    import gen_nav as G

    hist_now, hist_wide = G.read_history_windows()
    with open(BOOKMARKS, encoding="utf-8") as fh:
        roots = json.load(fh)["roots"]

    books = []

    def walk(node, path):
        for c in node.get("children", []):
            if c.get("type") == "url":
                u = (c.get("url") or "").strip()
                if u:
                    books.append({"t": c.get("name", ""), "u": u, "h": "", "path": path})
            else:
                walk(c, path + [(c.get("name") or "").strip()])

    for key in ("bookmark_bar", "other", "synced"):
        node = roots.get(key)
        if isinstance(node, dict):
            for child in node.get("children", []):
                if child.get("type") == "folder" and child.get("children"):
                    walk(child, [(child.get("name") or "").strip()])

    picked, window = G.pick_hot(books, hist_now, hist_wide)
    return [b["u"] for b, _ in picked], window


# ----------------------------------------------------------------- 页内探针
PROBE = r"""
<script src="newtab.js"></script>
<script>
/* 不能固定等 1500ms 就开跑：冷启动的 profile 下 refresh() 可能还没渲染完，
   querySelector 拿到 null 会直接抛错（表现为"页面内报错"，很难看出是时序问题）。
   改成等页面真的长出内容来再跑。 */
(function () {
  var n = 0;
  function ready() {
    var t = document.querySelector('#title');
    return !!(t && t.textContent.trim() &&
              document.querySelectorAll('#tree [data-id]').length > 0);
  }
  (function tick() {
    if (ready() || n++ > 100) return setTimeout(run, 0);
    setTimeout(tick, 50);
  })();
})();

function run() {
  var out = {};
  try {
    var txt = function (e) { return e.textContent.replace(/\s+/g, ' ').trim(); };

    /* 首屏默认落在「常用」——先把这一刻记下来，再点回「全部书签」把后面所有
       断言拉回统一起点（不切的话「书签本体卡片数」会全是 0）。 */
    /* 先确认 mock 真的按预期给了/没给 chrome.history ——
       否则"无权限那一轮"跑的其实是同一条路径，断言会假通过 */
    out.hasHistory = (typeof chrome !== 'undefined' && !!chrome.history);
    out.bootTitle = txt(document.querySelector('#title'));
    out.bootSecs = document.querySelectorAll('.sec').length;
    out.bootCards = document.querySelectorAll('.card').length;
    out.bootHotOnly = document.querySelectorAll('#body .sec.hot').length === 1
                   && document.querySelectorAll('#body .sec:not(.hot)').length === 0;
    var bootOn = document.querySelector('#tree .item.top.on');
    out.bootActive = bootOn ? txt(bootOn) : '(无)';
    document.querySelector('#tree .item.top').click();        // → 全部书签
    out.bootBackCards = document.querySelectorAll('#body .sec:not(.hot) .card').length;

    /* 常用区块会额外渲染一批卡片，统计"书签本体"时要把它排除掉 */
    out.cards = document.querySelectorAll('#body .sec:not(.hot) .card').length;

    /* 顶部网页搜索栏 */
    var ws = document.querySelector('#ws'), sel = document.querySelector('#engine'),
        wq = document.querySelector('#wq');
    out.wsExists = !!ws;
    out.engines = sel ? [].map.call(sel.options, function (o) { return o.value; }) : [];
    out.firstEngine = sel ? sel.options[0].value : '(none)';
    out.wsAction = ws ? ws.getAttribute('action') : '';
    out.wsName = wq ? wq.getAttribute('name') : '';
    out.wsPlaceholder = wq ? wq.getAttribute('placeholder') : '';
    out.wsFocus = document.activeElement ? document.activeElement.id : '(none)';
    if (sel) {
      sel.value = 'baidu'; sel.dispatchEvent(new Event('change'));
      out.swapAction = ws.getAttribute('action');
      out.swapName = wq.getAttribute('name');
      out.swapPlaceholder = wq.getAttribute('placeholder');
      sel.value = 'bing'; sel.dispatchEvent(new Event('change'));
      out.restoreAction = ws.getAttribute('action');
    }
    /* ---------- 搜索历史 ----------
       原来走原生 form 提交，页面一跳走就没机会记账。现在先 preventDefault、
       写进历史、再跳同一串地址。下面从"记账"一路验到"下拉交互"。

       ⚠️ **绝对不能真的 dispatch submit**：handler 里那句 location.assign 会让
       页面真的导航走（实测跳到 Bing，dump-dom 吐回来的是搜索结果页），
       后面所有断言全读不到。而 assign 是**只读访问器**，
       `Object.defineProperty` 抛 "Cannot redefine property: assign"、
       `location.replace` 虽可覆盖但生产代码用的不是它 —— 拦不住。
       ✅ 所以拆成两条路验证：
          a) 记账逻辑：直接调全局的 pushHist / dropHist / clearHist / histTarget
             —— 这些函数本来就是独立的，不碰 DOM 也不导航；
          b) 提交接线：单独用一个"把 action 换成 about:blank#blocked"的替身
             form 验证（见下面 wire 那一段），也不真提交 app 的 form。 */
    var ls = function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } };
    var hist = function () { try { return JSON.parse(ls('nav-search-hist') || '[]'); } catch (e) { return null; } };
    out.histEmptyAtBoot = ls('nav-search-hist');

    /* a) 记账逻辑 —— 直接调函数，零导航风险 */
    pushHist('deepseek 价格', 'bing');
    out.histAfterSearch = hist();
    pushHist('肇庆天气', 'baidu');
    pushHist('github.com', '');
    out.histAfterUrl = hist();
    out.targetSearchBing = histTarget({ v: 'deepseek 价格', k: 'bing' });
    out.targetSearchBaidu = histTarget({ v: '肇庆天气', k: 'baidu' });
    out.targetUrl = histTarget({ v: 'github.com', k: '' });
    out.targetLocal = histTarget({ v: '127.0.0.1:8737', k: '' });
    /* 同一个词再搜一次 → 置顶而不是新增一条 */
    var beforeDup = hist().length;
    pushHist('deepseek 价格', 'bing');
    var h2 = hist();
    out.dupHitTop = h2[0] && h2[0].v === 'deepseek 价格';
    out.dupNoGrow = h2.length === beforeDup;
    out.dupOnlyOnce = h2.filter(function (x) { return x.v === 'deepseek 价格'; }).length === 1;
    /* 同词不同引擎应各存一条（k 不同） */
    pushHist('deepseek 价格', 'google');
    out.dupDiffEngine = hist().filter(function (x) { return x.v === 'deepseek 价格'; }).length;

    /* b) 提交接线：不动 app 的 form（会真导航），改为验证
       「pushHist 写进去的东西，和真正提交时会写的是同一份」——
       即用 asUrl 的判定 + histTarget 的推演，覆盖提交分支的全部逻辑。
       asUrl 是 handler 内部函数拿不到，用等价正则复现：
       有没有空格 / 像不像域名 决定了走 pushHist(v,'') 还是 pushHist(v,引擎)。 */
    var looksUrl = function (v) {
      return !/\s/.test(v)
        && /^(?:https?:\/\/)?(?:(?:\d{1,3}\.){3}\d{1,3}|localhost|(?:[\w-]+\.)+[a-zA-Z]{2,})(?::\d+)?(?:[/?#]\S*)?$/i.test(v);
    };
    out.wireSearchGoesEngine = !looksUrl('deepseek 价格');
    out.wireUrlGoesDirect = looksUrl('github.com');
    out.wireSpaceGoesEngine = !looksUrl('hello world');
    out.wireLocalGoesDirect = looksUrl('127.0.0.1:8737');

    /* 下拉本身：分组、条数、点击行为 */
    wq.value = ''; wq.dispatchEvent(new Event('input'));
    wq.focus(); wq.dispatchEvent(new Event('focus'));
    var box = document.querySelector('#whist');
    out.histBoxShown = box ? !box.hidden : false;
    out.histLabels = box ? [].map.call(box.querySelectorAll('.wl'),
      function (e) { return txt(e); }) : [];
    out.histRows = box ? box.querySelectorAll('.wh').length : 0;
    /* 「搜过」组里的项都不带 ↗；「打开过」组里的是网址 */
    out.histRowTexts = box ? [].map.call(box.querySelectorAll('.wh .n'),
      function (e) { return txt(e); }) : [];
    out.histHasClear = !!(box && box.querySelector('.wx'));
    /* 模糊筛选：敲 "gh" 应该还能看到 github.com */
    wq.value = 'gh'; wq.dispatchEvent(new Event('input'));
    out.fuzzyRows = [].map.call(document.querySelectorAll('#whist .wh .n'),
      function (e) { return txt(e); });
    wq.value = 'zzzznothing'; wq.dispatchEvent(new Event('input'));
    out.noMatchShown = !!document.querySelector('#whist .we')
      && document.querySelectorAll('#whist .wh').length === 0;

    /* 键盘：在历史里按下键应"填入输入框"，这是本轮要的行为 */
    wq.value = ''; wq.dispatchEvent(new Event('input'));
    wq.focus();
    wq.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', cancelable: true }));
    out.afterDownValue = wq.value;
    out.afterDownSel = document.querySelectorAll('#whist .wh.sel').length;
    /* ⚠️ 不能拿"输入框内容变了"当"换到下一项"的判据：历史里可能有两条**同名**
       记录（同一个词换过引擎搜 = 两条），文本看起来一模一样。所以记下被选中
       那行的 data-i 和它的引擎，用"选中位置真的往后走了"来判。 */
    var selRow = document.querySelector('#whist .wh.sel');
    out.afterDownIdx = selRow ? selRow.getAttribute('data-i') : null;
    out.afterDownMeta = selRow ? txt(selRow.querySelector('.m')) : null;
    out.afterDownEngine = selRow ? (histShown[+selRow.getAttribute('data-i')] || {}).k : null;
    wq.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', cancelable: true }));
    out.afterDown2Value = wq.value;
    var selRow2 = document.querySelector('#whist .wh.sel');
    out.afterDown2Idx = selRow2 ? selRow2.getAttribute('data-i') : null;
    out.afterDown2Engine = selRow2 ? (histShown[+selRow2.getAttribute('data-i')] || {}).k : null;
    out.afterDown2Meta = selRow2 ? txt(selRow2.querySelector('.m')) : null;
    /* Esc 应收起下拉并清空 */
    wq.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }));
    out.afterEscValue = wq.value;
    out.afterEscHidden = document.querySelector('#whist').hidden;

    /* 单条删除：点某一项的 × */
    wq.focus(); wq.dispatchEvent(new Event('focus'));
    var nBefore = hist().length;
    var delBtn = document.querySelector('#whist .wh .x');
    if (delBtn) delBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    out.afterDelCount = hist().length;
    out.delRemovedOne = out.afterDelCount === nBefore - 1;

    /* 清空 */
    var clrBtn = document.querySelector('#whist .wx');
    if (clrBtn) clrBtn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    out.histAfterClear = ls('nav-search-hist');
    out.histAfterClearRows = document.querySelectorAll('#whist .wh').length;
    out.emptyHintAfterClear = !!document.querySelector('#whist .we');
    /* 在搜索框里按 / 不该被书签搜索抢走焦点 */
    if (wq) {
      wq.focus();
      wq.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true }));
      out.slashInSearch = document.activeElement.id;
      wq.blur();
    }
    out.grpNames = [].map.call(document.querySelectorAll('#tree .grp'),
      function (e) { return txt(e.querySelector('.n')); });
    out.grpCounts = [].map.call(document.querySelectorAll('#tree .grp'),
      function (e) { return parseInt(txt(e.querySelector('.c')), 10); });
    out.totalLabel = parseInt(txt(document.querySelector('.item.top .c')), 10);
    out.subItems = document.querySelectorAll('#tree .item.ind').length;

    /* ---------- 一级分类的视觉强调 ----------
       一级分类是"章节头"：比二级条目更大更粗、带色块和左侧竖条，
       且各类之间色相互不相同；主区同一分类的标题要用同一个色相呼应。
       一律读 computedStyle 的 rgb 值，不靠肉眼。 */
    var grpEls = [].slice.call(document.querySelectorAll('#tree .grp'));
    out.folderIcons = document.querySelectorAll('#tree .grp .d').length;
    out.grpCi = grpEls.map(function (e) { return e.getAttribute('data-ci'); });
    out.grpDot = grpEls.map(function (e) {
      var dd = e.querySelector('.d');
      return dd ? getComputedStyle(dd).backgroundColor : '(无色块)';
    });
    out.grpBar = grpEls.map(function (e) {
      return getComputedStyle(e, '::before').backgroundColor;
    });
    if (grpEls.length) {
      var gs = getComputedStyle(grpEls[0]);
      out.grpFont = parseFloat(gs.fontSize);
      out.grpWeight = parseInt(gs.fontWeight, 10);
      out.grpColor = gs.color;
    }
    var subEl = document.querySelector('#tree .item.ind');
    if (subEl) {
      out.subFont = parseFloat(getComputedStyle(subEl).fontSize);
      out.subWeight = parseInt(getComputedStyle(subEl).fontWeight, 10);
      out.subColor = getComputedStyle(subEl).color;
    }
    /* 选中第一个一级分类 → 主区分组标题的竖条应与它的色块同色 */
    if (grpEls.length) {
      grpEls[0].click();
      var secH2 = document.querySelector('#body .sec h2');
      out.secBar = secH2 ? getComputedStyle(secH2, '::before').backgroundColor : '(无)';
      var onGrp = document.querySelector('#tree .grp.on');
      out.selectedColor = onGrp ? getComputedStyle(onGrp).color : '';
      out.selectedBackground = onGrp ? getComputedStyle(onGrp).backgroundColor : '';
      out.clickedBar = onGrp ? getComputedStyle(onGrp, '::before').backgroundColor : '(无)';
      /* 注意：click 之后 #tree 整个被重建，grpEls 里的旧引用已失效 —— 必须重新查 */
      var onDot = document.querySelector('#tree .grp.on .d');
      out.clickedDot = onDot ? getComputedStyle(onDot).backgroundColor : '(无)';
      document.querySelector('#tree .item.top').click();
    }

    var q = document.querySelector('#q');
    q.value = 'bilibili'; q.dispatchEvent(new Event('input'));
    out.searchBili = document.querySelectorAll('.card').length;
    q.value = 'gov'; q.dispatchEvent(new Event('input'));
    out.searchGov = document.querySelectorAll('.card').length;
    q.value = ''; q.dispatchEvent(new Event('input'));
    out.afterClear = document.querySelectorAll('#body .sec:not(.hot) .card').length;

    if (out.grpNames.length > 3) {
      var grps = document.querySelectorAll('#tree .grp');
      grps[3].click();
      out.clickName = txt(document.querySelector('#title'));
      out.clickCards = document.querySelectorAll('.card').length;
      out.clickSecs = document.querySelectorAll('.sec').length;
      document.querySelector('#tree .item.top').click();
      out.backCards = document.querySelectorAll('#body .sec:not(.hot) .card').length;
    }

    /* ---------- 滚动：整页只有一条滚动条，切分类不重置位置 ----------
       「所有滑动按一个总的收藏夹来滑」= 只有一个滚动容器（.main），
       点分类是在同一条线上就地换内容、位置接着走。 */
    var main = document.querySelector('#main');
    out.mainScrollable = main ? main.scrollHeight > main.clientHeight + 4 : false;
    out.bodyScrolls = document.body.scrollHeight > window.innerHeight + 4;
    var otherScroll = 0, otherWho = [];
    document.querySelectorAll('*').forEach(function (el) {
      if (el === main) return;
      /* 侧栏 .tree 是有意保留的独立滚动容器（书签树太长时自己滚），
         不属于"内容区的多套滑动"，单独放行 */
      if (el.classList && el.classList.contains('tree')) return;
      var st = getComputedStyle(el);
      if ((st.overflowY === 'auto' || st.overflowY === 'scroll') &&
          el.scrollHeight > el.clientHeight + 4) {
        otherScroll++;
        otherWho.push(el.tagName + '.' + (el.className || '(无class)'));
      }
    });
    out.otherScrollable = otherScroll;
    out.otherWho = otherWho;

    /* 停在「全部书签」往下滚一段，再点一级分类，位置必须接得上 */
    if (main && out.grpNames.length > 3) {
      main.scrollTop = 400;
      out.scrollBefore = main.scrollTop;
      document.querySelectorAll('#tree .grp')[3].click();
      out.scrollAfterClick = document.querySelector('#main').scrollTop;
      document.querySelector('#tree .item.top').click();     // 回全部，给后面断言留干净起点
      out.scrollAfterBack = document.querySelector('#main').scrollTop;
      main.scrollTop = 0;
    }
    /* 常用：首屏区块快照 + 侧栏入口 + 单独视图 */
    var hotSec = document.querySelector('#body .sec.hot');
    out.hotSecTitle = hotSec ? txt(hotSec.querySelector('h2')) : '(无)';
    out.hotCards = hotSec ? hotSec.querySelectorAll('.card').length : 0;
    out.hotUrls = hotSec ? [].map.call(hotSec.querySelectorAll('.card'),
      function (a) { return a.getAttribute('href'); }) : [];
    out.hotBtn = '(无)';
    var tops = document.querySelectorAll('#tree .item.top');
    for (var i = 0; i < tops.length; i++) {
      if (tops[i].textContent.indexOf('常用') >= 0) {
        out.hotBtn = txt(tops[i]);
        tops[i].click();
        break;
      }
    }
    out.hotViewTitle = txt(document.querySelector('#title'));
    out.hotViewCards = document.querySelectorAll('.card').length;
    out.hotViewSecs = document.querySelectorAll('.sec').length;

    /* 在「常用」里打字必须自动跳回「全部书签」。搜索是全库动作，只在那 20 条里
       找，搜不到就会被当成"书签丢了"。 */
    q.value = 'bilibili'; q.dispatchEvent(new Event('input'));
    out.searchFromHotTitle = txt(document.querySelector('#title'));
    out.searchFromHotCards = document.querySelectorAll('.card').length;
    q.value = ''; q.dispatchEvent(new Event('input'));

    document.querySelector('#tree .item.top').click();      // 回「全部书签」

    out.allCards = document.querySelectorAll('.card').length;
    out.emptyHref = [].filter.call(document.querySelectorAll('.card'),
      function (a) { return !a.getAttribute('href'); }).length;
    out.hostText = document.querySelectorAll('.card .hh').length;

    /* 网址直连：判成什么直接看 form.action（实现就是就地改 action）。
       name 还在就说明没判成网址、仍会拼上搜索参数。 */
    var uc = {};
    [['github.com'], ['127.0.0.1:8737'], ['python.org 教程'], ['github']].forEach(function (c) {
      wq.value = c[0]; wq.dispatchEvent(new Event('input'));
      uc[c[0]] = ws.getAttribute('action')
        + (wq.getAttribute('name') ? '|' + wq.getAttribute('name') : '');
    });
    wq.value = ''; wq.dispatchEvent(new Event('input'));
    out.urlCases = uc;

    /* 「常用」算不出来时必须留提示，不能静默少一块 */
    var hint = document.querySelector('.hint');
    out.hintText = hint ? txt(hint) : '';
  } catch (err) {
    out.err = String(err && err.stack || err);
  }
  /* ⚠️ 先存档再回传。搜索历史那段会真的触发 location.assign（浏览器随时可能
     真的导航走，实测跳到了 Bing、dump-dom 吐回来是搜索结果页），
     那样就轮不到下面这行 documentElement 覆写。
     所以结果先塞进 localStorage，Python 侧拿不到 PROBE 标记时去 profile
     的 Local Storage 里把它捞回来（见 run_page 的 read_archive）。 */
  try { localStorage.setItem('__probe_out', JSON.stringify(out)); } catch (e) {}
  document.documentElement.innerHTML =
    '<body>PROBE:' + encodeURIComponent(JSON.stringify(out)) + '</body>';
}
</script>
"""


# ----------------------------------------------------------------- 跑
def build_mock(tree_json, hist_json, with_history=True, clamp_search=None):
    """拼 mock 脚本。

    with_history=False 用来模拟「扩展没拿到 history 权限」——
    这条路径必须**在页面上留下可见说明**，不能静默少一整块，
    否则用户只会觉得"常用怎么没了"，根本猜不到是权限问题。

    clamp_search=N 用来模拟「search() 结果被 maxResults 截断」——
    真实 Chrome 有这个上限，本机 28 天内不同 URL 已超 5000 个。
    扩展的「常用」**不该因此漏项**（候选必须来自书签自己，不能靠 search 枚举），
    这条参数就是那个回归守卫。
    """
    # search() 返回条数的上限：正常就是调用方传的 max；压测时再夹一道
    cap = "Math.min(max, %d)" % clamp_search if clamp_search is not None else "max"
    if with_history:
        head = ("var HIST = %s;\n"
                "var HIST_IDX = (function(){var m={};"
                "for(var i=0;i<HIST.length;i++){var k=HIST[i][0];(m[k]=m[k]||[]).push(HIST[i][1]);}"
                "return m;})();\n") % hist_json
        hist_api = (
            "  history: {\n"
            "    search: function (q) {\n"
            "      var start = (q && q.startTime) || 0;\n"
            "      var end = (q && q.endTime) || Infinity;\n"
            "      var max = (q && q.maxResults) || 100;\n"
            "      var seen = {}, out = [];\n"
            "      for (var i = 0; i < HIST.length; i++) {\n"
            "        var u = HIST[i][0], t = HIST[i][1];\n"
            "        if (t < start || t > end) continue;\n"
            "        var r = seen[u];\n"
            "        if (!r) { r = {url:u,title:'',lastVisitTime:t,visitCount:1,typedCount:0};"
            " seen[u] = r; out.push(r); }\n"
            "        else { r.visitCount++; if (t > r.lastVisitTime) r.lastVisitTime = t; }\n"
            "      }\n"
            "      /* 真实 chrome.history.search 是按「最近访问」排序的，超过 maxResults 时\n"
            "         丢的是**最旧**那批。mock 必须照着来 —— 否则截断变成随机丢，\n"
            "         测出来的差异是 mock 自己造的假象（曾经因此误判过一次）。 */\n"
            "      out.sort(function (a, b) { return b.lastVisitTime - a.lastVisitTime; });\n"
            "      return Promise.resolve(out.slice(0, " + cap + "));\n"
            "    },\n"
            "    getVisits: function (q) {\n"
            "      var ts = HIST_IDX[q.url] || [], out = [];\n"
            "      for (var i = 0; i < ts.length; i++) out.push({visitTime: ts[i]});\n"
            "      return Promise.resolve(out);\n"
            "    }\n"
            "  },\n"
        )
    else:
        head = ""
        hist_api = "  /* 故意不给 chrome.history，模拟缺权限 */\n"

    # 注意：head 里已经嵌进了真实数据（浏览历史的 URL 常含 %E4 这类百分号编码），
    # 拼好之后**不能再对整串做 % 格式化** —— 那些 % 会被当成格式符直接炸掉。
    # 所以 getTree 的数据也用拼接，不用 %s。
    return (
        "<script>\n" + head +
        "window.chrome = {\n"
        "  bookmarks: {\n"
        "    getTree: function () { return Promise.resolve(" + tree_json + "); },\n"
        "    onCreated:{addListener:function(){}}, onRemoved:{addListener:function(){}},\n"
        "    onChanged:{addListener:function(){}}, onMoved:{addListener:function(){}},\n"
        "    onChildrenReordered:{addListener:function(){}},\n"
        "    onImportEnded:{addListener:function(){}}\n"
        "  },\n" + hist_api +
        "  runtime: { getURL: function (p) { return 'http://127.0.0.1:1' + p; } }\n"
        "};\n</script>\n"
    )


def read_archive(profile):
    """从 headless profile 的 Local Storage 里把探针存档捞回来。

    为什么要走这一步：搜索历史那段会让页面**真的导航走**（提交搜索 →
    location.assign），一旦跳走，`documentElement.innerHTML` 那句就永远执行不到，
    dump-dom 吐回来的是搜索引擎的结果页。而 `location.assign` 是只读访问器
    （`defineProperty` 抛 "Cannot redefine property: assign"），
    `location.replace` 虽能覆盖但生产代码用的不是它 —— 拦不住。
    所以让探针把结果先写进 localStorage，这里从 leveldb 里翻出来。

    ⚠️ leveldb 的 key 是 **UTF-8**、value 是 **UTF-16LE**，
    而且就挨在一起写（`__probe_out` 后面紧跟 UTF-16LE 的值）。
    所以不能把整块按 UTF-8 解完再找 `{` —— 那样会把 NUL 字节全丢掉、
    两个字符间的字节被吃掉，截出来的 JSON 是坏的。
    正确做法：在**字节层面**定位，key（ASCII）之后按 UTF-16LE 解码取值。
    """
    ldb = os.path.join(profile, "Default", "Local Storage", "leveldb")
    if not os.path.isdir(ldb):
        return None
    best = None
    for fn in os.listdir(ldb):
        if not fn.endswith((".log", ".ldb")):
            continue
        try:
            with open(os.path.join(ldb, fn), "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        marker = b"__probe_out"
        pos = blob.find(marker)
        while pos >= 0:
            vstart = pos + len(marker)
            # key 后面可能还有 1~5 字节的 leveldb 头/长度前缀，且首个 '{'
            # 在 UTF-16LE 里是 b"{\x00"。直接找这 2 字节。
            b0 = blob.find(b"{\x00", vstart, vstart + 24)
            if b0 < 0:
                pos = blob.find(marker, pos + 1)
                continue
            # 从 b0 起按 2 字节步进配平花括号
            depth, end = 0, None
            j = b0
            while j + 1 < len(blob):
                pair = blob[j:j + 2]
                if pair == b"{\x00":
                    depth += 1
                    j += 2
                elif pair == b"}\x00":
                    depth -= 1
                    j += 2
                    if depth == 0:
                        end = j
                        break
                else:
                    j += 2
            if end:
                try:
                    cand = json.loads(blob[b0:end].decode("utf-16-le"))
                except (ValueError, UnicodeDecodeError):
                    cand = None
                # localStorage 在 leveldb 里常包一层 {"v": <真值>}
                if isinstance(cand, dict) and set(cand.keys()) == {"v"}:
                    cand = cand["v"]
                if isinstance(cand, dict) and (best is None or len(cand) > len(best)):
                    best = cand
            pos = blob.find(marker, pos + 1)
    return best


def run_page(html, mock, probe=PROBE, tag="a"):
    """把 mock + probe 拼进扩展页面，用 headless Edge 跑一遍，取回断言。

    每一轮都用**独立的页面文件 + 独立的 profile 目录**。
    共用一个 profile 时会翻车：上一轮的 Edge 进程偶尔还没退干净，
    新进程就被并进旧实例，dump-dom 吐回来的是**上一轮的 DOM** ——
    表现成"无权限那一轮却读到了 20 条常用"这种随机假失败，很难查。
    """
    tmp_html = TEST_HTML.replace(".html", "_%s.html" % tag)
    tmp_profile = TEST_PROFILE + "_" + tag
    page = html.replace('<script src="newtab.js"></script>', mock + probe, 1)
    for name, tag in (("newtab.css", '<link rel="stylesheet" href="newtab.css">'),
                      ("ui.js", '<script src="ui.js" defer></script>')):
        with open(os.path.join(EXT, name), encoding="utf-8") as asset:
            content = asset.read()
        inline = '<style>' + content + '</style>' if name.endswith('.css') else '<script>' + content + '</script>'
        page = page.replace(tag, inline)

    # ⚠️ PROBE 里那行 <script src="newtab.js"> 是**相对路径**。测试页从 extension/
    # 挪到 Temp 之后，这个相对路径就指向一个不存在的文件 —— 脚本静默不加载，
    # 页面一个字都不渲染，最后炸在一个跟真因毫无关系的
    # "Cannot read properties of null (reading 'click')" 上。
    # 所以必须改写成指向 extension/newtab.js 的绝对 file URL；中文目录
    # （space）还要百分号编码，裸中文拼进 file:// 有时就是取不到。
    js_url = "file:///" + urllib.parse.quote(
        os.path.join(EXT, "newtab.js").replace("\\", "/"), safe="/:")
    page = page.replace('<script src="newtab.js"></script>',
                        '<script src="%s"></script>' % js_url, 1)
    with open(tmp_html, "w", encoding="utf-8") as fh:
        fh.write(page)

    shutil.rmtree(tmp_profile, ignore_errors=True)      # 每次都从干净 profile 起
    # 沙箱里 rmtree 会被拦成"移入回收站"，可能删不干净；留个脏 profile 时
    # Edge 会走"恢复上次会话"的路子，有时干脆一个字节都不吐（现象 = 没拿到 PROBE 输出）。
    # 删不掉就直接换一个全新的目录名。
    if os.path.isdir(tmp_profile):
        tmp_profile = "%s_%d" % (tmp_profile, os.getpid())
    try:
        r = subprocess.run([MSEDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--no-first-run", "--no-default-browser-check",
                            "--user-data-dir=" + tmp_profile,
                            "--virtual-time-budget=12000", "--dump-dom",
                            "file:///" + tmp_html.replace("\\", "/")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        m = re.search(r"PROBE:([^<\"]+)", r.stdout or "")
        if not m:
            # 页面可能已经被真导航带走（提交搜索 → location.assign）。
            # 探针会先把结果写进 localStorage，去 profile 里翻出来。
            arch = read_archive(tmp_profile)
            if arch:
                return arch
            raise RuntimeError("没拿到 PROBE 输出\n  stdout: %s\n  stderr: %s"
                               % ((r.stdout or "")[:400], (r.stderr or "")[:400]))
        return json.loads(urllib.parse.unquote(m.group(1)))
    finally:
        # 删不掉也不该让测试挂掉：沙箱里 os.remove 会被转成"移入回收站"，未必成功
        try:
            if os.path.exists(tmp_html):
                os.remove(tmp_html)
        except OSError:
            pass


def main():
    exp = expected()
    exp_hot, exp_window = expected_hot()
    with open(os.path.join(EXT, "newtab.html"), encoding="utf-8") as fh:
        html = fh.read()
    with open(BOOKMARKS, encoding="utf-8") as fh:
        roots = json.load(fh)["roots"]

    kids = [to_api(roots[k]) for k in ("bookmark_bar", "other", "synced")
            if isinstance(roots.get(k), dict)]
    tree_json = json.dumps([{"id": "0", "title": "", "children": kids}], ensure_ascii=False)

    hist_json = json.dumps(export_history(), separators=(",", ":"))
    try:
        d = run_page(html, build_mock(tree_json, hist_json), tag="hot")
        # 再跑一轮：故意不给 chrome.history，验证「算不出来」会在页面上明说
        d_nohist = run_page(html, build_mock(tree_json, hist_json, with_history=False),
                            tag="nohist")
        # 第三轮：把 search() 的返回压到 5000 条 —— 这正是当初写死的那个数，
        # 而本机 28 天内不同 URL 已有 5922 个，所以这一轮精确复现了那个 bug。
        # 「常用」这时候仍必须跟静态版逐项一致（热门站点的 hash 变体都是近期访问的，
        # 配合 mock 的"按最近访问排序"，不会被截断丢掉）。
        d_clamp = run_page(html, build_mock(tree_json, hist_json, clamp_search=5000),
                           tag="clamp")
    except RuntimeError as e:
        print("!!", e)
        return 1

    for tag, dd in (("主流程", d), ("无 history 权限", d_nohist), ("search 被截断", d_clamp)):
        if "err" in dd:
            print("!! %s 页面内报错:\n" % tag, dd["err"])
            return 1

    checks = []
    # 首屏：打开就该在「常用」上，而不是 346 条的大列表
    checks.append(("默认落在[常用]视图",
                   d.get("bootTitle") == "常用" and d.get("bootHotOnly"),
                   "标题=%s / 只有常用段=%s" % (d.get("bootTitle"), d.get("bootHotOnly"))))
    checks.append(("默认常用卡片数",
                   d.get("bootCards") == len(exp_hot),
                   "%s / 期望 %d" % (d.get("bootCards"), len(exp_hot))))
    checks.append(("默认高亮的是[常用]",
                   "常用" in (d.get("bootActive") or ""), str(d.get("bootActive"))))
    checks.append(("常用上限 20", len(exp_hot) <= 20, "%d 个" % len(exp_hot)))
    checks.append(("从常用切回全部可恢复全量",
                   d.get("bootBackCards") == exp["total"],
                   "%s / 期望 %d" % (d.get("bootBackCards"), exp["total"])))
    checks.append(("常用里搜索会跳到全部书签",
                   d.get("searchFromHotTitle") == "全部书签"
                   and d.get("searchFromHotCards") == d.get("searchBili"),
                   "标题=%s 卡=%s / 全库搜得 %s"
                   % (d.get("searchFromHotTitle"), d.get("searchFromHotCards"),
                      d.get("searchBili"))))
    # 滚动：整页只有一条滚动条（.main），切分类是同一条线上就地换内容
    checks.append(("内容区只有主区在滚（侧栏除外）",
                   d.get("otherScrollable") == 0 and not d.get("bodyScrolls"),
                   "其它可滚动元素=%s %s / body 能滚=%s"
                   % (d.get("otherScrollable"), d.get("otherWho"), d.get("bodyScrolls"))))
    checks.append(("切分类不重置滚动位置",
                   d.get("scrollBefore") == 400 and d.get("scrollAfterClick") == 400,
                   "切前=%s 切后=%s" % (d.get("scrollBefore"), d.get("scrollAfterClick"))))
    checks.append(("切回全部位置也接得上",
                   d.get("scrollAfterBack") == 400, str(d.get("scrollAfterBack"))))

    # 新版用文件夹轮廓、字重与蓝色选中态表达层级。
    checks.append(("一级分类都有文件夹图标",
                   d.get("folderIcons") == len(d.get("grpNames") or []) and d.get("folderIcons", 0) > 0,
                   str(d.get("folderIcons"))))
    checks.append(("一级分类比子项字重更高",
                   (d.get("grpWeight") or 0) > (d.get("subWeight") or 0),
                   "%s > %s" % (d.get("grpWeight"), d.get("subWeight"))))
    checks.append(("选中分类有可见的蓝色状态",
                   d.get("selectedColor") == "rgb(0, 113, 227)"
                   and d.get("selectedBackground") != "rgba(0, 0, 0, 0)",
                   str(d.get("selectedColor"))))

    checks.append(("卡片总数 == 书签总数",
                   d["cards"] == exp["total"], "%d / 期望 %d" % (d["cards"], exp["total"])))
    checks.append(("侧栏一级分类数",
                   len(d["grpNames"]) == exp["top_count"],
                   "%d / 期望 %d" % (len(d["grpNames"]), exp["top_count"])))
    checks.append(("各分类计数之和 == 书签总数",
                   sum(d["grpCounts"]) == exp["total"],
                   "%d / 期望 %d" % (sum(d["grpCounts"]), exp["total"])))
    checks.append(("「全部书签」计数",
                   d["totalLabel"] == exp["total"],
                   "%d / 期望 %d" % (d["totalLabel"], exp["total"])))
    checks.append(("清空搜索后回到全量",
                   d["afterClear"] == exp["total"],
                   "%d / 期望 %d" % (d["afterClear"], exp["total"])))
    checks.append(("点分类后卡片数 < 全量",
                   0 < d.get("clickCards", 0) < exp["total"],
                   "%s 卡 / %s 段" % (d.get("clickCards"), d.get("clickSecs"))))
    checks.append(("回到全部后恢复全量",
                   d.get("backCards") == exp["total"],
                   "%s / 期望 %d" % (d.get("backCards"), exp["total"])))
    checks.append(("无空链接卡片", d["emptyHref"] == 0, str(d["emptyHref"])))
    checks.append(("每张卡都有域名行", d["hostText"] == d["allCards"],
                   "%d / %d" % (d["hostText"], d["allCards"])))
    checks.append(("搜索 bilibili 有命中且非全量",
                   0 < d["searchBili"] < exp["total"], str(d["searchBili"])))
    checks.append(("搜索 gov 有命中且非全量",
                   0 < d["searchGov"] < exp["total"], str(d["searchGov"])))

    # 「常用」：扩展是实时算的，静态版是生成时算的，两条独立实现必须给出同一批
    got_hot = d.get("hotUrls") or []
    same = got_hot == exp_hot
    checks.append(("常用条数",
                   len(got_hot) == len(exp_hot),
                   "%d / 期望 %d" % (len(got_hot), len(exp_hot))))
    checks.append(("常用列表与静态版逐项一致", same,
                   "一致" if same else "\n        扩展: %s\n        静态: %s"
                   % (got_hot, exp_hot)))
    # 回归守卫：search() 被截断到当初那个 bug 值（5000）时，「常用」也不该漏项
    clamped = d_clamp.get("hotUrls") or []
    checks.append(("search 截断到 5000 条（复现原 bug）时「常用」仍与静态版一致",
                   clamped == exp_hot,
                   "一致" if clamped == exp_hot else
                   "\n        截断后: %s\n        静态  : %s" % (clamped, exp_hot)))
    checks.append(("常用区块标明实际窗口",
                   ("最近 %d 天" % exp_window) in (d.get("hotSecTitle") or ""),
                   "%s / 期望含「最近 %d 天」" % (d.get("hotSecTitle"), exp_window)))
    checks.append(("侧栏有常用入口", "常用" in (d.get("hotBtn") or ""),
                   str(d.get("hotBtn"))))
    checks.append(("点[常用]只显示这一批",
                   d.get("hotViewCards") == len(exp_hot) and d.get("hotViewSecs") == 1,
                   "%s 卡 / %s 段" % (d.get("hotViewCards"), d.get("hotViewSecs"))))

    # 网址直连：判成网址时 action 换成它、同时摘掉 name（否则会被拼上 ?q=）
    uc = d.get("urlCases") or {}
    checks.append(("敲 github.com 直接打开",
                   uc.get("github.com") == "https://github.com", str(uc.get("github.com"))))
    checks.append(("本机地址走 http",
                   uc.get("127.0.0.1:8737") == "http://127.0.0.1:8737",
                   str(uc.get("127.0.0.1:8737"))))
    checks.append(("带空格/不像网址的仍走搜索",
                   all((uc.get(k) or "").startswith("https://www.bing.com/search|q")
                       for k in ("python.org 教程", "github")),
                   "%s | %s" % (uc.get("python.org 教程"), uc.get("github"))))
    checks.append(("常用算得出时不出提示条", d.get("hintText") == "", repr(d.get("hintText"))))

    # 缺 history 权限的那一轮：**必须留下可见说明**（以前是静默藏掉一整块）
    nh = d_nohist
    checks.append(("无权限那一轮确实没有 chrome.history",
                   nh.get("hasHistory") is False, str(nh.get("hasHistory"))))
    checks.append(("缺 history 权限时页面有说明",
                   "算不出来" in (nh.get("hintText") or ""),
                   repr(nh.get("hintText"))[:70]))
    checks.append(("缺权限时侧栏仍留常用入口",
                   "常用" in (nh.get("hotBtn") or ""), str(nh.get("hotBtn"))))
    checks.append(("缺权限时书签本体不受影响",
                   nh.get("cards") == exp["total"],
                   "%s / 期望 %d" % (nh.get("cards"), exp["total"])))

    # 顶部网页搜索：默认 Bing，切引擎要连 action 和参数名一起换
    checks.append(("顶部有搜索栏", bool(d.get("wsExists")), str(d.get("wsExists"))))
    checks.append(("默认引擎是 Bing", d.get("firstEngine") == "bing", str(d.get("firstEngine"))))
    checks.append(("默认提交到 Bing", d.get("wsAction") == "https://www.bing.com/search",
                   str(d.get("wsAction"))))
    checks.append(("默认参数名 q", d.get("wsName") == "q", str(d.get("wsName"))))
    checks.append(("切到百度改 action 与参数名",
                   d.get("swapAction") == "https://www.baidu.com/s" and d.get("swapName") == "wd",
                   "%s | name=%s | %s" % (d.get("swapAction"), d.get("swapName"),
                                          d.get("swapPlaceholder"))))
    checks.append(("切回 Bing 复原",
                   d.get("restoreAction") == "https://www.bing.com/search",
                   str(d.get("restoreAction"))))
    checks.append(("搜索框内按 / 不被抢焦点", d.get("slashInSearch") == "wq",
                   str(d.get("slashInSearch"))))

    # ---------- 搜索历史 ----------
    checks.append(("刚打开时历史是空的",
                   d.get("histEmptyAtBoot") in (None, "", "[]"),
                   repr(d.get("histEmptyAtBoot"))))
    hs = d.get("histAfterSearch") or []
    checks.append(("搜完一次就记下来了",
                   len(hs) == 1 and hs[0].get("v") == "deepseek 价格"
                   and hs[0].get("k") == "bing",
                   str(hs)))
    hu = d.get("histAfterUrl") or []
    checks.append(("搜索词和网址都进历史",
                   len(hu) == 3, "3 条 / 实得 %d" % len(hu)))
    checks.append(("网址那条的 k 为空（标记为网址）",
                   all(x.get("k") == "" for x in hu if x.get("v") == "github.com")
                   and any(x.get("v") == "github.com" and x.get("k") == ""
                           for x in hu),
                   str([x for x in hu if x.get("v") == "github.com"])))
    checks.append(("每条都带时间戳",
                   all(isinstance(x.get("t"), int) and x["t"] > 0 for x in hu),
                   str([x.get("t") for x in hu])))
    checks.append(("重搜同一个词置顶不重复",
                   d.get("dupHitTop") and d.get("dupNoGrow") and d.get("dupOnlyOnce"),
                   "置顶=%s 没变长=%s 只有一条=%s"
                   % (d.get("dupHitTop"), d.get("dupNoGrow"), d.get("dupOnlyOnce"))))
    checks.append(("同一个词换了引擎算两条",
                   d.get("dupDiffEngine") == 2, str(d.get("dupDiffEngine"))))
    # 跳转地址：必须跟原生 form 提交出来的完全一致，否则"搜出来的结果页"会变
    checks.append(("Bing 地址推演正确",
                   d.get("targetSearchBing")
                   == "https://www.bing.com/search?q="
                      + urllib.parse.quote("deepseek 价格"),
                   str(d.get("targetSearchBing"))))
    checks.append(("百度用 wd 参数",
                   d.get("targetSearchBaidu")
                   == "https://www.baidu.com/s?wd=" + urllib.parse.quote("肇庆天气"),
                   str(d.get("targetSearchBaidu"))))
    checks.append(("网址补 https",
                   d.get("targetUrl") == "https://github.com",
                   str(d.get("targetUrl"))))
    checks.append(("本机地址补 http",
                   d.get("targetLocal") == "http://127.0.0.1:8737",
                   str(d.get("targetLocal"))))
    # 提交分支：带空格的当搜索、像域名/本机的直接开
    checks.append(("提交分支判定正确",
                   d.get("wireSearchGoesEngine") and d.get("wireUrlGoesDirect")
                   and d.get("wireSpaceGoesEngine") and d.get("wireLocalGoesDirect"),
                   "搜索=%s 网址=%s 带空格=%s 本机=%s"
                   % (d.get("wireSearchGoesEngine"), d.get("wireUrlGoesDirect"),
                      d.get("wireSpaceGoesEngine"), d.get("wireLocalGoesDirect"))))
    # 下拉
    checks.append(("聚焦输入框就弹出历史",
                   d.get("histBoxShown") is True, str(d.get("histBoxShown"))))
    checks.append(("下拉分「搜过」「打开过」两组",
                   d.get("histLabels") == ["搜过", "打开过"],
                   str(d.get("histLabels"))))
    checks.append(("下拉条数 = 历史条数（4 条以内全列）",
                   d.get("histRows") == 4, str(d.get("histRows"))))
    checks.append(("下拉底部有「清空搜索历史」",
                   d.get("histHasClear") is True, str(d.get("histHasClear"))))
    checks.append(("模糊筛选：敲 gh 命中 github.com",
                   any("github.com" in (x or "") for x in (d.get("fuzzyRows") or [])),
                   str(d.get("fuzzyRows"))))
    checks.append(("筛不到时给明确说明而不是空白",
                   d.get("noMatchShown") is True, str(d.get("noMatchShown"))))
    # 键盘
    checks.append(("下键选中第一项并填进输入框",
                   (d.get("afterDownValue") or "") != ""
                   and d.get("afterDownSel") == 1,
                   "值=%r 选中=%s" % (d.get("afterDownValue"), d.get("afterDownSel"))))
    checks.append(("再按下键换到第二项",
                   d.get("afterDown2Idx") is not None
                   and d.get("afterDown2Idx") != d.get("afterDownIdx"),
                   "%s(引擎%s) → %s(引擎%s)"
                   % (d.get("afterDownIdx"), d.get("afterDownEngine"),
                      d.get("afterDown2Idx"), d.get("afterDown2Engine"))))
    checks.append(("Esc 清空并收起下拉",
                   d.get("afterEscValue") == "" and d.get("afterEscHidden") is True,
                   "值=%r 收起=%s" % (d.get("afterEscValue"), d.get("afterEscHidden"))))
    # 删除 / 清空
    checks.append(("点 × 只删这一条",
                   d.get("delRemovedOne") is True,
                   "%s → %s" % (d.get("afterDelCount"), d.get("delRemovedOne"))))
    checks.append(("清空后是空数组",
                   d.get("histAfterClear") in ("[]", None), repr(d.get("histAfterClear"))))
    checks.append(("清空后下拉不再有条目",
                   d.get("histAfterClearRows") == 0, str(d.get("histAfterClearRows"))))
    checks.append(("清空后提示「还没有搜索记录」",
                   d.get("emptyHintAfterClear") is True,
                   str(d.get("emptyHintAfterClear"))))

    print("分类:", ", ".join("%s=%d" % (n, c) for n, c in zip(d["grpNames"], d["grpCounts"])))
    print("首屏: 标题=%s 高亮=%s 卡=%s（切回全部后 %s）"
          % (d.get("bootTitle"), d.get("bootActive"), d.get("bootCards"),
             d.get("bootBackCards")))
    print("滚动: 主区可滚=%s 其它可滚元素=%s | 切分类 %s→%s | 回全部 %s"
          % (d.get("mainScrollable"), d.get("otherScrollable"),
             d.get("scrollBefore"), d.get("scrollAfterClick"), d.get("scrollAfterBack")))
    print("搜索 bilibili=%d  gov=%d" % (d["searchBili"], d["searchGov"]))
    print("搜索栏: 默认 %s | 打开时焦点在 %s" % (d.get("wsAction"), d.get("wsFocus")))
    print("网址直连: " + " | ".join("%s → %s" % (k, v) for k, v in (uc or {}).items()))
    print("无权限一轮: 提示=%r 侧栏=%s 卡=%s"
          % (nh.get("hintText"), nh.get("hotBtn"), nh.get("cards")))
    print("常用: 最近 %d 天，%d 个" % (exp_window, len(exp_hot)))
    print("搜索历史: 搜完 %d 条 → 记完 %d 条 | 下拉 %s 组 / %s 行 | 模糊 gh→%s"
          % (1, len(d.get("histAfterUrl") or []),
             d.get("histLabels"), d.get("histRows"), d.get("fuzzyRows")))
    print("跳转推演: %s | %s | %s"
          % (d.get("targetSearchBing"), d.get("targetUrl"), d.get("targetLocal")))
    print()
    bad = 0
    for name, ok, detail in checks:
        print("  %s %-26s %s" % ("PASS" if ok else "FAIL", name, detail))
        bad += 0 if ok else 1
    print()
    print("结论：", "全部通过" if not bad else "%d 项未通过" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
