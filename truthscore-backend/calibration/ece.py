"""
TruthScore -- ECE Calibration and Feedback Loop.
"""
from config import *

_feedback_store: list = []


def compute_ece(results_path: str, n_bins: int = 10) -> dict:
    import csv
    rows = list(csv.DictReader(open(results_path, encoding="utf-8")))
    if not rows:
        return {"ece": None, "error": "empty file"}
    bins  = [[] for _ in range(n_bins)]
    total = 0
    for r in rows:
        try:
            score   = int(r.get("score", 50)) / 100.0
            correct = r.get("correct", "").upper() in ("YES", "TRUE")
            bin_idx = min(int(score * n_bins), n_bins - 1)
            bins[bin_idx].append((score, correct))
            total += 1
        except (ValueError, KeyError):
            pass
    if total == 0:
        return {"ece": None, "error": "no valid rows"}
    ece       = 0.0
    bin_stats = []
    for b in bins:
        if not b:
            continue
        avg_conf = sum(s for s, _ in b) / len(b)
        avg_acc  = sum(1 for _, c in b if c) / len(b)
        weight   = len(b) / total
        ece     += weight * abs(avg_conf - avg_acc)
        bin_stats.append({"n": len(b), "conf": round(avg_conf, 3),
                          "acc": round(avg_acc, 3)})
    return {
        "ece":     round(ece, 4),
        "total":   total,
        "bins":    bin_stats,
        "verdict": ("well-calibrated" if ece < 0.05
                    else "slightly overconfident" if ece < 0.10
                    else "overconfident"),
    }


def compute_calibration_map(results_path: str) -> dict:
    import csv
    from collections import defaultdict
    rows = list(csv.DictReader(open(results_path, encoding="utf-8")))
    bins = defaultdict(list)
    for r in rows:
        try:
            score  = int(r.get("score", 50))
            correct = r.get("correct", "").upper() in ("YES", "TRUE")
            bins[(score // 10) * 10].append(correct)
        except (ValueError, KeyError):
            pass
    cal_map = {}
    for bucket, results in sorted(bins.items()):
        accuracy = sum(results) / len(results) * 100
        cal_map[bucket] = round(accuracy)
    return cal_map


def record_feedback(claim: str, verdict: str, score: int,
                    topic: str, correct: bool,
                    failure_reason: str = "") -> None:
    import time
    entry = {
        "claim": claim[:200], "verdict": verdict,
        "score": score, "topic": topic,
        "correct": correct, "failure_reason": failure_reason,
        "timestamp": time.time(),
    }
    _feedback_store.append(entry)
    print(f"  [FEEDBACK] verdict={verdict} correct={correct} topic={topic}")


def get_weak_domains() -> list:
    from collections import defaultdict
    stats = defaultdict(lambda: {"correct": 0, "total": 0})
    for entry in _feedback_store:
        t = entry.get("topic", "general")
        stats[t]["total"] += 1
        if entry.get("correct"):
            stats[t]["correct"] += 1
    results = []
    for topic, s in stats.items():
        if s["total"] >= 5:
            acc = s["correct"] / s["total"]
            results.append((topic, round(acc, 3)))
    return sorted(results, key=lambda x: x[1])