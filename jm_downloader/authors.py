"""作者名归一化 / 去重。

禁漫的作者字段常见形态是「社团(画师)」，同一个实体有多种写法::

    DOGYEAR(九条だんぼ)  /  DOGYEAR  /  九条だんぼ

策略是以「单个作者条目」为单位做并查集：条目内部所有 token（括号内外）互相 union，
条目之间只靠共享 token 自然合并。这样上面三个会落进同一连通分量，而 ``A,B`` 这种
逗号分隔的合作作者不会被错误合并。
"""

import re
from typing import Dict, Iterable, List, Optional, Sequence

# 支持的括号对（与 utils.clean_title_for_filename 保持一致）
_PAIRS = {
    '(': ')',
    '[': ']',
    '{': '}',
    '【': '】',
    '（': '）',
    '〈': '〉',
    '《': '》',
    '〔': '〕',
}
_CLOSERS = set(_PAIRS.values())

# 作者条目之间的分隔符（逗号 / 中文逗号）
_SEP_CHARS = ',，'
_WS_RE = re.compile(r'\s+')


def _norm(s: str) -> str:
    return _WS_RE.sub(' ', s).strip()


def _is_pure_bracket_group(s: str) -> bool:
    """判断 ``s`` 是否「整体就是一个括号组」，例如 ``(九条だんぼ)``。"""
    if not s or s[0] not in _PAIRS:
        return False
    depth = 0
    for i, ch in enumerate(s):
        if ch in _PAIRS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
            if depth == 0:
                return i == len(s) - 1
    return False


def split_author_field(field: Optional[str]) -> List[str]:
    """把 author 字段按顶层逗号切分成若干作者条目（尊重括号嵌套）。

    会做一次合并修正：形如 ``DOGYEAR,(九条だんぼ)`` 的第二段其实是上一段的
    括号补充（禁漫数据里很常见），应并回上一段而不是当成独立作者；
    而 ``Alpha,Beta`` 这种两个平铺名字则保持独立，避免把合作作者合并。
    """
    if not field:
        return []
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in str(field):
        if ch in _PAIRS:
            depth += 1
            buf.append(ch)
        elif ch in _CLOSERS:
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch in _SEP_CHARS and depth == 0:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))

    merged: List[str] = []
    for p in (_norm(x) for x in parts):
        if not p:
            continue
        if merged and _is_pure_bracket_group(p):
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    return merged


def author_tokens(entry: str, _depth: int = 0) -> List[str]:
    """拆出一个作者条目里的所有 token。

    ``DOGYEAR(九条だんぼ)`` -> ``['DOGYEAR', '九条だんぼ']``
    ``DOGYEAR``            -> ``['DOGYEAR']``
    ``[ DOGYEAR (九条だんぼ)]`` -> ``['DOGYEAR', '九条だんぼ']``
    """
    if not entry:
        return []
    primary: List[str] = []
    inners: List[str] = []
    buf: List[str] = []
    closers: List[str] = []

    for ch in str(entry):
        if ch in _PAIRS:
            closers.append(_PAIRS[ch])
            if len(closers) == 1:
                buf = []
            else:
                buf.append(ch)
        elif closers and ch == closers[-1]:
            closers.pop()
            if not closers:
                inners.append(''.join(buf))
                buf = []
            else:
                buf.append(ch)
        elif closers:
            buf.append(ch)
        else:
            primary.append(ch)

    if buf and closers:
        inners.append(''.join(buf))

    tokens: List[str] = []
    prim = _norm(''.join(primary))
    if prim:
        tokens.append(prim)

    for seg in inners:
        seg = _norm(seg)
        if not seg:
            continue
        # 括号里还可能再套括号（如 [ DOGYEAR (九条だんぼ)]），递归拆开
        if _depth < 3 and any(c in _PAIRS for c in seg):
            for t in author_tokens(seg, _depth + 1):
                if t not in tokens:
                    tokens.append(t)
        elif seg not in tokens:
            tokens.append(seg)
    return tokens


class AuthorResolver:
    """基于并查集的作者别名归并器。

    先对所有作者字段调用 :meth:`add_field` 建图，再调用
    :meth:`resolve_field` / :meth:`components` 取归一化结果。
    """

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}
        self._order: Dict[str, int] = {}
        self._primary_hits: Dict[str, int] = {}
        self._total_hits: Dict[str, int] = {}
        self._canonical_cache: Dict[str, str] = {}
        self._seq = 0

    # -- 并查集 ---------------------------------------------------------
    def _touch(self, token: str) -> None:
        if token not in self._parent:
            self._parent[token] = token
            self._order[token] = self._seq
            self._primary_hits[token] = 0
            self._total_hits[token] = 0
            self._seq += 1

    def _find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra == rb:
            return
        # 让「先出现」的节点当根，保证结果稳定可复现
        if self._order[ra] <= self._order[rb]:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb
        self._canonical_cache.clear()

    # -- 建图 -----------------------------------------------------------
    def add_entry(self, entry: str) -> None:
        tokens = author_tokens(entry)
        if not tokens:
            return
        for t in tokens:
            self._touch(t)
            self._total_hits[t] += 1
        self._primary_hits[tokens[0]] += 1
        for t in tokens[1:]:
            self._union(tokens[0], t)
        # 命中次数变了，规范名的排序键就变，缓存必须失效（union 里那次覆盖不到这里）
        self._canonical_cache.clear()

    def add_field(self, field: Optional[str]) -> None:
        for entry in split_author_field(field):
            self.add_entry(entry)

    # -- 查询 -----------------------------------------------------------
    def canonical_name(self, root: str) -> str:
        cached = self._canonical_cache.get(root)
        if cached:
            return cached
        members = [t for t in self._parent if self._find(t) == root]
        if not members:
            return root
        # 优先选「作为主名出现过最多次」的 token，其次总出现次数，最后取最早出现的
        best = max(members, key=lambda t: (
            self._primary_hits[t], self._total_hits[t], -self._order[t]))
        self._canonical_cache[root] = best
        return best

    def aliases_of(self, root: str) -> List[str]:
        members = [t for t in self._parent if self._find(t) == root]
        canonical = self.canonical_name(root)
        members.sort(key=lambda t: (t != canonical, self._order[t]))
        return members

    def resolve_entry(self, entry: str) -> Optional[str]:
        tokens = [t for t in author_tokens(entry) if t in self._parent]
        if not tokens:
            return None
        root = min((self._find(t) for t in tokens), key=lambda r: self._order[r])
        return self.canonical_name(root)

    def resolve_field(self, field: Optional[str]) -> List[str]:
        """返回该字段涉及的所有规范作者名（去重、保持顺序）。"""
        out: List[str] = []
        for entry in split_author_field(field):
            name = self.resolve_entry(entry)
            if name and name not in out:
                out.append(name)
        return out

    def components(self) -> Dict[str, List[str]]:
        """规范作者名 -> 别名列表。"""
        roots: Dict[str, List[str]] = {}
        for token in self._parent:
            roots.setdefault(self._find(token), []).append(token)
        return {self.canonical_name(r): self.aliases_of(r) for r in roots}

    def __len__(self) -> int:
        return len({self._find(t) for t in self._parent})


def build_resolver(fields: Iterable[Optional[str]]) -> AuthorResolver:
    resolver = AuthorResolver()
    for f in fields:
        resolver.add_field(f)
    return resolver
