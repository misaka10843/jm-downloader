"""生成自包含的 HTML 选择页（作者监听 / 搜索结果）。

静态页面：数据内嵌、CSS/JS 全内联，双击可开，不受 file:// 的 CORS 限制；封面直接
引用禁漫 CDN。页面无法写库或调 API，所以选择结果靠「导出 JSON」再由 CLI 的
``--from-file`` 消费。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
<header class="bar">
  <div class="left">
    <h1>__H1__</h1>
    <div class="meta">__META__</div>
  </div>
  <div class="right">
__TOOLBAR__
  </div>
</header>
<div class="hint">__HINT__</div>
<main>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty" hidden>没有匹配的内容</div>
</main>
<div id="toast" class="toast" hidden></div>
<div id="drawer" class="drawer" hidden>
  <div class="drawer-head">
    <strong>选择结果 JSON</strong>
    <button class="btn ghost" onclick="closeDrawer()">关闭</button>
  </div>
  <textarea id="jsonout" readonly spellcheck="false"></textarea>
</div>
<script type="application/json" id="page-data">__DATA__</script>
<script>
__JS__
</script>
</body>
</html>
"""

_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --line:#e3e6ea; --text:#1f2328; --muted:#6b7280;
  --accent:#2563eb; --accent-soft:#e8effd; --ok:#0f766e; --ok-soft:#e6f4f1;
  --warn:#b45309; --warn-soft:#fdf3e3; --shadow:0 1px 2px rgba(16,24,40,.06),0 4px 12px rgba(16,24,40,.05);
}
*{box-sizing:border-box}
/* 必须显式声明：`.drawer` 设了 display:flex，作者样式表优先级高于 UA 的
   [hidden]{display:none}，不加这条抽屉会在页面加载时就直接展开。 */
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Roboto,"Helvetica Neue",Arial,sans-serif}
.bar{position:sticky;top:0;z-index:20;display:flex;gap:16px;align-items:flex-start;
  justify-content:space-between;flex-wrap:wrap;padding:14px 20px;background:var(--panel);
  border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
.left{flex:1 1 auto;min-width:0}
.bar h1{margin:0 0 4px;font-size:17px;font-weight:650}
.meta{color:var(--muted);font-size:12.5px}
.meta b{color:var(--text);font-weight:600}
.right{display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:8px;
  padding:7px 12px;font-size:13px;cursor:pointer;transition:.15s;white-space:nowrap}
.btn:hover{border-color:#c9cfd6;background:#fafbfc}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:#1d4ed8}
.btn.ghost{background:transparent}
.btn.sm{padding:5px 9px;font-size:12px}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11.5px;
  background:#eef1f4;color:var(--muted);white-space:nowrap}
.badge.ok{background:var(--ok-soft);color:var(--ok)}
.badge.warn{background:var(--warn-soft);color:var(--warn)}
.badge.pages{background:var(--accent-soft);color:var(--accent);font-weight:650}
input[type=search],select{border:1px solid var(--line);background:var(--panel);border-radius:8px;
  padding:7px 10px;font-size:13px;color:var(--text);min-width:150px}
label.chk{display:inline-flex;gap:6px;align-items:center;font-size:13px;color:var(--muted);cursor:pointer}
.hint{padding:10px 20px 0;color:var(--muted);font-size:12.5px}
.hint code{background:#eef1f4;padding:1px 6px;border-radius:5px;color:#374151;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
main{padding:14px 20px 40px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;
  box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.card-top{display:flex;gap:10px;align-items:flex-start}
.card-top input[type=checkbox]{width:17px;height:17px;margin-top:2px;flex:0 0 auto;cursor:pointer;accent-color:var(--accent)}
.title{font-weight:600;font-size:14px;line-height:1.4;word-break:break-word}
.title a{color:inherit;text-decoration:none}
.title a:hover{color:var(--accent);text-decoration:underline}
.sub{color:var(--muted);font-size:12px;margin-top:2px;word-break:break-word}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.strips{display:flex;gap:7px;overflow-x:auto;padding-bottom:3px}
.strip{position:relative;flex:0 0 auto;width:74px;height:99px;border-radius:7px;overflow:hidden;
  background:#eef1f4;border:1px solid var(--line)}
.strip img{width:100%;height:100%;object-fit:cover;display:block}
.strip .pg{position:absolute;right:3px;bottom:3px;background:rgba(17,24,39,.82);color:#fff;
  font-size:10.5px;padding:1px 5px;border-radius:5px;font-weight:600}
.strip .fb{display:flex;align-items:center;justify-content:center;height:100%;color:#9aa3ad;font-size:11px}
.cover{flex:0 0 auto;width:96px;height:128px;border-radius:8px;overflow:hidden;background:#eef1f4;
  border:1px solid var(--line);position:relative}
.cover img{width:100%;height:100%;object-fit:cover;display:block}
.cover .fb{display:flex;align-items:center;justify-content:center;height:100%;color:#9aa3ad;font-size:11px}
.more{flex:0 0 auto;width:52px;height:99px;border-radius:7px;border:1px dashed var(--line);
  display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px}
.empty{padding:60px 20px;text-align:center;color:var(--muted)}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#111827;color:#fff;
  padding:9px 16px;border-radius:9px;font-size:13px;z-index:60;box-shadow:var(--shadow)}
.drawer{position:fixed;inset:auto 0 0 0;max-height:62vh;background:var(--panel);
  border-top:1px solid var(--line);z-index:50;display:flex;flex-direction:column;box-shadow:0 -6px 20px rgba(16,24,40,.12)}
.drawer-head{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;
  border-bottom:1px solid var(--line)}
.drawer textarea{flex:1;min-height:220px;border:0;outline:0;resize:none;padding:12px 16px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;background:#fbfcfd;color:#1f2328}
"""

_JS = r"""
const DATA = JSON.parse(document.getElementById('page-data').textContent);
const MODE = DATA.mode;                 // 'authors' | 'albums'
const ITEMS = DATA.items || [];
const state = { selected: new Set(), q: '', only: 'all', sort: 'default' };

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2200);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function keyOf(it) { return MODE === 'authors' ? it.canonical : String(it.album_id); }

function imgTag(url, cls) {
  if (!url) return '<div class="fb">无封面</div>';
  const fallback = "this.style.display='none';this.parentNode.innerHTML='<div class=\\'fb\\'>加载失败</div>'";
  return '<img class="' + cls + '" loading="lazy" referrerpolicy="no-referrer" src="' + esc(url) +
         '" onerror="' + fallback + '">';
}

function pagesBadge(n) {
  return n ? '<span class="badge pages">' + n + ' 页</span>' : '<span class="badge">页数未知</span>';
}

/* ---------------- 卡片渲染 ---------------- */
function authorCard(a) {
  const works = a.works || [];
  let strips = works.slice(0, 6).map((w) =>
    '<div class="strip" title="' + esc(w.title) + '">' + imgTag(w.cover_url, '') +
    (w.page_count ? '<span class="pg">' + w.page_count + 'p</span>' : '') + '</div>').join('');
  if (works.length > 6) strips += '<div class="more">+' + (works.length - 6) + '</div>';
  const chips = (a.aliases || []).map((x) => '<span class="badge">' + esc(x) + '</span>').join('');
  return '<div class="card">' +
    '<div class="card-top">' +
      '<input type="checkbox" data-key="' + esc(a.canonical) + '"' + (state.selected.has(a.canonical) ? ' checked' : '') + '>' +
      '<div style="flex:1;min-width:0">' +
        '<div class="title">' + esc(a.display_name || a.canonical) + '</div>' +
        '<div class="sub">作品 <b>' + works.length + '</b> 部' +
          (a.watched ? ' · <span class="badge ok">已监听</span>' : '') + '</div>' +
      '</div>' +
    '</div>' +
    (chips ? '<div class="chips">' + chips + '</div>' : '') +
    (strips ? '<div class="strips">' + strips + '</div>' : '<div class="sub">数据库里还没有该作者的作品封面</div>') +
  '</div>';
}

function albumCard(it) {
  const chips = (it.tags || '').split(',').filter(Boolean).slice(0, 5)
    .map((t) => '<span class="badge">' + esc(t) + '</span>').join('');
  const fav = it.is_favorite ? '<span class="badge warn">已收藏</span>' : '';
  return '<div class="card">' +
    '<div class="card-top">' +
      '<input type="checkbox" data-key="' + esc(it.album_id) + '"' + (state.selected.has(String(it.album_id)) ? ' checked' : '') + '>' +
      '<div class="cover">' + imgTag(it.cover_url, '') + '</div>' +
      '<div style="flex:1;min-width:0">' +
        '<div class="title"><a href="' + esc(it.url) + '" target="_blank" rel="noreferrer">' +
          esc(it.title || it.album_id) + '</a></div>' +
        '<div class="sub">ID <b>' + esc(it.album_id) + '</b> · ' + pagesBadge(it.page_count) + ' ' + fav + '</div>' +
        '<div class="sub">' + esc(it.authors || '作者未知') + '</div>' +
        (chips ? '<div class="chips" style="margin-top:6px">' + chips + '</div>' : '') +
      '</div>' +
    '</div>' +
  '</div>';
}

/* ---------------- 过滤 / 排序 ---------------- */
function visible() {
  let list = ITEMS.slice();
  const q = state.q.trim().toLowerCase();
  if (q) {
    list = list.filter((it) => {
      const hay = MODE === 'authors'
        ? (it.canonical + ' ' + (it.aliases || []).join(' '))
        : (it.title + ' ' + it.album_id + ' ' + (it.authors || '') + ' ' + (it.tags || ''));
      return hay.toLowerCase().includes(q);
    });
  }
  if (MODE === 'authors' && state.only === 'watched') list = list.filter((it) => it.watched);
  if (MODE === 'authors' && state.only === 'unwatched') list = list.filter((it) => !it.watched);
  if (MODE === 'albums' && state.only === 'unfav') list = list.filter((it) => !it.is_favorite);

  if (state.sort === 'pages_desc') list.sort((a, b) => (b.page_count || 0) - (a.page_count || 0));
  if (state.sort === 'pages_asc') list.sort((a, b) => (a.page_count || 0) - (b.page_count || 0));
  if (state.sort === 'title') list.sort((a, b) => String(a.title || a.canonical).localeCompare(String(b.title || b.canonical)));
  return list;
}

function render() {
  const list = visible();
  $('grid').innerHTML = list.map(MODE === 'authors' ? authorCard : albumCard).join('');
  $('empty').hidden = list.length > 0;
  $('grid').querySelectorAll('input[type=checkbox][data-key]').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) state.selected.add(cb.dataset.key); else state.selected.delete(cb.dataset.key);
      cb.closest('.card').classList.toggle('sel', cb.checked);
      updateCount();
    });
  });
  updateCount();
}

function updateCount() {
  $('selcount').textContent = state.selected.size;
}

/* ---------------- 选择操作 ---------------- */
function selectAll(on) {
  state.selected.clear();
  if (on) visible().forEach((it) => state.selected.add(keyOf(it)));
  render();
}
function invert() {
  const next = new Set();
  visible().forEach((it) => { const k = keyOf(it); if (!state.selected.has(k)) next.add(k); });
  state.selected = next;
  render();
}
function clearAll() { state.selected.clear(); render(); }

/* ---------------- 导出 / 命令 ---------------- */
function payload() {
  const picked = ITEMS.filter((it) => state.selected.has(keyOf(it)));
  const base = { generated_at: new Date().toISOString(), keyword: DATA.keyword || null };
  if (MODE === 'authors') {
    return Object.assign(base, {
      type: 'authors', albums: [],
      authors: picked.map((a) => ({ canonical: a.canonical, display_name: a.display_name || a.canonical, aliases: a.aliases || [] })),
    });
  }
  return Object.assign(base, {
    type: 'albums', authors: [],
    albums: picked.map((a) => ({
      album_id: String(a.album_id), title: a.title, cover_url: a.cover_url,
      page_count: a.page_count, authors: a.authors, tags: a.tags, url: a.url,
    })),
  });
}

function download(name, text) {
  try {
    const blob = new Blob([text], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 0);
    return true;
  } catch (e) { return false; }
}

function exportSelection() {
  if (!state.selected.size) { toast('还没有选中任何条目'); return; }
  const text = JSON.stringify(payload(), null, 2);
  const name = MODE === 'authors' ? 'watchlist.json' : 'selection.json';
  const ok = download(name, text);
  $('jsonout').value = text;
  $('drawer').hidden = false;
  toast(ok ? ('已导出 ' + name + '（共 ' + state.selected.size + ' 项）') : '浏览器阻止了下载，已在下方面板显示 JSON');
}

function copyText(text, okMsg) {
  const done = () => toast(okMsg);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else { fallbackCopy(text, done); }
}
function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('复制失败，请手动复制'); }
  ta.remove();
}

function copyCommand(tpl) {
  const name = MODE === 'authors' ? 'watchlist.json' : 'selection.json';
  if (!state.selected.size) { toast('还没有选中任何条目'); return; }
  copyText(tpl.replace('__FILE__', name), '命令已复制（记得先导出 ' + name + '）');
}

function closeDrawer() { $('drawer').hidden = true; }

/* ---------------- 初始化 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
  const q = $('q'), only = $('only'), sort = $('sort');
  if (q) q.addEventListener('input', () => { state.q = q.value; render(); });
  if (only) only.addEventListener('change', () => { state.only = only.value; render(); });
  if (sort) sort.addEventListener('change', () => { state.sort = sort.value; render(); });
  ITEMS.forEach((it) => { if (MODE === 'authors' && it.watched) state.selected.add(it.canonical); });
  render();
});
"""

_TOOLBAR_TEMPLATE = """    <input type="search" id="q" placeholder="过滤…">
    __ONLY__
    __SORT__
    <span class="badge">已选 <b id="selcount">0</b></span>
    <button class="btn sm" onclick="selectAll(true)">全选</button>
    <button class="btn sm" onclick="invert()">反选</button>
    <button class="btn sm" onclick="clearAll()">清空</button>
    <button class="btn primary" onclick="exportSelection()">导出选择</button>
__EXTRA_BUTTONS__"""


def _embed_json(obj: Any) -> str:
    """把数据内嵌进 ``<script type="application/json">``，转义所有 ``<``。

    只转义 ``</`` 是不够的。``<script>`` 内部一旦出现 ``<!--``，HTML 解析器会进入
    "script data escaped" 状态；若后面再跟一个 ``<script>``，就进入
    "script data **double** escaped" 状态 —— 在那种状态下 ``</script>`` **不再闭合元素**，
    数据块会把后面的整个文档一起吞掉：JSON.parse 报
    "Unexpected non-whitespace character after JSON"，页面 0 张卡片、脚本完全不初始化。
    （实测：一个含 ``<!--<script>`` 的作品标题就能把整页打瘫。）

    把 ``<`` 全部写成 ``\\u003c`` 是标准做法：JSON 语义完全不变，
    而 HTML 解析器在原始文本里再也看不到 ``<``，上述状态机无从触发。
    """
    return json.dumps(obj, ensure_ascii=False).replace('<', '\\u003c')


_PAGE_PLACEHOLDER_RE = re.compile('|'.join(re.escape(k) for k in (
    '__CSS__', '__TITLE__', '__H1__', '__META__', '__HINT__', '__TOOLBAR__',
    '__DATA__', '__JS__')))


def _render(out_path: Path, *, title: str, h1: str, meta_html: str, hint_html: str,
            toolbar: str, data: Dict[str, Any]) -> Path:
    values = {
        '__CSS__': _CSS,
        '__TITLE__': _escape(title),
        '__H1__': _escape(h1),
        '__META__': meta_html,
        '__HINT__': hint_html,
        '__TOOLBAR__': toolbar,
        '__DATA__': _embed_json(data),
        '__JS__': _JS,
    }
    # 必须一次替换完：串行 replace 会重新扫描已插入的内容，标题里只要出现 __JS__
    # 就会被换成整段脚本，数据块解析失败、页面 0 张卡片。
    html = _PAGE_PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], _PAGE_TEMPLATE)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding='utf-8')
    return out_path


def _escape(s: Any) -> str:
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def render_author_page(authors: List[Dict[str, Any]], out_path: Path,
                       meta: Optional[Dict[str, Any]] = None) -> Path:
    """作者监听页：展示规范作者 + 别名 + 其作品封面，供勾选要监听的作者。"""
    meta = meta or {}
    watched = sum(1 for a in authors if a.get('watched'))
    works = sum(len(a.get('works') or []) for a in authors)

    meta_html = (
        f'共 <b>{len(authors)}</b> 位作者 · <b>{works}</b> 部作品 · 已监听 <b>{watched}</b> 位'
        f' · 数据库 <b>{_escape(meta.get("db", "-"))}</b>'
        f' · 生成于 {_escape(datetime.now().strftime("%Y-%m-%d %H:%M"))}'
    )
    hint_html = (
        '勾选要持续关注更新的作者 → 点「导出选择」得到 <code>watchlist.json</code> → '
        '执行 <code>python cli.py watch --from-file watchlist.json</code> 保存监听列表 → '
        '之后 <code>python cli.py check-update</code> 即可只检查这些作者（每人最多前 10 条搜索结果）。'
    )
    extra = ('    <button class="btn sm" onclick="copyCommand(\'python cli.py watch --from-file __FILE__\')">复制监听命令</button>\n'
             '    <button class="btn sm" onclick="copyText(\'python cli.py check-update\', \'命令已复制\')">复制检查命令</button>')
    toolbar = (_TOOLBAR_TEMPLATE
               .replace('__ONLY__',
                        '    <select id="only">'
                        '<option value="all">全部作者</option>'
                        '<option value="unwatched">只看未监听</option>'
                        '<option value="watched">只看已监听</option>'
                        '</select>')
               .replace('__SORT__', '')
               .replace('__EXTRA_BUTTONS__', extra))

    return _render(out_path, title='JM 作者监听选择', h1='选择要监听更新的作者',
                   meta_html=meta_html, hint_html=hint_html, toolbar=toolbar,
                   data={'mode': 'authors', 'items': authors, 'keyword': None})


def render_search_page(items: List[Dict[str, Any]], out_path: Path, *, keyword: str = '',
                       kind: str = 'site', meta: Optional[Dict[str, Any]] = None) -> Path:
    """搜索结果页：展示 id / 封面 / 页数 / 作者 / 标签，供勾选后下载或加入收藏。"""
    meta = meta or {}
    pages = sum(int(i.get('page_count') or 0) for i in items)

    meta_html = (
        f'关键词 <b>{_escape(keyword)}</b>（{_escape(kind)}） · 共 <b>{len(items)}</b> 条'
        f' · 合计 <b>{pages}</b> 页 · 生成于 {_escape(datetime.now().strftime("%Y-%m-%d %H:%M"))}'
    )
    hint_html = (
        '勾选要处理的作品 → 点「导出选择」得到 <code>selection.json</code> → '
        '下载执行 <code>python cli.py download --from-file selection.json</code>，'
        '加入收藏执行 <code>python cli.py favorite --from-file selection.json</code>。'
    )
    extra = ('    <button class="btn sm" onclick="copyCommand(\'python cli.py download --from-file __FILE__\')">复制下载命令</button>\n'
             '    <button class="btn sm" onclick="copyCommand(\'python cli.py favorite --from-file __FILE__\')">复制收藏命令</button>')
    toolbar = (_TOOLBAR_TEMPLATE
               .replace('__ONLY__',
                        '    <select id="only">'
                        '<option value="all">全部结果</option>'
                        '<option value="unfav">只看未收藏</option>'
                        '</select>')
               .replace('__SORT__',
                        '    <select id="sort">'
                        '<option value="default">默认顺序</option>'
                        '<option value="pages_desc">页数从多到少</option>'
                        '<option value="pages_asc">页数从少到多</option>'
                        '<option value="title">按标题</option>'
                        '</select>')
               .replace('__EXTRA_BUTTONS__', extra))

    return _render(out_path, title=f'JM 搜索结果 - {keyword}', h1=f'搜索结果：{keyword}',
                   meta_html=meta_html, hint_html=hint_html, toolbar=toolbar,
                   data={'mode': 'albums', 'items': items, 'keyword': keyword})
