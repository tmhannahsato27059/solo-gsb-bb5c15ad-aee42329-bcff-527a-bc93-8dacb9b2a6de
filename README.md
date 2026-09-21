# 分布式时钟约束诊断 API

提交形如 **“B 时钟 − A 时钟位于 `[min, max]` 毫秒”** 的有向观测，服务将其转换为
[差分约束系统](https://en.wikipedia.org/wiki/Difference_constraints) 并完成：

1. **可行性判定**：是否存在一组自洽的节点时钟偏移；
2. **可行时**：给出每个节点相对指定 **参考节点** 的最紧上下界，并标出无法从参考节点
   双向约束的节点（孤岛 / 单向可达）；每个界同时返回一条由观测边组成的 **证据链**
   （`upper_proof` / `lower_proof`），沿链逐步累加即可复现该界；
3. **不可行时**：返回一条由观测 ID 组成的 **矛盾环**、环上每条约束边及累计界限
   （形如 `0 <= -3.0`），环的起点与顺序经过规范化，多次调用结果稳定；
4. **修复建议**：不可行时在不修改任何原观测的前提下，搜索使剩余系统恢复可行的
   **最小观测删除集合**（按删除数量、观测 ID 字典序唯一确定），返回待删观测 ID、
   修复后的诊断摘要，以及是否触及删除上限；
5. 提供 **批量诊断接口**，结果按提交下标稳定返回。

算法：差分约束图 + Bellman-Ford（超源初始化检测负环、前驱指针回卷提取矛盾环），
可行时在最短路的紧边子图上贪心提取字典序最小的证据链，
纯 Python 标准实现，无外部服务依赖。

---

## 一键启动（单条命令）

需要 Docker 与 Docker Compose v2：

```bash
docker compose up --build -d
```

服务监听并对外暴露 **8000 端口**：

- 健康检查：<http://localhost:8000/health>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：

```bash
docker compose down
```

不用 Compose 时：

```bash
docker build -t clock-constraint-diagnosis .
docker run --rm -p 8000:8000 clock-constraint-diagnosis
```

---

## 配置

配置全部通过环境变量完成（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 容器内 uvicorn 监听地址 |
| `PORT` | `8000` | 容器内监听端口；compose 同时用它映射宿主机端口 |

例如改用宿主机 18000 端口：

```bash
PORT=18000 docker compose up --build -d   # 访问 http://localhost:18000
```

---

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

---

## 接口

### `POST /api/v1/diagnose` — 单条诊断

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `reference` | string | 是 | 参考时钟节点（计算界的基准） |
| `observations` | array | 是 | 观测列表，可为空 |
| `observations[].id` | string | 是 | 观测唯一 ID（矛盾环按它回指） |
| `observations[].source` | string | 是 | 被减时钟节点 A |
| `observations[].target` | string | 是 | 被减时钟节点 B |
| `observations[].min` | number | 是 | 区间下界（毫秒），须 ≤ `max` |
| `observations[].max` | number | 是 | 区间上界（毫秒） |
| `nodes` | string[] | 否 | 显式节点列表；缺省取观测端点与 `reference` 的并集 |
| `name` | string | 否 | 可选名称，响应原样回显 |

语义：每条观测表示 `x_target - x_source ∈ [min, max]`（`x_v` 为节点 v 的时钟偏移，
单位毫秒）。

#### 可行响应（示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
      {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4}
    ]
  }'
```

```json
{
  "feasible": true,
  "reference": "A",
  "name": null,
  "node_bounds": [
    {"node": "A", "lower": 0.0, "upper": 0.0,
     "lower_proof": [], "upper_proof": [],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
    {"node": "B", "lower": 1.0, "upper": 5.0,
     "lower_proof": [
       {"observation_id": "o1", "constraint": "lower",
        "from": "B", "to": "A", "weight": -1.0}
     ],
     "upper_proof": [
       {"observation_id": "o1", "constraint": "upper",
        "from": "A", "to": "B", "weight": 5.0}
     ],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
    {"node": "C", "lower": 3.0, "upper": 9.0,
     "lower_proof": [
       {"observation_id": "o2", "constraint": "lower",
        "from": "C", "to": "B", "weight": -2.0},
       {"observation_id": "o1", "constraint": "lower",
        "from": "B", "to": "A", "weight": -1.0}
     ],
     "upper_proof": [
       {"observation_id": "o1", "constraint": "upper",
        "from": "A", "to": "B", "weight": 5.0},
       {"observation_id": "o2", "constraint": "upper",
        "from": "B", "to": "C", "weight": 4.0}
     ],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true}
  ],
  "unconstrained_nodes": []
}
```

`lower` / `upper` 即 `x_node - x_reference` 的最紧上下界（毫秒）。
无法从参考节点方向到达（上界缺失）或无法回到参考节点（下界缺失）的节点，对应界为
`null`，并被收入 `unconstrained_nodes`。

#### 证据链（`upper_proof` / `lower_proof`）

每个节点的界都附带可逐步复核的证据链：

- `upper_proof`：**从参考节点走到目标节点** 的一条最短链，步骤 `weight` 之和
  恰好等于 `upper`（例如上例 C：`5.0 + 4.0 = 9.0`）；
- `lower_proof`：**从目标节点走回参考节点** 的一条最短链，步骤 `weight` 之和
  的相反数恰好等于 `lower`（例如上例 C：`-(-2.0 + -1.0) = 3.0`）；
- 参考节点本身没有非平凡链，两条链均为空数组 `[]`；
- 界不可达时，界与其证据链均为 `null`；
- 每步包含 `observation_id`、`constraint`（`upper`=对应 `max` 的边，
  `lower`=对应 `-min` 的边）、`from`、`to`、`weight`；同一步的 `to` 等于
  下一步的 `from`，沿链首尾相接。

存在多条等权最短链时，选择 **步骤四元组序列**
`(observation_id, constraint, from, to)` 按字典序比较最小的那条链
（逐个四元组比较，而非边数最少）。因此结果只取决于观测集合本身，
**不受观测提交顺序影响**。注意候选只限于简单链：0 权环虽不改变权值和，
但会凭空延长序列，不参与比较。

#### 不可行响应（矛盾环，示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "feasible": false,
  "name": null,
  "cycle": {
    "steps": [
      {"observation_id": "o2", "from": "A", "to": "B",
       "constraint": "lower", "weight": -3.0, "bound": 3.0},
      {"observation_id": "o1", "from": "B", "to": "A",
       "constraint": "lower", "weight": 0.0, "bound": 0.0}
    ],
    "accumulated_bound": -3.0,
    "contradiction": "0 <= -3.0"
  }
}
```

- `steps` 沿环方向闭合排列：上一步的 `to` 等于下一步的 `from`；
- `constraint`：`upper` 表示该边对应观测的 `max`（边 `source -> target`，权 `max`），
  `lower` 表示对应 `-min`（边 `target -> source`，权 `-min`）；
- `weight` 为差分约束图中的边权，`accumulated_bound` 为环上权值之和（负数），
  沿环累加推出 `0 <= accumulated_bound`，即矛盾式。

### `POST /api/v1/diagnose/repair` — 修复建议

请求体沿用单条诊断请求的全部字段，并增加一个可选字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `max_repairs` | integer | 否 | 最多允许删除的观测条数，默认 `3`，须 ≥ 0 |

语义：**不修改任何原观测**，仅搜索「删除哪些观测可让剩余系统恢复可行」。
候选集合按 **删除数量最少** 优先、同数量时按 **观测 ID 字典序** 最小枚举，
首个可行集合即唯一答案（与观测提交顺序无关）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string/null | 请求名称回显 |
| `max_repairs` | integer | 本次生效的删除上限 |
| `removed_observation_ids` | string[] | 建议删除的观测 ID（字典序）；空数组表示无需删除或上限内无解 |
| `limit_reached` | boolean | `true` 表示在 `max_repairs` 上限内不存在可行修复（此时建议为空） |
| `diagnosis` | object | 应用建议后的诊断摘要（结构同单条诊断响应） |

三种情形：

- **原系统本就可行**：`removed_observation_ids` 为空、`limit_reached=false`，
  `diagnosis` 即原系统的可行诊断；
- **修复成功**：`removed_observation_ids` 给出待删集合，`diagnosis` 为删除后
  剩余系统的可行诊断。其节点集合按「显式 `nodes` ∪ `reference` ∪ 剩余观测端点」
  重新计算——仅出现在被删观测中的节点随之消失；显式声明的节点保留，
  并可能落入 `unconstrained_nodes`；
- **达到上限**：`removed_observation_ids` 为空、`limit_reached=true`，
  `diagnosis` 为原系统的不可行诊断（含矛盾环），可调大 `max_repairs` 重试。
  注意搜索是穷举的，开销随上限组合式增长，请勿设置过大的上限。

#### 修复成功（示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/repair \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "name": null,
  "max_repairs": 3,
  "removed_observation_ids": ["o1"],
  "limit_reached": false,
  "diagnosis": {
    "feasible": true,
    "reference": "A",
    "name": null,
    "node_bounds": [
      {"node": "A", "lower": 0.0, "upper": 0.0,
       "lower_proof": [], "upper_proof": [],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
      {"node": "B", "lower": -4.0, "upper": -3.0,
       "lower_proof": [
         {"observation_id": "o2", "constraint": "upper",
          "from": "B", "to": "A", "weight": 4.0}
       ],
       "upper_proof": [
         {"observation_id": "o2", "constraint": "lower",
          "from": "A", "to": "B", "weight": -3.0}
       ],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true}
    ],
    "unconstrained_nodes": []
  }
}
```

删除 `o1` 后仅剩 `o2`（`x_A - x_B ∈ [3, 4]`），故 `x_B - x_A ∈ [-4, -3]`。

#### 达到上限（示例）

两对独立矛盾至少需要删除 2 条观测，`max_repairs=1` 时：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/repair \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "max_repairs": 1,
    "observations": [
      {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
      {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
      {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "name": null,
  "max_repairs": 1,
  "removed_observation_ids": [],
  "limit_reached": true,
  "diagnosis": {
    "feasible": false,
    "name": null,
    "cycle": {
      "steps": [
        {"observation_id": "a2", "from": "A", "to": "B",
         "constraint": "lower", "weight": -3.0, "bound": 3.0},
        {"observation_id": "a1", "from": "B", "to": "A",
         "constraint": "lower", "weight": 0.0, "bound": 0.0}
      ],
      "accumulated_bound": -3.0,
      "contradiction": "0 <= -3.0"
    }
  }
}
```

### `POST /api/v1/diagnose/batch` — 批量诊断

请求体 `{"diagnoses": [ <单条请求>, ... ]}`（至少 1 条）。每条可带 `name`，
响应按提交顺序返回 `index`，互不影响：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/batch \
  -H 'Content-Type: application/json' \
  -d '{"diagnoses":[
    {"name":"ok", "reference":"A",
     "observations":[{"id":"x","source":"A","target":"B","min":0,"max":2}]},
    {"name":"bad", "reference":"A",
     "observations":[
       {"id":"o1","source":"A","target":"B","min":0,"max":10},
       {"id":"o2","source":"B","target":"A","min":3,"max":4}
     ]}
  ]}'
```

### `GET /health`

返回 `{"status": "ok"}`，compose 已配置健康检查。

### 校验错误

非法区间（`min > max`）、重复观测 ID、`reference` 不在 `nodes` 中等请求问题返回
HTTP `422` 及详细错误信息。

---

## 建模与稳定性说明

每条观测 `x_B - x_A ∈ [min, max]` 展开为两条边：

- `A -> B`，权 `max`（上界约束 `x_B - x_A <= max`）
- `B -> A`，权 `-min`（下界约束 `x_A - x_B <= -min`）

性质：

- 参考节点 `r` 到 `v` 的**最短路** = `x_v - x_r` 的最紧上界；
- 反图中 `r` 到 `v` 的最短路取负 = 最紧下界；
- **证据链**：在距离标签下满足「紧边」条件（上界链 `d[v] = d[u] + w`，
  下界链 `d[u] = w + d[v]`）的边构成最短链子图，其中任意参考节点 ↔ 目标
  节点的路径权值和都等于对应界；在该子图上贪心地取步骤四元组
  `(observation_id, constraint, from, to)` 序列字典序最小的**简单链**，
  即得到 `upper_proof` / `lower_proof`。贪心每步要求所选边的终点在避开
  已访问节点的前提下仍能到达目标，因此不会被 0 权环引入死循环，
  也不需要回溯；
- **负环 ⇔ 不可行**；负环即矛盾环，环上权值之和恒为负；
- **修复**：穷举删除子集（按删除数量、观测 ID 字典序），首个使剩余图无负环的
  集合即唯一修复建议；删除全部观测必然可行，因此上限不小于观测总数时必有解。

结果顺序的确定性保证：

- 节点一律按字典序输出；
- Bellman-Ford 的边按 `(观测 ID, 约束类型 upper<lower)` 排序后松弛，先到先松弛；
- 证据链的出边按步骤四元组 `(观测 ID, 约束类型 upper<lower, from, to)`
  排序后贪心选链；因此相同的观测集合无论提交顺序如何，证据链完全一致；
- 矛盾环先沿前驱回卷，再旋转到 `(起点节点, 观测 ID, 约束类型)` 最小的边作为唯一起点；
- 因此相同的观测集合无论提交顺序如何，证据链、矛盾环与累计界限完全一致。

## 项目结构

```
app/
  main.py      # FastAPI 路由：/api/v1/diagnose、/api/v1/diagnose/batch、/api/v1/diagnose/repair、/health
  models.py    # 请求/响应 Pydantic 模型与入参校验（含证据链步骤模型）
  solver.py    # 差分约束建模、Bellman-Ford、负环提取与规范化、证据链贪心提取、最小删除集合搜索
tests/         # 算法层与 HTTP 接口测试（48 个用例）
Dockerfile
docker-compose.yml
```
