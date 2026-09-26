"""引用影响分析：跨语言 ICU 引用依赖图。

边的语义
--------
语言 ``loc`` 的消息 ``msg`` 中出现 ``{@ref_key}`` 时，沿 **显式父语言回退链**
解析到第一个拥有 ``ref_key`` 的语言 ``loc'``，得到一条有向边：

    (loc, msg) ──@ref_key──▶ (loc', ref_key)

即「源消息依赖目标消息」。沿父链都找不到时，边为 **断裂边**（``dst=None``，
携带尝试过的回退链）。

因此删除 / 改写某条资源 (locale, key) 的传递影响范围 = 反向可达集合：
距离 1 为直接依赖，距离 ≥2 为间接依赖；BFS 父指针同时给出最短证据路径。

确定性约定
----------
- 节点按 (locale, key) 排序，所有邻居遍历均有序；
- BFS / Kosaraju 的输出只依赖图本身，不依赖 dict 迭代顺序；
- 受影响节点按 (distance, locale, key) 稳定排序；
- 强连通分量按成员元组排序，分量内部成员排序，自环也算循环。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from .analyzer import AnalysisResult, FallbackGraph

# 节点：(语言, 消息键)
Node = tuple[str, str]


@dataclass
class RefEdge:
    eid: int
    src: Node
    dst: Optional[Node]       # None 表示沿父链无法解析（断裂引用）
    ref_key: str
    pos: int                  # 首次出现位置
    positions: list[int]      # 同一消息内全部出现位置
    chain: list[str]          # 解析时走过的回退链（含本语言）


@dataclass
class RefGraph:
    nodes: list[Node]
    index: dict[Node, int]
    edges: list[RefEdge]
    out: dict[Node, list[RefEdge]]    # 本节点发出的引用
    into: dict[Node, list[RefEdge]]   # 指向本节点的引用


# ---------------------------------------------------------------- 建图

def resolution_chain(graph: FallbackGraph, locale: str) -> list[str]:
    """回退链；循环时截断到循环重复节点（与 analyzer._resolve_ref 一致）。"""
    chain, reason = graph.chain(locale)
    if reason and reason.startswith("cycle:"):
        rep = reason.split(":", 1)[1]
        chain = chain[: chain.index(rep) + 1]
    return chain


def build_ref_graph(analysis: AnalysisResult) -> RefGraph:
    """从审查结果构建跨语言引用依赖图。"""
    bundles = analysis.bundles
    graph = analysis.graph

    nodes: list[Node] = sorted(
        (loc, key)
        for loc, b in bundles.items()
        for key in b.entries)
    index = {n: i for i, n in enumerate(nodes)}
    out: dict[Node, list[RefEdge]] = {n: [] for n in nodes}
    into: dict[Node, list[RefEdge]] = {n: [] for n in nodes}
    edges: list[RefEdge] = []

    for loc in sorted(bundles):
        b = bundles[loc]
        chain = resolution_chain(graph, loc)
        for src_key in sorted(b.entries):
            shape = b.shapes.get(src_key)
            if shape is None:
                continue  # ICU 语法错误的消息无法可靠提取引用
            occurrences: dict[str, list[int]] = {}
            for v, _path in shape.walk_vars():
                if v.is_ref:
                    occurrences.setdefault(v.name, []).append(v.pos)
            for ref_key in sorted(occurrences):
                dst: Optional[Node] = next(
                    ((anc, ref_key) for anc in chain
                     if (anc, ref_key) in index), None)
                e = RefEdge(
                    eid=0, src=(loc, src_key), dst=dst, ref_key=ref_key,
                    pos=min(occurrences[ref_key]),
                    positions=sorted(occurrences[ref_key]),
                    chain=list(chain))
                edges.append(e)
                out[e.src].append(e)
                if dst is not None:
                    into[dst].append(e)

    # 确定性排序后重新编号
    edges.sort(key=lambda e: (e.src[0], e.src[1], e.ref_key, e.pos))
    for i, e in enumerate(edges):
        e.eid = i
    for n in nodes:
        out[n].sort(key=lambda e: (
            e.ref_key, e.pos,
            e.dst if e.dst is not None else ("￿", "￿")))
        into[n].sort(key=lambda e: (e.src[0], e.src[1], e.ref_key, e.pos))
    return RefGraph(nodes=nodes, index=index, edges=edges, out=out, into=into)


# ---------------------------------------------------------------- 强连通分量

def find_sccs(g: RefGraph) -> list[list[Node]]:
    """Kosaraju 求强连通分量，只返回循环分量（成员 >1 或自环）。确定性排序。"""
    fwd: dict[Node, list[Node]] = {
        n: sorted({e.dst for e in g.out[n] if e.dst is not None})
        for n in g.nodes}
    rev: dict[Node, list[Node]] = {n: [] for n in g.nodes}
    for u in g.nodes:
        for v in fwd[u]:
            rev[v].append(u)
    for n in g.nodes:
        rev[n].sort()

    # 第一遍：迭代 DFS 求完成序（避免深链超过递归深度限制）
    visited: set[Node] = set()
    order: list[Node] = []
    for start in g.nodes:  # g.nodes 已排序
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[Node, int]] = [(start, 0)]
        while stack:
            u, i = stack[-1]
            nbrs = fwd[u]
            if i < len(nbrs):
                v = nbrs[i]
                stack[-1] = (u, i + 1)
                if v not in visited:
                    visited.add(v)
                    stack.append((v, 0))
            else:
                order.append(u)
                stack.pop()

    # 第二遍：逆序在反向图上取分量
    seen: set[Node] = set()
    sccs: list[list[Node]] = []
    for start in reversed(order):
        if start in seen:
            continue
        comp: list[Node] = []
        seen.add(start)
        stack2 = [start]
        while stack2:
            u = stack2.pop()
            comp.append(u)
            for v in rev[u]:
                if v not in seen:
                    seen.add(v)
                    stack2.append(v)
        sccs.append(sorted(comp))
    sccs.sort()

    cyclic: list[list[Node]] = []
    for comp in sccs:
        if len(comp) > 1:
            cyclic.append(comp)
        else:
            n = comp[0]
            if any(e.dst == n for e in g.out[n]):  # 自环
                cyclic.append(comp)
    return cyclic


# ---------------------------------------------------------------- 传递影响

def reverse_bfs(g: RefGraph, target: Node) -> tuple[dict[Node, int],
                                                    dict[Node, RefEdge]]:
    """从 target 沿反向边 BFS。

    返回 (距离表, 父边表)。邻居有序 + BFS 层级展开，使等长最短路径中选中的
    证据路径唯一确定（先被发现的上游优先）。
    """
    dist: dict[Node, int] = {target: 0}
    parent: dict[Node, RefEdge] = {}
    q: deque[Node] = deque([target])
    while q:
        u = q.popleft()
        for e in g.into[u]:
            s = e.src
            if s not in dist:
                dist[s] = dist[u] + 1
                parent[s] = e
                q.append(s)
    return dist, parent


def evidence_path(parent: dict[Node, RefEdge], node: Node,
                  target: Node) -> tuple[list[Node], list[RefEdge]]:
    """重建 node ──▶ target 的最短证据路径（节点与边等长差一）。"""
    path_nodes: list[Node] = [node]
    path_edges: list[RefEdge] = []
    cur = node
    while cur != target:
        e = parent[cur]
        path_edges.append(e)
        cur = e.dst  # type: ignore[assignment]
        path_nodes.append(cur)
    return path_nodes, path_edges


def forward_depths(g: RefGraph, target: Node) -> dict[Node, int]:
    """target 沿正向边到其（传递）依赖的距离，用于无关节点布局分级。"""
    dist = {target: 0}
    q: deque[Node] = deque([target])
    while q:
        u = q.popleft()
        for e in g.out[u]:
            if e.dst is not None and e.dst not in dist:
                dist[e.dst] = dist[u] + 1
                q.append(e.dst)
    return dist


# ---------------------------------------------------------------- 序列化

def _node_payload(node_id: int, node: Node, target: Node,
                  dist: Optional[int]) -> dict:
    if node == target:
        kind, distance = "target", 0
    elif dist is not None:
        kind = "direct" if dist == 1 else "indirect"
        distance = dist
    else:
        kind, distance = "other", None
    return {"id": node_id, "locale": node[0], "key": node[1],
            "kind": kind, "distance": distance}


def _edge_payload(e: RefEdge, id_of: dict[Node, int]) -> dict:
    return {
        "id": e.eid,
        "from": id_of[e.src],
        "to": id_of[e.dst] if e.dst is not None else None,
        "ref_key": e.ref_key,
        "pos": e.pos,
        "positions": e.positions,
        "chain": e.chain,
        "broken": e.dst is None,
    }


# ---------------------------------------------------------------- 完整分析

def analyze_impact(analysis: AnalysisResult, locale: str, key: str) -> dict:
    """对 (locale, key) 计算删除/改写时的传递影响。目标不存在抛 KeyError。"""
    g = build_ref_graph(analysis)
    target: Node = (locale, key)
    if target not in g.index:
        raise KeyError(target)

    dist, parent = reverse_bfs(g, target)
    deps: dict[Node, int] = forward_depths(g, target)
    sccs = find_sccs(g)
    target_scc = next((i for i, comp in enumerate(sccs) if target in comp), -1)

    impacted_nodes = sorted(
        (n for n in g.nodes if n in dist and n != target),
        key=lambda n: (dist[n], n[0], n[1]))
    affected_locales = sorted({n[0] for n in impacted_nodes})

    entries = analysis.bundles
    impacted: list[dict] = []
    for n in impacted_nodes:
        path_nodes, path_edges = evidence_path(parent, n, target)
        impacted.append({
            "locale": n[0],
            "key": n[1],
            "distance": dist[n],
            "direct": dist[n] == 1,
            "value": entries[n[0]].entries[n[1]],
            "path": [{"locale": loc, "key": k} for loc, k in path_nodes],
            "path_refs": [e.ref_key for e in path_edges],
        })

    id_of = g.index
    graph_payload = {
        "nodes": [_node_payload(i, n, target, dist.get(n))
                  for i, n in enumerate(g.nodes)],
        "edges": [_edge_payload(e, id_of) for e in g.edges],
        "dep_distances": {str(id_of[n]): d for n, d in deps.items()
                          if n != target},
    }

    broken = sorted(
        (e for e in g.edges if e.dst is None),
        key=lambda e: (e.src[0], e.src[1], e.ref_key, e.pos))
    broken_payload = [{
        "locale": e.src[0], "key": e.src[1], "ref_key": e.ref_key,
        "pos": e.pos, "positions": e.positions, "chain": e.chain,
    } for e in broken]

    direct = [n for n in impacted_nodes if dist[n] == 1]
    indirect = [n for n in impacted_nodes if dist[n] >= 2]
    return {
        "base_locale": analysis.base_locale,
        "target": {
            "locale": locale, "key": key,
            "value": entries[locale].entries[key],
        },
        "summary": {
            "direct": len(direct),
            "indirect": len(indirect),
            "affected_locale_count": len(affected_locales),
            "scc_count": len(sccs),
            "in_cycle": target_scc >= 0,
            "total_nodes": len(g.nodes),
            "total_edges": len(g.edges),
            "broken_refs": len(broken_payload),
        },
        "impacted": impacted,
        "affected_locales": affected_locales,
        "sccs": [[{"locale": loc, "key": k} for loc, k in comp]
                 for comp in sccs],
        "target_scc_index": target_scc,
        "graph": graph_payload,
        "broken_refs": broken_payload,
    }


# ---------------------------------------------------------------- 预演对比

def _edge_maps(g: RefGraph) -> tuple[dict, dict]:
    """按 (源语言, 源键, 引用键) 建立 已解析边 / 断裂边 索引。"""
    resolved: dict[tuple[str, str, str], RefEdge] = {}
    broken: dict[tuple[str, str, str], RefEdge] = {}
    for e in g.edges:
        k = (e.src[0], e.src[1], e.ref_key)
        (broken if e.dst is None else resolved)[k] = e
    return resolved, broken


def _node_dict(n: Node) -> dict:
    return {"locale": n[0], "key": n[1]}


def compare_graphs(before: RefGraph, after: RefGraph,
                   deleted: set[Node]) -> dict:
    """对比修复前后的引用图，分类断裂引用。

    每条引用边有两个正交状态：**是否存在**（源消息是否仍携带该 ``{@ref_key}``，
    由消息文本决定）与 **存在时是否可解析**（``dst`` 是否为空）。分类必须同时
    考察两者——仅看解析状态会把「改写新引入的断裂引用」误判为原有问题，也会在
    「改写移除了原有断裂引用」时读到不存在的后图边。

    - new_broken：   修复后新出现的断裂边：修复前可解析、修复后断裂
                     （cause=target_deleted/chain_changed），或改写新引入的
                     ``{@ref_key}`` 沿父链本就解析不到（cause=ref_added）；
    - preexisting：  修复前后均携带且均断裂（原有问题，与本次修复无关）；
    - resolved：     修复前断裂、修复后仍携带且可解析（修复集顺带修好的）；
    - removed：      修复前断裂、修复后源消息仍在但不再携带该引用
                     （改写把断裂引用一并移除，引用数随之减少）；
    - retargeted：   修复前后都可解析，但落点变化（删除后改由更高祖先承接等）；
    - source_removed：引用所在的源消息被删除，引用随之消失（非断裂问题）。
    """
    rb, bb = _edge_maps(before)
    ra, ba = _edge_maps(after)
    new_broken: list[dict] = []
    preexisting: list[dict] = []
    resolved: list[dict] = []
    removed: list[dict] = []
    retargeted: list[dict] = []
    source_removed: list[dict] = []
    after_nodes = set(after.nodes)

    keys = sorted(set(rb) | set(bb) | set(ra) | set(ba))
    for k in keys:
        loc, src, ref_key = k
        e0 = rb.get(k) or bb.get(k)   # 修复前的边；None 表示当时不携带该引用
        e1 = ra.get(k) or ba.get(k)   # 修复后的边；None 表示改写后不再携带
        # 源消息在修复后已不存在：其引用一并消失，不是任何人的断裂引用
        if (loc, src) not in after_nodes:
            source_removed.append({
                "locale": loc, "key": src, "ref_key": ref_key,
                "pos": e0.pos if e0 else 0,
                "was_broken": e0 is not None and e0.dst is None,
            })
            continue
        if e1 is None:
            # 改写后源消息不再携带该引用：只有原本断裂才值得报告
            # （健康引用被改写移除不是断裂问题）
            if e0 is not None and e0.dst is None:
                removed.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e0.pos, "chain": e0.chain,
                })
            continue
        if e1.dst is None:
            if e0 is None:
                # 改写新引入的引用，沿父链本就解析不到：新增断裂
                new_broken.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e1.pos, "chain": e1.chain,
                    "previous_target": None,
                    "cause": "ref_added",
                })
            elif e0.dst is None:
                preexisting.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e1.pos, "chain": e1.chain,
                })
            else:
                new_broken.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e1.pos, "chain": e1.chain,
                    "previous_target": _node_dict(e0.dst),
                    "cause": ("target_deleted" if e0.dst in deleted
                              else "chain_changed"),
                })
        else:
            if e0 is None:
                continue  # 改写新引入的健康引用，非断裂问题
            if e0.dst is None:
                resolved.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e1.pos, "resolved_to": _node_dict(e1.dst),
                })
            elif e0.dst != e1.dst:
                retargeted.append({
                    "locale": loc, "key": src, "ref_key": ref_key,
                    "pos": e1.pos,
                    "from": _node_dict(e0.dst), "to": _node_dict(e1.dst),
                })

    return {
        "new_broken": new_broken,
        "preexisting": preexisting,
        "resolved": resolved,
        "removed": removed,
        "retargeted": retargeted,
        "source_removed": source_removed,
        "summary": {
            "new_broken": len(new_broken),
            "preexisting": len(preexisting),
            "resolved": len(resolved),
            "removed": len(removed),
            "retargeted": len(retargeted),
            "source_removed": len(source_removed),
            "broken_before": len(bb),
            "broken_after": len(ba),
        },
    }
