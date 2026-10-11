#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/dice.py —— 《凡人修仙传·人生模拟器》骰子引擎
=====================================================

**骰值一律由本脚本生成，模型不得自拟骰值、不得口算"随机"。**

随机性来源（为什么这叫"真随机"）
--------------------------------
- 熵源：``os.urandom()`` → 操作系统 CSPRNG（Windows 上是 BCryptGenRandom，
  Linux/macOS 上是 getrandom(2)／/dev/urandom）。
- 熵池混合**硬件级噪声**：CPU RDRAND/RDSEED 指令、硬件中断时序、
  键鼠/磁盘/网络抖动等，由内核持续搅拌。软件层面这是能做到的最强随机
  （密码学安全、不可预测、无种子可猜、无周期）。
  要"物理意义上的真随机"得接量子噪声发生器，对本游戏不必要。
- 不用 ``random`` 模块：它是 Mersenne Twister **伪随机**，输出几十个数
  就能反推内部状态，绝不允许出现在公证链上。

无偏性（拒绝采样）
------------------
朴素做法 ``urandom % n`` 在 2^k 不整除 n 时对余数有取模偏差。
本脚本只取 ``[0, limit)`` 内的值、超界直接丢弃重取（limit = 2^8k 内
最大的 n 的倍数），保证每个结果**严格等概率**。

审计链（防篡改公证）
--------------------
每一次投骰追加写入 ``dice-log.jsonl``，每条记录含上一条的 SHA-256
（哈希链）＋本条内容的 SHA-256。任何一条被改动/删除/插入，
``verify`` 子命令立刻能查出来。这就是本作「全程公示、可回溯、防暗改」
的机械化落法。

用法
----
    python tools/dice.py d100                    # 一枚 D100
    python tools/dice.py 3d100                   # 一次投 3 枚 D100
    python tools/dice.py d20 d6                  # 多种骰各一枚
    python tools/dice.py judge -t 55             # D100 判定（T=55），自动派发五段
    python tools/dice.py judge -t 55 --tier hard # 严苛骰档（大成功≤3/大失败≥94）
    python tools/dice.py judge -t 40 --hidden    # 隐藏骰：只报结果不报骰值
    python tools/dice.py weighted "天灵根:2,异灵根:5,双灵根:13,三灵根:30,四灵根:35,五灵根:15"
    python tools/dice.py verify                  # 校验审计链完整性
    python tools/dice.py tail 5                  # 看最近 5 条投骰记录

五段判定口径（《规则补遗》11.1/11.2，TZ-0029）
----------------------------------------------
设 T = 阈值，骰档给定 大成功界 gs 与 大失败界 mf：
宽 gs≤8/mf≥95 ／ 正常 gs≤5/mf≥96 ／ 严苛 gs≤3/mf≥94。
可用空间 S = (mf-1) - T（正常档即原文的 95-T）：
  D ≤ gs            → 大成功（完全成功+额外收益）
  D ≤ T             → 成功（基础收益完整）
  D ≤ T + S/3       → A1 胜有代价（达成+一个负面）
  D ≤ T + 2S/3      → A2 下台阶（未达成+有下台阶）
  D ≤ mf-1          → A3 只给基础（达成+附加收益全取消）
  D ≥ mf            → 大失败（未达成+额外损失）
T ≥ 96 无部分成功（三段）；S ≤ 0 时 A 档自动消失（四段）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "dice-log.jsonl"
LOCK_PATH = Path(__file__).resolve().parent / "dice-log.lock"
GENESIS = "0" * 64
DICE_RE = re.compile(r"^(\d*)[dD](\d+)$")

# 骰档 → (大成功上界 gs, 大失败下界 mf)；与 xiuzhen-gm 投骰规范一致
TIERS = {
    "wide": (8, 95),
    "normal": (5, 96),
    "hard": (3, 94),
}


# ---------------------------------------------------------------- 随机核心

def rand_below(n: int, count: int = 1) -> list[int]:
    """返回 count 个 [0, n) 的严格均匀整数（拒绝采样，无取模偏差）。"""
    if n < 1:
        raise ValueError("n 必须 ≥ 1")
    if n == 1:
        return [0] * count
    k = (n.bit_length() + 7) // 8                 # 每个样本取 k 字节
    space = 1 << (8 * k)
    limit = space - (space % n)                    # 2^8k 内最大的 n 的倍数
    out: list[int] = []
    while len(out) < count:
        need = count - len(out)
        # 一次多取 25% + 8 个样本的熵，摊薄系统调用；超界样本丢弃
        raw = os.urandom(k * (need + (need >> 2) + 8))
        for i in range(0, len(raw) - k + 1, k):
            v = int.from_bytes(raw[i:i + k], "big")
            if v < limit:
                out.append(v % n)
                if len(out) == count:
                    break
    return out


def roll(faces: int, count: int = 1) -> list[int]:
    """投 count 枚 faces 面骰（1..faces）。"""
    if faces < 2:
        raise ValueError("骰子面数必须 ≥ 2")
    return [x + 1 for x in rand_below(faces, count)]


# ---------------------------------------------------------------- 审计链

def _entry_hash(prev: str, entry: dict) -> str:
    blob = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + blob).encode("utf-8")).hexdigest()


def _last_hash() -> str:
    if not LOG_PATH.exists():
        return GENESIS
    line = ""
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            pass
    if not line.strip():
        return GENESIS
    try:
        return json.loads(line)["hash"]
    except Exception:
        return GENESIS


def _next_id() -> int:
    if not LOG_PATH.exists():
        return 1
    n = 0
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for _ in f:
            n += 1
    return n + 1


@contextmanager
def _log_lock():
    """跨进程互斥：并发投骰时保证哈希链串行写入，不产生岔链。"""
    deadline = time.time() + 10.0
    while True:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            if time.time() > deadline:      # 锁残留超时后强行接管，避免死锁
                try:
                    os.unlink(LOCK_PATH)
                except OSError:
                    pass
                continue
            time.sleep(0.005)
    try:
        yield
    finally:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass


def log_event(kind: str, detail: dict) -> dict:
    """把一次投骰写入哈希链，返回该条记录。"""
    with _log_lock():
        entry = {
            "id": _next_id(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "kind": kind,
            "detail": detail,
            "prev": _last_hash(),
        }
        entry["hash"] = _entry_hash(entry["prev"], entry)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry


def verify_chain() -> tuple[bool, int, str]:
    """校验整条哈希链，返回 (是否完好, 条数, 说明)。"""
    if not LOG_PATH.exists():
        return True, 0, "审计链为空"
    prev = GENESIS
    n = 0
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except Exception as exc:
                return False, n, f"第 {ln} 行不是合法 JSON：{exc}"
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev") != prev:
                return False, n, f"第 {ln} 行 prev 与上一条不符（链断裂）"
            if _entry_hash(prev, body) != e.get("hash"):
                return False, n, f"第 {ln} 行内容与哈希不符（被改动）"
            prev = e["hash"]
            n += 1
    return True, n, "完好"


# ---------------------------------------------------------------- 五段判定

def five_segment(d: int, t: int, tier: str = "normal") -> str:
    """D100 五段派发（补遗 11.1/11.2，天道零裁量）。"""
    gs, mf = TIERS[tier]
    if d <= gs:
        return "大成功"
    if d >= mf:
        return "大失败"
    if d <= t:
        return "成功"
    cap = mf - 1
    s = max(cap - t, 0)
    if s == 0:
        return "大失败" if d >= mf else "成功"   # T=cap 时无部分成功
    a1 = t + s / 3
    a2 = t + 2 * s / 3
    if d <= a1:
        return "A1 胜有代价"
    if d <= a2:
        return "A2 下台阶"
    return "A3 只给基础"


# ---------------------------------------------------------------- 子命令

def cmd_roll(specs: list[str], hidden: bool) -> None:
    results = []
    for spec in specs:
        m = DICE_RE.match(spec.strip())
        if not m:
            sys.exit(f"无法识别骰式：{spec!r}（形如 d100 / 3d100 / d6）")
        count = int(m.group(1) or 1)
        faces = int(m.group(2))
        if count < 1 or count > 10000:
            sys.exit("投骰枚数须在 1~10000 之间")
        vals = roll(faces, count)
        results.append({"spec": spec, "faces": faces, "count": count, "values": vals})
    entry = log_event("roll", {"rolls": results, "hidden": hidden})
    for r in results:
        shown = "？（隐藏）" if hidden else ", ".join(map(str, r["values"]))
        print(f"[dice] {r['spec']} → {shown}")
    print(f"      (id={entry['id']:06d}, hash={entry['hash'][:16]}…)")


def cmd_judge(t: int, tier: str, hidden: bool, note: str) -> None:
    if not 0 <= t <= 100:
        sys.exit("阈值 T 须在 0~100 之间")
    [d] = roll(100, 1)
    seg = five_segment(d, t, tier)
    gs, mf = TIERS[tier]
    entry = log_event("judge", {
        "d100": d, "T": t, "tier": tier,
        "gs": gs, "mf": mf, "segment": seg, "note": note,
    })
    shown = "？（隐藏）" if hidden else d
    print(f"[dice] 判定{'「%s」' % note if note else ''} D100 = {shown} ｜ T ≤ {t} ｜ {seg}")
    print(f"      (id={entry['id']:06d}, hash={entry['hash'][:16]}…)")


def cmd_weighted(spec: str, hidden: bool) -> None:
    items = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            sys.exit(f"权重项须是「名字:权重」：{part!r}")
        name, w = part.rsplit(":", 1)
        w = int(w)
        if w < 0:
            sys.exit("权重不能为负")
        items.append((name.strip(), w))
    total = sum(w for _, w in items)
    if total <= 0:
        sys.exit("权重总和须 > 0")
    [x] = rand_below(total, 1)
    acc, picked = 0, items[-1][0]
    for name, w in items:
        acc += w
        if x < acc:
            picked = name
            break
    entry = log_event("weighted", {"items": items, "picked": picked, "hidden": hidden})
    shown = "？（隐藏）" if hidden else picked
    print(f"[dice] 权重抽取 → {shown}")
    print(f"      (id={entry['id']:06d}, hash={entry['hash'][:16]}…)")


def cmd_verify() -> None:
    ok, n, msg = verify_chain()
    print(f"[dice] 审计链：{n} 条记录，{'✔ 完好' if ok else '✘ 损坏'} —— {msg}")
    sys.exit(0 if ok else 1)


def cmd_tail(n: int) -> None:
    if not LOG_PATH.exists():
        print("[dice] 审计链为空")
        return
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    for line in lines[-n:]:
        e = json.loads(line)
        print(f"#{e['id']:06d} {e['ts']} {e['kind']}: "
              f"{json.dumps(e['detail'], ensure_ascii=False)} hash={e['hash'][:12]}…")


# ---------------------------------------------------------------- 入口

def main() -> None:
    # Windows 控制台默认 GBK，强制 UTF-8 输出，避免「→」「✔」等字形损坏
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]

    # 通用开关
    hidden = "--hidden" in rest
    rest = [a for a in rest if a != "--hidden"]

    if cmd == "verify":
        cmd_verify()
    elif cmd == "tail":
        cmd_tail(int(rest[0]) if rest else 5)
    elif cmd == "judge":
        ap = argparse.ArgumentParser(prog="dice.py judge")
        ap.add_argument("-t", "--threshold", type=int, required=True, help="阈值 T（0~100）")
        ap.add_argument("--tier", choices=sorted(TIERS), default="normal",
                        help="骰档 wide/normal/hard（默认 normal）")
        ap.add_argument("--note", default="", help="判定名（公示用）")
        a = ap.parse_args(rest)
        cmd_judge(a.threshold, a.tier, hidden, a.note)
    elif cmd == "weighted":
        if not rest:
            sys.exit('用法：dice.py weighted "名字:权重,名字:权重"')
        cmd_weighted(rest[0], hidden)
    else:
        cmd_roll([cmd] + rest, hidden)


if __name__ == "__main__":
    main()
