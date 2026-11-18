"""
PROMPTx-RAG 步驟 4：依情境選擇檢索策略（2026-11-15）

方法對應張賢宗教授團隊的 PROMPTx-PE（Khan et al., CMC 2026, DOI 10.32604/cmc.2025.074557）：
  獎勵 R = 0.4 準確 + 0.4 穩健 + 0.2 效率（式 10，權重同式 4 的初始值）
    準確：0.5 前 3 名命中 + 0.3 第 1 名命中 + 0.2 需求召回（沒有需求的題目依比例重算）
    穩健：4 種擾動版本裡最差的前 3 名命中（式 6 取最差）
    效率：延遲分數 = 1 − 0.5·log10(延遲 / 50ms)，限制在 0～1
  情境（式 3）：rag/strategy_eval.py 的 context_features（句子長度、英文比例、是否寫出軟體名稱、
    人工目錄分數與分差、擴充目錄與人工的分差、需求關鍵字分數）
  策略學習：
    fixed_best   網格：訓練集平均獎勵最高的單一策略（基準）
    linucb       情境式多臂老虎機（Li et al., 2010；MBA-RAG 的做法），只看到選中策略的獎勵
    thompson     線性 Thompson 抽樣
    dqn          論文演算法 1：ε-greedy、經驗回放、目標網路（一步決策，目標值＝獎勵）
    linucb_aw    LinUCB＋式 7 的自動調權重（‖ΔAcc,ΔRob,ΔEff‖ > 0.05 才調）
    linucb_rb    LinUCB＋演算法 2 的退回機制（近期平均獎勵下降超過 δ 就退回上一個檢查點）
    oracle       每題挑最好的策略（上限，不可實作）
評估：依題號分組的 5 折交叉驗證 × 3 次亂數種子；與 fixed_best 比較用 Nadeau–Bengio 修正的重複抽樣 t 檢定
（各折共用訓練資料，一般配對 t 檢定會低估變異）。另做跨出題者測試（Gemini 題訓練 → Claude 題測試，反之亦然）。

  python rag/policy.py evaluate
輸出：docs/rag_promptx/policy_results.json、policy_folds.jsonl
"""
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "rag_promptx"
W = (0.4, 0.4, 0.2)
EXCLUDE_ARMS = {"seed_rerank"}   # 只有原句結果（每句約 8 秒），不列入策略候選，見 strategy_eval.ORIG_ONLY
FEAT_KEYS = ["len", "latin_ratio", "name_hit", "digits", "seed_top", "seed_margin", "ext_minus_seed", "req_bm25"]


# ── 資料 ─────────────────────────────────────────────────────────────
def eff_score(ms):
    return float(min(1.0, max(0.0, 1 - 0.5 * math.log10(max(ms, 50.0) / 50.0))))


def load(table=OUT / "strategy_table.jsonl", feats=OUT / "context_features.jsonl"):
    rows = [json.loads(x) for x in open(table, encoding="utf-8")]
    fx = {}
    for x in open(feats, encoding="utf-8"):
        r = json.loads(x)
        if r["variant"] == "orig":
            fx[r["id"]] = [float(r[k]) for k in FEAT_KEYS]
    by = defaultdict(lambda: defaultdict(dict))
    src = {}
    for r in rows:
        by[r["id"]][r["arm"]][r["variant"]] = r
        src[r["id"]] = r["src"]
    arms = sorted({r["arm"] for r in rows} - EXCLUDE_ARMS)
    items = []
    for qid, a in by.items():
        if qid not in fx or any("orig" not in a.get(arm, {}) for arm in arms):
            continue
        comp = []
        for arm in arms:
            o = a[arm]["orig"]
            nr = o["need_recall"]
            acc = (0.5 * o["hit3"] + 0.3 * o["hit1"] + 0.2 * nr) if nr is not None else (0.5 * o["hit3"] + 0.3 * o["hit1"]) / 0.8
            pert = [v["hit3"] for k, v in a[arm].items() if k != "orig"]
            rob = float(min(pert)) if pert else float(o["hit3"])
            lat = float(np.mean([v["latency_ms"] for v in a[arm].values()]))
            comp.append((acc, rob, eff_score(lat), lat, o["hit1"], o["hit3"], len(pert)))
        items.append({"id": qid, "src": src[qid], "x": fx[qid], "comp": comp})
    return items, arms


def reward(c, w=W):
    return w[0] * c[0] + w[1] * c[1] + w[2] * c[2]


# ── 策略 ─────────────────────────────────────────────────────────────
class Scaler:
    def fit(self, X):
        self.m, self.s = X.mean(0), X.std(0) + 1e-6
        return self

    def __call__(self, X):
        return np.hstack([(X - self.m) / self.s, np.ones((len(X), 1))])


def fixed_best(train, arms, rng):
    best = int(np.argmax([np.mean([reward(it["comp"][a]) for it in train]) for a in range(len(arms))]))
    return lambda X: np.full(len(X), best)


def _linear_bandit(train, arms, rng, kind="ucb", alpha=0.5, adaptive=False, rollback=False, passes=3, lam=1.0):
    sc = Scaler().fit(np.array([it["x"] for it in train]))
    d, K = len(FEAT_KEYS) + 1, len(arms)
    A = [lam * np.eye(d) for _ in range(K)]
    b = [np.zeros(d) for _ in range(K)]
    w = list(W)
    hist = []          # (acc, rob, eff) 的近期平均，供式 7
    ckpt, ckpt_avg, recent = None, None, []
    for _ in range(passes):
        for i in rng.permutation(len(train)):
            it = train[i]
            x = sc(np.array([it["x"]]))[0]
            if kind == "ucb":
                p = [x @ np.linalg.solve(A[k], b[k]) + alpha * math.sqrt(x @ np.linalg.solve(A[k], x)) for k in range(K)]
            else:
                p = []
                for k in range(K):
                    inv = np.linalg.inv(A[k])
                    theta = rng.multivariate_normal(inv @ b[k], (alpha ** 2) * inv)
                    p.append(x @ theta)
            k = int(np.argmax(p))
            c = it["comp"][k]
            r = reward(c, w)
            A[k] += np.outer(x, x)
            b[k] += r * x
            if adaptive:   # 式 7：λ_i ← λ_i·exp(−η·∂L/∂λ_i)，∂L/∂λ_i 取第 i 項的損失（1 − 分數）
                hist.append(c[:3])
                if len(hist) >= 40:
                    prev, now = np.mean(hist[-40:-20], 0), np.mean(hist[-20:], 0)
                    if np.linalg.norm(now - prev) > 0.05:
                        w = [w[j] * math.exp(-0.5 * (1 - now[j])) for j in range(3)]
                        s = sum(w)
                        w = [v / s for v in w]
            if rollback:   # 演算法 2：每 50 步比較近期平均獎勵，下降超過 δ 就退回檢查點
                recent.append(reward(c))
                if len(recent) == 50:
                    avg = float(np.mean(recent))
                    if ckpt_avg is not None and avg < ckpt_avg - 0.05:
                        A, b = [m.copy() for m in ckpt[0]], [v.copy() for v in ckpt[1]]
                    else:
                        ckpt, ckpt_avg = ([m.copy() for m in A], [v.copy() for v in b]), avg
                    recent = []
    theta = [np.linalg.solve(A[k], b[k]) for k in range(K)]
    return lambda X: np.argmax(sc(np.array(X)) @ np.array(theta).T, axis=1)


def dqn(train, arms, rng, episodes=6, eps0=1.0, eps_min=0.05, decay=0.6, batch=32, tau=50):
    import torch
    torch.manual_seed(int(rng.integers(1 << 30)))
    sc = Scaler().fit(np.array([it["x"] for it in train]))
    d, K = len(FEAT_KEYS) + 1, len(arms)

    def net():
        return torch.nn.Sequential(torch.nn.Linear(d, 64), torch.nn.ReLU(), torch.nn.Linear(64, 32),
                                   torch.nn.ReLU(), torch.nn.Linear(32, K))
    q, qt = net(), net()
    qt.load_state_dict(q.state_dict())
    opt = torch.optim.Adam(q.parameters(), lr=3e-3)
    buf, eps, step = [], eps0, 0
    for _ in range(episodes):
        for i in rng.permutation(len(train)):
            it = train[i]
            x = torch.tensor(sc(np.array([it["x"]]))[0], dtype=torch.float32)
            k = int(rng.integers(K)) if rng.random() < eps else int(q(x).argmax())
            buf.append((x, k, reward(it["comp"][k])))
            if len(buf) >= batch:
                idx = rng.choice(len(buf), batch, replace=False)
                xs = torch.stack([buf[j][0] for j in idx])
                ks = torch.tensor([buf[j][1] for j in idx])
                ys = torch.tensor([buf[j][2] for j in idx], dtype=torch.float32)  # 一步決策：目標值 = 獎勵
                loss = torch.nn.functional.mse_loss(q(xs).gather(1, ks[:, None]).squeeze(1), ys)
                opt.zero_grad()
                loss.backward()
                opt.step()
            step += 1
            if step % tau == 0:
                qt.load_state_dict(q.state_dict())
        eps = max(eps_min, eps * decay)

    def act(X):
        with torch.no_grad():
            return qt(torch.tensor(sc(np.array(X)), dtype=torch.float32)).argmax(1).numpy()
    return act


POLICIES = {
    "fixed_best": fixed_best,
    "linucb": lambda tr, ar, rng: _linear_bandit(tr, ar, rng, "ucb"),
    "thompson": lambda tr, ar, rng: _linear_bandit(tr, ar, rng, "ts", alpha=0.3),
    "dqn": dqn,
    "linucb_aw": lambda tr, ar, rng: _linear_bandit(tr, ar, rng, "ucb", adaptive=True),
    "linucb_rb": lambda tr, ar, rng: _linear_bandit(tr, ar, rng, "ucb", rollback=True),
}


# ── 評估 ─────────────────────────────────────────────────────────────
def score(test, choice, arms):
    cs = [it["comp"][int(k)] for it, k in zip(test, choice)]
    lat = sorted(c[3] for c in cs)
    return {"R": float(np.mean([reward(c) for c in cs])), "acc": float(np.mean([c[0] for c in cs])),
            "rob": float(np.mean([c[1] for c in cs])), "eff": float(np.mean([c[2] for c in cs])),
            "hit1": float(np.mean([c[4] for c in cs])), "hit3": float(np.mean([c[5] for c in cs])),
            "lat_p95": lat[int(0.95 * (len(lat) - 1))],
            "arm_share": {arms[a]: round(float(np.mean(np.array(choice) == a)), 3) for a in range(len(arms))}}


def folds(items, k, seed):
    def h(q):
        return int(hashlib.md5(f"{seed}:{q}".encode()).hexdigest(), 16) % k
    return [([it for it in items if h(it["id"]) != f], [it for it in items if h(it["id"]) == f]) for f in range(k)]


def corrected_t(diffs, n_train, n_test):
    """Nadeau & Bengio (2003) 修正的重複抽樣 t 檢定；回傳 (平均差, t, 雙尾 p, 95% CI)。"""
    from scipy import stats
    d = np.array(diffs)
    m, v, J = d.mean(), d.var(ddof=1), len(d)
    se = math.sqrt((1 / J + n_test / n_train) * v) if v > 0 else 1e-12
    t = m / se
    p = 2 * stats.t.sf(abs(t), J - 1)
    hw = stats.t.ppf(0.975, J - 1) * se
    return round(float(m), 4), round(float(t), 3), round(float(p), 4), [round(float(m - hw), 4), round(float(m + hw), 4)]


def evaluate(k=5, seeds=(0, 1, 2)):
    items, arms = load()
    print(f"題目 {len(items)}（Gemini {sum(i['src'] == 'gemini' for i in items)}、Claude {sum(i['src'] == 'claude' for i in items)}），"
          f"策略 {len(arms)}：{arms}", flush=True)
    per = defaultdict(list)
    fold_log = open(OUT / "policy_folds.jsonl", "w", encoding="utf-8")
    sizes = []
    for seed in seeds:
        for fi, (tr, te) in enumerate(folds(items, k, seed)):
            sizes.append((len(tr), len(te)))
            rng = np.random.default_rng(1000 * seed + fi)
            res = {}
            for name, make in POLICIES.items():
                pol = make(tr, arms, rng)
                res[name] = score(te, pol([it["x"] for it in te]), arms)
            res["oracle"] = score(te, [int(np.argmax([reward(c) for c in it["comp"]])) for it in te], arms)
            for a, arm in enumerate(arms):
                res[f"arm:{arm}"] = score(te, [a] * len(te), arms)
            for name, s in res.items():
                per[name].append(s)
            fold_log.write(json.dumps({"seed": seed, "fold": fi, "results": res}, ensure_ascii=False) + "\n")
            print(f"  種子 {seed} 折 {fi}：" + "  ".join(f"{n}={res[n]['R']:.3f}" for n in POLICIES) +
                  f"  oracle={res['oracle']['R']:.3f}", flush=True)
    n_tr, n_te = np.mean([s[0] for s in sizes]), np.mean([s[1] for s in sizes])
    summary = {}
    for name, lst in per.items():
        summary[name] = {m: round(float(np.mean([s[m] for s in lst])), 4) for m in ("R", "acc", "rob", "eff", "hit1", "hit3", "lat_p95")}
        if name != "fixed_best" and not name.startswith("arm:"):
            summary[name]["vs_fixed_best"] = corrected_t([a["R"] - b["R"] for a, b in zip(lst, per["fixed_best"])], n_tr, n_te)
    # 跨出題者：一個題組訓練、另一個題組測試
    cross = {}
    for tr_src, te_src in (("gemini", "claude"), ("claude", "gemini")):
        tr = [i for i in items if i["src"] == tr_src]
        te = [i for i in items if i["src"] == te_src]
        if not tr or not te:
            continue
        rng = np.random.default_rng(7)
        cross[f"{tr_src}→{te_src}"] = {name: round(score(te, make(tr, arms, rng)([it["x"] for it in te]), arms)["R"], 4)
                                       for name, make in POLICIES.items()}
    # 採用規則（寫死）：總分顯著高於 fixed_best（p < 0.05）且兩個跨出題者測試都不低於 fixed_best
    adopt = {}
    for name in POLICIES:
        if name == "fixed_best":
            continue
        t = summary[name]["vs_fixed_best"]
        cross_ok = all(v[name] >= v["fixed_best"] for v in cross.values())
        adopt[name] = bool(t[0] > 0 and t[2] < 0.05 and cross_ok)
    res = {"n_items": len(items), "arms": arms, "weights": W, "k": k, "seeds": list(seeds),
           "summary": summary, "cross_source": cross, "adopt": adopt,
           "rule": "總分顯著高於 fixed_best（Nadeau–Bengio 修正 t 檢定 p<0.05）且兩個跨出題者測試都不低於 fixed_best"}
    json.dump(res, open(OUT / "policy_results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({"summary": summary, "cross_source": cross, "adopt": adopt}, ensure_ascii=False, indent=1))
    return res


if __name__ == "__main__":
    {"evaluate": evaluate}[sys.argv[1]]()
