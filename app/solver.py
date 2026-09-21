"""差分约束求解核心。

观测语义
--------
一条观测 ``obs`` 表示有向关系（单位：毫秒）::

    obs.min <= clock[obs.target] - clock[obs.source] <= obs.max

等价于两条差分约束（令 x_v 为节点 v 的时钟偏移）::

    x_target - x_source <= obs.max   ->  边 source -> target，权 obs.max   (上界约束)
    x_source - x_target <= -obs.min  ->  边 target -> source，权 -obs.min  (下界约束)

在该有向图中：

* 从参考节点 r 到 v 的最短路长度即 x_v - x_r 的最紧上界；
* 在反图中从 r 到 v 的最短路长度（即原图 v -> r 的最短路）取负，
  得到最紧下界；
* 图中存在负环 ⇔ 约束系统不可行。负环上的边按方向累加其权值，
  总和为负，即形如 ``0 <= 负数`` 的矛盾。

可行诊断中每个节点的上下界还附带 **证据链**（``upper_proof`` /
``lower_proof``）：上界链是参考节点到该节点的一条最短链（边权和等于上界），
下界链是该节点回到参考节点的一条最短链（边权和的相反数等于下界）。
多条等权最短链时，按各步骤四元组
``(observation_id, constraint, from, to)`` 序列的字典序取最小者，
与观测提交顺序无关。

所有结果的顺序均按 (观测 ID, 约束类型) / 节点字典序确定，保证稳定输出。

修复建议
--------
``suggest_repair`` 在不修改任何观测的前提下，搜索使剩余系统恢复可行的
最小观测删除集合：按 (删除数量, 观测 ID 字典序) 递增枚举候选，首个可行
集合即唯一答案，与观测的提交顺序无关。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Observation:
    """一条有向时钟观测：x_target - x_source ∈ [min, max]（毫秒）。"""

    id: str
    source: str
    target: str
    min: float
    max: float


@dataclass(frozen=True)
class Edge:
    u: str
    v: str
    weight: float
    observation_id: str
    kind: str  # "upper"（上界边）或 "lower"（下界边）
    bound: float  # 对应观测的原始端点（upper->max, lower->min）


def _norm(value: float) -> float:
    """归一化浮点输出：避免 -0.0，并保持有限数值。"""
    if value == 0:
        return 0.0
    return float(value)


def _build_edges(observations: Sequence[Observation]) -> List[Edge]:
    """把观测展开为确定性顺序的边列表。"""
    edges: List[Edge] = []
    for obs in observations:
        edges.append(
            Edge(
                u=obs.source,
                v=obs.target,
                weight=_norm(obs.max),
                observation_id=obs.id,
                kind="upper",
                bound=obs.max,
            )
        )
        edges.append(
            Edge(
                u=obs.target,
                v=obs.source,
                weight=_norm(-obs.min),
                observation_id=obs.id,
                kind="lower",
                bound=obs.min,
            )
        )
    # upper(0) 排在 lower(1) 之前；同观测同类型保持声明顺序，整体顺序确定。
    kind_rank = {"upper": 0, "lower": 1}
    edges.sort(key=lambda e: (e.observation_id, kind_rank[e.kind]))
    return edges


def _bellman_ford(edges: Sequence[Edge], nodes: Sequence[str], source: str):
    """从单源跑 Bellman-Ford（最多 V-1 轮）。

    返回 (dist, predecessor)；不可达节点距离为 None。
    边遍历顺序固定，因此松弛决策（先到先得）完全确定。
    """
    inf = float("inf")
    dist = {v: inf for v in nodes}
    prev: dict[str, Optional[Edge]] = {v: None for v in nodes}
    dist[source] = 0.0

    for _ in range(len(nodes) - 1):
        updated = False
        for e in edges:
            du = dist[e.u]
            if du == inf:
                continue
            nd = du + e.weight
            if nd < dist[e.v]:
                dist[e.v] = nd
                prev[e.v] = e
                updated = True
        if not updated:
            break
    return dist, prev


def _extract_negative_cycle(
    edges: Sequence[Edge], nodes: Sequence[str]
) -> Optional[List[Edge]]:
    """以零虚拟源运行 Bellman-Ford，检测并提取一个负环。

    步骤固定：先跑满 V 轮松弛，再额外扫描第一条仍可松弛的边，从该边的
    起点沿前驱指针走入环，回卷得到环上的边（按环方向排列）。
    """
    dist = {v: 0.0 for v in nodes}
    prev: dict[str, Optional[Edge]] = {v: None for v in nodes}

    # 无负环时所有最短路至多 V-1 轮收敛，因此这里跑满 V 轮不提前终止，
    # 再用一次额外扫描确认是否仍可松弛（不能以"第 V 轮有更新"直接判定，
    # 因为个别边可能需要第 V 轮才完成收敛性传播）。
    for _ in range(len(nodes)):
        for e in edges:
            nd = dist[e.u] + e.weight
            if nd < dist[e.v]:
                dist[e.v] = nd
                prev[e.v] = e

    # 从该松弛边的起点沿前驱指针走 V 步：起点已参与 V 轮松弛，其前驱链
    # 走至多 V-1 步必进入某个负环（从终点走则可能恰好走出一条长度为 V
    # 的传播链而得到空前驱）。随后记录节点直到再次相遇即得到简单环。
    relaxed_edge: Optional[Edge] = None
    for e in edges:
        if dist[e.u] + e.weight < dist[e.v]:
            relaxed_edge = e
            break
    if relaxed_edge is None:
        return None

    cur = relaxed_edge.u
    for _ in range(len(nodes)):
        pe = prev[cur]
        if pe is None:  # 理论上不会发生，防御性处理
            return None
        cur = pe.u

    path: List[str] = []
    on_path: dict[str, int] = {}
    while cur not in on_path:
        on_path[cur] = len(path)
        path.append(cur)
        pe = prev[cur]
        if pe is None:
            return None
        cur = pe.u

    cycle_nodes = path[on_path[cur] :]

    # 由环内节点 -> 前驱边，恢复成沿环方向排列的边序列。
    cycle_edges: List[Edge] = []
    for node in cycle_nodes:
        pe = prev[node]
        if pe is None:
            return None
        cycle_edges.append(pe)
    cycle_edges.reverse()

    # 闭环校验，防止任何异常状态下返回不自洽的结果。
    for i, edge in enumerate(cycle_edges):
        if edge.u != cycle_edges[i - 1].v:
            return None
    return cycle_edges


def _canonicalize_cycle(cycle_edges: List[Edge]) -> List[Edge]:
    """将环旋转为唯一表示：以 (起点节点, 观测 ID, 类型) 字典序最小的边开头。"""
    kind_rank = {"upper": 0, "lower": 1}
    keys = [
        (e.u, e.observation_id, kind_rank[e.kind]) for e in cycle_edges
    ]
    start_index = min(range(len(cycle_edges)), key=lambda i: keys[i])
    return cycle_edges[start_index:] + cycle_edges[:start_index]


# 步骤四元组 (observation_id, constraint, from, to) 中约束类型的次序。
_PROOF_KIND_RANK = {"upper": 0, "lower": 1}
# 判定「紧边」时的浮点容差：只吸收同值不同运算顺序导致的舍入差，
# 不会把真正更长的路径纳入最短链 DAG。
_PROOF_EPS = 1e-9


def _edge_key(e: Edge) -> tuple:
    """步骤四元组 (observation_id, constraint, from, to) 的字典序键。"""
    return (e.observation_id, _PROOF_KIND_RANK[e.kind], e.u, e.v)


def _ordered_out_edges(edges: Sequence[Edge], nodes: Sequence[str]):
    """按步骤四元组字典序排好的出边表 adj[u] = [e, ...]。"""
    adj = {v: [] for v in nodes}
    for e in edges:
        if e.u in adj and e.v in adj:
            adj[e.u].append(e)
    for out in adj.values():
        out.sort(key=_edge_key)
    return adj


def _lex_smallest_shortest_path(
    start: str,
    target: str,
    dist,
    adj,
    distance_to_target: bool,
) -> List[Edge]:
    """求 start -> target 的字典序最小最短（简单）链。

    ``distance_to_target`` 指明 ``dist`` 的语义：

    * ``False``：dist 为距 start（链的起点）的最短距离，紧边条件
      ``dist[v] == dist[u] + weight``（上界链使用）；
    * ``True``：dist 为到 target（链的终点）的最短距离，紧边条件
      ``dist[u] == weight + dist[v]``（下界链使用）。

    紧边子图中任意 start->target 路径的权值和都等于最短路；由于权值和
    为 0 的环（非简单路径）会制造任意长但字典序更小的前缀，这里只在
    简单路径中取字典序最小者。

    贪心每一步选择四元组字典序最小、且其终点在「不经过已访问节点」的
    前提下仍能沿紧边到达 target 的边。该可行性检查保证被选前缀总能补成
    一条简单最短链，故无需回溯，且选择只由图的四元组次序决定，
    与观测提交顺序无关。
    """
    if start == target:
        return []

    def can_reach(src: str, blocked: frozenset) -> bool:
        """紧边子图中 src 不经过 blocked 节点能否到达 target。"""
        if src == target:
            return True
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            for e in adj[u]:
                if not _is_tight(e, dist, distance_to_target):
                    continue
                if e.v == target:
                    return True
                if e.v not in seen and e.v not in blocked:
                    seen.add(e.v)
                    stack.append(e.v)
        return False

    path: List[Edge] = []
    cur = start
    visited = {start}
    while cur != target:
        chosen: Optional[Edge] = None
        for e in adj[cur]:
            if e.v in visited:
                continue
            if not _is_tight(e, dist, distance_to_target):
                continue
            if can_reach(e.v, frozenset(visited)):
                chosen = e
                break
        if chosen is None:  # 理论上不会发生：cur 本身即可达 target
            break
        path.append(chosen)
        cur = chosen.v
        visited.add(cur)
    return path


def _is_tight(e: Edge, dist, distance_to_target: bool) -> bool:
    """边 e 是否为关于 dist 的紧边。

    distance_to_target=False（距链起点的最短路）：dist[v] == dist[u] + w；
    distance_to_target=True （到链终点的最短路）：dist[u] == w + dist[v]。
    """
    du, dv = dist[e.u], dist[e.v]
    if du == float("inf") or dv == float("inf"):
        return False
    if distance_to_target:
        gap = du - (e.weight + dv)
    else:
        gap = dv - (du + e.weight)
    return abs(gap) <= _PROOF_EPS * (1.0 + abs(du) + abs(dv))


def _step_dict(e: Edge) -> dict:
    """把边格式化为证据链步骤（from/to 保持边在原图中的方向）。"""
    return {
        "observation_id": e.observation_id,
        "constraint": e.kind,
        "from": e.u,
        "to": e.v,
        "weight": _norm(e.weight),
    }


def diagnose(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
):
    """执行一次完整诊断。

    返回字典：
      可行   -> {"feasible": True, "reference": ..., "node_bounds": [...],
                 "unconstrained_nodes": [...]}
               其中每个 node_bound 附带：
                 * upper_proof：reference -> 该节点的字典序最小最短链
                   （步骤权值和等于 upper），参考节点为空链，不可达为 None；
                 * lower_proof：该节点 -> reference 的字典序最小最短链
                   （步骤权值和的相反数等于 lower），参考节点为空链，不可达为 None。
      不可行 -> {"feasible": False, "cycle": {"steps": [...],
                 "accumulated_bound": float, "contradiction": str}}
    """
    ordered_nodes = sorted(set(nodes))
    edges = _build_edges(observations)

    cycle_edges = _extract_negative_cycle(edges, ordered_nodes)
    if cycle_edges is not None:
        cycle_edges = _canonicalize_cycle(cycle_edges)
        steps = [
            {
                "observation_id": e.observation_id,
                "from": e.u,
                "to": e.v,
                "constraint": e.kind,
                "weight": _norm(e.weight),
                "bound": _norm(e.bound),
            }
            for e in cycle_edges
        ]
        total = _norm(sum(e.weight for e in cycle_edges))
        return {
            "feasible": False,
            "cycle": {
                "steps": steps,
                "accumulated_bound": total,
                "contradiction": f"0 <= {total}",
            },
        }

    # 可行：正向最短路给出最紧上界。
    dist, _ = _bellman_ford(edges, ordered_nodes, reference)

    # 反图最短路 dist_rev[v] = 原图 v -> reference 的最短路，
    # 于是最紧下界 x_v - x_r >= -dist_rev[v]。
    reverse_edges = [
        Edge(
            u=e.v,
            v=e.u,
            weight=e.weight,
            observation_id=e.observation_id,
            kind=e.kind,
            bound=e.bound,
        )
        for e in edges
    ]
    dist_rev, _ = _bellman_ford(reverse_edges, ordered_nodes, reference)

    # 证据链：
    # 上界链沿原图 reference -> v，关于 dist（距参考节点的最短路）紧；
    # 下界链沿原图 v -> reference，关于 dist_rev（到参考节点的最短路）紧，
    # 步骤按 v -> reference 的行走顺序排列。两者都在原图的出边表上做
    # 字典序贪心，选择只由步骤四元组次序决定，与观测提交顺序无关。
    adj = _ordered_out_edges(edges, ordered_nodes)

    inf = float("inf")
    node_bounds = []
    unconstrained: List[str] = []
    for v in ordered_nodes:
        reachable_forward = dist[v] != inf
        reachable_reverse = dist_rev[v] != inf
        upper = _norm(dist[v]) if reachable_forward else None
        lower = _norm(-dist_rev[v]) if reachable_reverse else None
        constrained = reachable_forward and reachable_reverse
        if not constrained:
            unconstrained.append(v)
        if reachable_forward:
            upper_edges = _lex_smallest_shortest_path(
                reference, v, dist, adj, distance_to_target=False
            )
            upper_proof = [_step_dict(e) for e in upper_edges]
        else:
            upper_proof = None
        if reachable_reverse:
            lower_edges = _lex_smallest_shortest_path(
                v, reference, dist_rev, adj, distance_to_target=True
            )
            lower_proof = [_step_dict(e) for e in lower_edges]
        else:
            lower_proof = None
        node_bounds.append(
            {
                "node": v,
                "lower": lower,
                "upper": upper,
                "lower_proof": lower_proof,
                "upper_proof": upper_proof,
                "reachable_from_reference": reachable_forward,
                "can_reach_reference": reachable_reverse,
                "constrained": constrained,
            }
        )

    return {
        "feasible": True,
        "reference": reference,
        "node_bounds": node_bounds,
        "unconstrained_nodes": unconstrained,
    }


def suggest_repair(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
    max_repairs: int,
) -> Tuple[Optional[List[str]], Optional[List[Observation]]]:
    """搜索使剩余系统恢复可行的最小观测删除集合（不修改任何观测）。

    按 (删除数量, 观测 ID 字典序) 递增枚举候选集合：
    ``itertools.combinations`` 在按 ID 排序的输入上即按字典序产出组合，
    因此第一个使剩余系统可行的集合就是唯一答案。

    返回 ``(removed_ids, remaining)``：

    * 系统本就可行 -> ``([], 按 ID 排序的全部观测)``；
    * 找到修复 -> ``removed_ids`` 为按字典序排列的待删观测 ID，
      ``remaining`` 为删除后的剩余观测（原观测对象不变）；
    * 上限内无解 -> ``(None, None)``。

    说明：搜索是穷举的，开销随 ``max_repairs`` 组合式增长，
    调用方应把删除上限控制在较小范围。
    """
    ordered = sorted(observations, key=lambda o: o.id)
    if diagnose(nodes, reference, ordered)["feasible"]:
        return [], list(ordered)

    budget = max(0, min(max_repairs, len(ordered)))
    for k in range(1, budget + 1):
        for combo in itertools.combinations(ordered, k):
            removed = {o.id for o in combo}
            remaining = [o for o in ordered if o.id not in removed]
            if diagnose(nodes, reference, remaining)["feasible"]:
                return [o.id for o in combo], remaining
    return None, None
