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


def diagnose(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
):
    """执行一次完整诊断。

    返回字典：
      可行   -> {"feasible": True, "reference": ..., "node_bounds": [...],
                 "unconstrained_nodes": [...]}
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
        node_bounds.append(
            {
                "node": v,
                "lower": lower,
                "upper": upper,
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
