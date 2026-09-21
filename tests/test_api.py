"""FastAPI 接口测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_diagnose_feasible():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is True
    bounds = {b["node"]: b for b in data["node_bounds"]}
    assert bounds["A"]["lower"] == 0.0 and bounds["A"]["upper"] == 0.0
    assert bounds["B"]["lower"] == 1.0 and bounds["B"]["upper"] == 5.0
    assert bounds["C"]["lower"] == 3.0 and bounds["C"]["upper"] == 9.0
    assert data["unconstrained_nodes"] == []


def test_diagnose_proof_chains_basic():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    bounds = {b["node"]: b for b in data["node_bounds"]}

    # 参考节点：两条空链
    assert bounds["A"]["upper_proof"] == []
    assert bounds["A"]["lower_proof"] == []

    # 上界链 A -> C：步骤首尾相接，weight 和等于 upper
    up = bounds["C"]["upper_proof"]
    assert up == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0},
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "C", "weight": 4.0},
    ]
    assert sum(s["weight"] for s in up) == bounds["C"]["upper"] == 9.0

    # 下界链 C -> A：weight 和的相反数等于 lower
    lp = bounds["C"]["lower_proof"]
    assert lp == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "C", "to": "B", "weight": -2.0},
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0},
    ]
    assert -sum(s["weight"] for s in lp) == bounds["C"]["lower"] == 3.0


def test_diagnose_proof_tiebreak_lex_smallest_and_order_independent():
    # 等权最短上界链两条：p1 单步 A->C(1) vs q1 A->B(1)+q2 B->C(0)；
    # 首步四元组 ("p1",... ) < ("q1",...)，取 p1。
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "p1", "source": "A", "target": "C", "min": 0, "max": 1},
            {"id": "q1", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "q2", "source": "B", "target": "C", "min": 0, "max": 0},
        ],
    }
    data1 = client.post("/api/v1/diagnose", json=payload).json()
    # 打乱提交顺序，证据链完全一致
    payload["observations"] = list(reversed(payload["observations"]))
    data2 = client.post("/api/v1/diagnose", json=payload).json()
    assert data1["node_bounds"] == data2["node_bounds"]

    c1 = next(b for b in data1["node_bounds"] if b["node"] == "C")
    assert [
        (s["observation_id"], s["constraint"], s["from"], s["to"])
        for s in c1["upper_proof"]
    ] == [("p1", "upper", "A", "C")]


def test_diagnose_proof_null_for_unreachable_empty_for_reference():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    bounds = {b["node"]: b for b in data["node_bounds"]}
    assert bounds["D"]["upper_proof"] is None
    assert bounds["D"]["lower_proof"] is None
    assert bounds["A"]["upper_proof"] == []
    assert bounds["A"]["lower_proof"] == []


def test_diagnose_infeasible_cycle_uses_from_alias():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is False
    steps = data["cycle"]["steps"]
    assert data["cycle"]["accumulated_bound"] == -3.0
    assert data["cycle"]["contradiction"] == "0 <= -3.0"
    # 序列化别名为 from，且每步闭环
    assert set(steps[0].keys()) >= {"from", "to", "observation_id", "constraint"}
    for i, step in enumerate(steps):
        assert step["from"] == steps[i - 1]["to"]


def test_unconstrained_node_reported():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["unconstrained_nodes"] == ["D"]
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["upper"] is None
    assert d["constrained"] is False


def test_invalid_interval_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 9, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_duplicate_observation_id_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_reference_missing_from_nodes_rejected():
    payload = {"reference": "Z", "nodes": ["A", "B"], "observations": []}
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_nodes_default_to_observation_endpoints():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    assert [b["node"] for b in data["node_bounds"]] == ["A", "B"]


def test_batch_order_and_index_stable():
    payload = {
        "diagnoses": [
            {
                "name": "ok",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
                ],
            },
            {
                "name": "bad",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1},
                    {"id": "o2", "source": "B", "target": "A", "min": 2, "max": 3},
                ],
            },
        ]
    }
    resp = client.post("/api/v1/diagnose/batch", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] == 2
    assert data["results"][0]["index"] == 0
    assert data["results"][0]["name"] == "ok"
    assert data["results"][0]["result"]["feasible"] is True
    assert data["results"][1]["index"] == 1
    assert data["results"][1]["name"] == "bad"
    assert data["results"][1]["result"]["feasible"] is False


def test_batch_empty_rejected():
    resp = client.post("/api/v1/diagnose/batch", json={"diagnoses": []})
    assert resp.status_code == 422


def test_repair_feasible_returns_empty_suggestion():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is False
    assert data["max_repairs"] == 3  # 默认上限
    assert data["diagnosis"]["feasible"] is True


def test_repair_infeasible_minimal_deletion():
    payload = {
        "name": "fixme",
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "fixme"
    assert data["removed_observation_ids"] == ["o1"]
    assert data["limit_reached"] is False
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert diag["name"] == "fixme"
    bounds = {b["node"]: b for b in diag["node_bounds"]}
    # 剩余 o2：x_A - x_B ∈ [3, 4] -> x_B - x_A ∈ [-4, -3]
    assert bounds["B"]["lower"] == -4.0 and bounds["B"]["upper"] == -3.0
    # 修复后的诊断同样携带证据链，链权值和与界一致
    up = bounds["B"]["upper_proof"]
    lp = bounds["B"]["lower_proof"]
    assert [s["observation_id"] for s in up] == ["o2"]
    assert sum(s["weight"] for s in up) == -3.0
    assert -sum(s["weight"] for s in lp) == -4.0


def test_repair_default_max_repairs_allows_two_deletions():
    # 两对独立矛盾需删 2 条，默认上限 3 足够
    payload = {
        "reference": "A",
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_repairs"] == 3
    assert data["removed_observation_ids"] == ["a1", "b1"]
    assert data["limit_reached"] is False
    assert data["diagnosis"]["feasible"] is True


def test_repair_limit_reached_returns_original_infeasible_diagnosis():
    payload = {
        "reference": "A",
        "max_repairs": 1,
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is True
    diag = data["diagnosis"]
    assert diag["feasible"] is False
    assert diag["cycle"]["accumulated_bound"] < 0


def test_repair_max_repairs_zero_on_infeasible():
    payload = {
        "reference": "A",
        "max_repairs": 0,
        "observations": [
            {"id": "bad", "source": "A", "target": "A", "min": 1, "max": 2}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["limit_reached"] is True
    assert data["removed_observation_ids"] == []
    assert data["diagnosis"]["feasible"] is False


def test_repair_negative_max_repairs_rejected():
    payload = {"reference": "A", "max_repairs": -1, "observations": []}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_unknown_field_rejected():
    payload = {"reference": "A", "observations": [], "bogus": 1}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_dropped_observation_node_vanishes():
    # bad 为 C 上的自环矛盾；删除后 C 不再是剩余系统的一部分
    payload = {
        "reference": "A",
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B"]


def test_repair_explicit_nodes_are_retained():
    # 显式声明的节点在修复后保留，被删观测的节点变为不受约束
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B", "C"]
    assert diag["unconstrained_nodes"] == ["C"]
