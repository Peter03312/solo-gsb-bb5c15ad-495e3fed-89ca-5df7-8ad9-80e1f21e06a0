"""时间窗推算引擎（纯函数，不依赖数据库与网络，便于单元测试）。

业务规则
--------
* 每个事件可给出「最早开始 / 最晚开始」时间范围与持续分钟数；缺任一项的事件为
  *暂存草稿*，保留输入但其关联关系不参与推算。
* 「先于」关系 before -> after 要求：before 的结束时刻 <= after 的开始时刻，
  即 after.start >= before.start + duration(before)。
* 在输入范围之上做最紧边界传播（DAG 上的最长/短路），得到每个事件可行的
  开始/结束时间窗，并为每条边界保留可读的「依据链」。
* 约束冲突（输入范围自相矛盾、先于关系无法满足、关系成环）时保留全部输入，
  result.status = "conflict"，冲突版本不得定稿。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


class PayloadError(ValueError):
    """输入结构/格式不合法（对应 HTTP 400）。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _parse_dt(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"{field} 必须是 ISO 时间字符串（如 2026-10-03T08:00）")
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise PayloadError(f"{field} 时间格式无法解析：{value!r}，应形如 2026-10-03T08:00")


def _fmt_dt(dt: datetime) -> str:
    if dt.second == 0 and dt.microsecond == 0:
        return dt.strftime("%Y-%m-%dT%H:%M")
    return dt.isoformat()


def _as_duration(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadError(f"{field} 必须是非负数字（分钟）")
    f = float(value)
    if f != f:  # NaN
        raise PayloadError(f"{field} 不能是 NaN")
    if f < 0:
        raise PayloadError(f"{field} 不能为负数")
    return f


def _name(event: dict) -> str:
    title = event.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return str(event["id"])


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

def validate_payload(payload: Any) -> dict:
    """校验并返回规整后的 payload。结构性错误抛 PayloadError。"""
    if not isinstance(payload, dict):
        raise PayloadError("请求体必须是 JSON 对象")

    raw_events = payload.get("events", [])
    raw_relations = payload.get("relations", [])
    if not isinstance(raw_events, list):
        raise PayloadError("events 必须是数组")
    if not isinstance(raw_relations, list):
        raise PayloadError("relations 必须是数组")

    events: list[dict] = []
    seen_ids: set[str] = set()
    for i, ev in enumerate(raw_events):
        if not isinstance(ev, dict):
            raise PayloadError(f"events[{i}] 必须是对象")
        ev_id = ev.get("id")
        if not isinstance(ev_id, str) or not ev_id.strip():
            raise PayloadError(f"events[{i}] 缺少非空字符串 id")
        ev_id = ev_id.strip()
        if ev_id in seen_ids:
            raise PayloadError(f"事件 id 重复：{ev_id}")
        seen_ids.add(ev_id)

        title = ev.get("title", "")
        if title is not None and not isinstance(title, str):
            raise PayloadError(f"事件 {ev_id} 的 title 必须是字符串")

        item = {"id": ev_id, "title": title or ""}

        for field in ("start_min", "start_max"):
            val = ev.get(field)
            if val is not None and val != "":
                item[field] = _parse_dt(val, f"事件 {ev_id} 的 {field}")
            else:
                item[field] = None

        dur = ev.get("duration_minutes")
        if dur is None or dur == "":
            item["duration_minutes"] = None
        else:
            item["duration_minutes"] = _as_duration(dur, f"事件 {ev_id} 的 duration_minutes")

        note = ev.get("note", "")
        item["note"] = note if isinstance(note, str) else ""
        events.append(item)

    ids = {e["id"] for e in events}
    relations: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, rel in enumerate(raw_relations):
        if not isinstance(rel, dict):
            raise PayloadError(f"relations[{i}] 必须是对象")
        before, after = rel.get("before"), rel.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise PayloadError(f"relations[{i}] 的 before/after 必须是事件 id 字符串")
        before, after = before.strip(), after.strip()
        if before not in ids:
            raise PayloadError(f"relations[{i}] 的 before 引用了不存在的事件：{before}")
        if after not in ids:
            raise PayloadError(f"relations[{i}] 的 after 引用了不存在的事件：{after}")
        if before == after:
            raise PayloadError(f"事件 {before} 不能先于自身")
        pair = (before, after)
        if pair in seen_pairs:
            continue  # 完全重复的关系静默去重
        seen_pairs.add(pair)
        note = rel.get("note", "")
        relations.append({"before": before, "after": after,
                          "note": note if isinstance(note, str) else ""})

    # 时区混用会导致时刻不可比较
    aware = [e["start_min"] for e in events if e["start_min"] is not None
             and e["start_min"].tzinfo is not None]
    naive = [e["start_min"] for e in events if e["start_min"] is not None
             and e["start_min"].tzinfo is None]
    if aware and naive:
        raise PayloadError("时间不能混用「带时区」与「不带时区」两种写法")

    return {"events": events, "relations": relations}


# --------------------------------------------------------------------------- #
# 依据链步骤
# --------------------------------------------------------------------------- #

def _step_input_lower(ev: dict) -> dict:
    return {"kind": "input_lower", "event": ev["id"],
            "at": _fmt_dt(ev["start_min"]),
            "text": f"「{_name(ev)}」最早开始输入为 {_fmt_dt(ev['start_min'])}"}


def _step_input_upper(ev: dict) -> dict:
    return {"kind": "input_upper", "event": ev["id"],
            "at": _fmt_dt(ev["start_max"]),
            "text": f"「{_name(ev)}」最晚开始输入为 {_fmt_dt(ev['start_max'])}"}


def _step_duration(ev: dict, reverse: bool = False) -> dict:
    dur = ev["duration_minutes"]
    label = str(int(dur)) if float(dur).is_integer() else str(dur)
    if reverse:
        text = (f"由「{_name(ev)}」的结束时刻反推其开始时刻：减去持续的 {label} 分钟")
    else:
        text = (f"「{_name(ev)}」持续 {label} 分钟（结束 = 开始 + {label} 分钟）")
    return {"kind": "duration", "event": ev["id"], "minutes": dur, "text": text}


def _step_relation(rel: dict, names: dict) -> dict:
    return {"kind": "relation", "before": rel["before"], "after": rel["after"],
            "text": f"「{names[rel['before']]}」先于「{names[rel['after']]}」"
                    f"（{names[rel['after']]} 开始 ≥ {names[rel['before']]} 结束）"}


# --------------------------------------------------------------------------- #
# 主推算流程
# --------------------------------------------------------------------------- #

def compute(raw_payload: Any) -> dict:
    """根据原始输入推算时间窗。返回与输入分离保存的结果对象。"""
    payload = validate_payload(raw_payload)
    events = payload["events"]
    relations = payload["relations"]
    names = {e["id"]: _name(e) for e in events}
    by_id = {e["id"]: e for e in events}

    result_events: dict[str, dict] = {}
    conflicts: list[dict] = []
    active: set[str] = set()

    # 1) 分类：草稿 / 输入范围冲突 / 可参与推算
    for ev in events:
        missing = []
        if ev["start_min"] is None:
            missing.append("最早开始时间")
        if ev["start_max"] is None:
            missing.append("最晚开始时间")
        if ev["duration_minutes"] is None:
            missing.append("持续时长")

        entry: dict[str, Any] = {
            "id": ev["id"], "title": ev["title"], "draft": False,
            "input_conflict": False, "reason": None,
            "duration_minutes": ev["duration_minutes"],
            "start_window": None, "end_window": None, "conflict_indexes": [],
        }

        if missing:
            entry["draft"] = True
            entry["reason"] = "暂存草稿：缺少" + "、".join(missing) + "，关联关系暂不参与推算"
            result_events[ev["id"]] = entry
            continue

        if ev["start_min"] > ev["start_max"]:
            entry["input_conflict"] = True
            entry["reason"] = "输入的最早开始时间晚于最晚开始时间"
            idx = len(conflicts)
            entry["conflict_indexes"].append(idx)
            conflicts.append({
                "kind": "input_range", "events": [ev["id"]],
                "message": f"「{names[ev['id']]}」输入的时间范围自相矛盾："
                           f"最早开始 {_fmt_dt(ev['start_min'])} 晚于 "
                           f"最晚开始 {_fmt_dt(ev['start_max'])}",
                "chain": [_step_input_lower(ev), _step_input_upper(ev)],
            })
            result_events[ev["id"]] = entry
            continue

        active.add(ev["id"])
        result_events[ev["id"]] = entry

    # 2) 关系过滤：端点非活跃（草稿/冲突）的关系暂不参与推算
    rel_view: list[dict] = []
    active_edges: list[dict] = []
    for rel in relations:
        view = {"before": rel["before"], "after": rel["after"], "note": rel["note"],
                "status": "ok", "reason": None, "conflict_indexes": []}
        drafts = [eid for eid in (rel["before"], rel["after"])
                  if result_events[eid]["draft"]]
        bads = [eid for eid in (rel["before"], rel["after"])
                if result_events[eid]["input_conflict"]]
        if drafts:
            view["status"] = "skipped"
            view["reason"] = "暂不参与推算：" + "、".join(
                f"「{names[d]}」为暂存草稿" for d in drafts)
        elif bads:
            view["status"] = "skipped"
            view["reason"] = "暂不参与推算：" + "、".join(
                f"「{names[d]}」输入时间范围存在冲突" for d in bads)
        else:
            active_edges.append(rel)
        rel_view.append(view)

    # 3) 关系成环检测：不断找出一个环、登记冲突、把环上的边移出传播集合
    rel_step_of = {id(rel): _step_relation(rel, names) for rel in relations}
    edge_view_of = {(v["before"], v["after"]): v for v in rel_view}
    propagatable = _remove_cycles(
        active_edges, active, names, result_events, conflicts,
        rel_step_of, edge_view_of)

    # 4) 边界传播（DAG）。下界 = 最长路径；上界 = 取最紧
    #    L[x]: 可行的最早开始（只能被推后）  U[x]: 可行的最晚开始（只能被提前）
    L: dict[str, tuple[datetime, list]] = {}
    U: dict[str, tuple[datetime, list]] = {}
    for eid in active:
        ev = by_id[eid]
        L[eid] = (ev["start_min"], [_step_input_lower(ev)])
        U[eid] = (ev["start_max"], [_step_input_upper(ev)])

    n = len(active)
    for _ in range(max(1, n) + 2):
        changed = False
        for rel in propagatable:
            a, b = rel["before"], rel["after"]
            dur = timedelta(minutes=by_id[a]["duration_minutes"])
            rstep = rel_step_of[id(rel)]

            nl = L[a][0] + dur
            if nl > L[b][0] and len(L[a][1]) < n + 1:
                L[b] = (nl, L[a][1] + [_step_duration(by_id[a]), rstep])
                changed = True

            # 上界链读法：b 最晚何时开始（输入/或由其后继推出），
            # 再经「先于关系」反推 a 的结束上限，再减去 a 的时长得到 a 开始上限
            nu = U[b][0] - dur
            if nu < U[a][0] and len(U[b][1]) < n + 1:
                U[a] = (nu, U[b][1] + [rstep, _step_duration(by_id[a], reverse=True)])
                changed = True
        if not changed:
            break

    # 5) 关系可行性检查：before 最早结束 <= after 最晚开始
    for rel in propagatable:
        a, b = rel["before"], rel["after"]
        dur = timedelta(minutes=by_id[a]["duration_minutes"])
        if L[a][0] + dur > U[b][0]:
            idx = len(conflicts)
            chain = (L[a][1] + [_step_duration(by_id[a]), rel_step_of[id(rel)]]
                     + U[b][1])
            message = (
                f"「{names[a]}」最早可能在 {_fmt_dt(L[a][0] + dur)} 结束，"
                f"但「{names[b]}」最晚 {_fmt_dt(U[b][0])} 必须开始，"
                f"「先于」约束无法满足（要求 {names[a]} 结束 ≤ {names[b]} 开始）")
            conflicts.append({"kind": "relation", "events": [a, b],
                              "before": a, "after": b,
                              "message": message, "chain": chain})
            view = edge_view_of[(a, b)]
            view["status"] = "conflict"
            view["conflict_indexes"].append(idx)
            result_events[a]["conflict_indexes"].append(idx)
            result_events[b]["conflict_indexes"].append(idx)

    # 6) 组装每个活跃事件的可行开始/结束窗口
    for eid in active:
        ev = by_id[eid]
        entry = result_events[eid]
        dur = timedelta(minutes=ev["duration_minutes"])
        dur_step = _step_duration(ev)
        entry["start_window"] = {
            "min": _fmt_dt(L[eid][0]), "max": _fmt_dt(U[eid][0]),
            "min_chain": L[eid][1], "max_chain": U[eid][1],
        }
        entry["end_window"] = {
            "min": _fmt_dt(L[eid][0] + dur), "max": _fmt_dt(U[eid][0] + dur),
            "min_chain": L[eid][1] + [dur_step],
            "max_chain": U[eid][1] + [dur_step],
        }

    has_draft = any(e["draft"] for e in result_events.values())
    status = "conflict" if conflicts else ("draft" if has_draft else "ok")
    return {
        "status": status,
        "events": result_events,
        "relations": rel_view,
        "conflicts": conflicts,
    }


def _remove_cycles(edges, active, names, result_events, conflicts,
                   rel_step_of, edge_view_of) -> list[dict]:
    """用 DFS 反复找出有向环；环上的边登记为冲突并从传播集合中剔除。

    先于关系成环意味着 A 必须在 B 之前结束、B 又必须在 A 之前结束，
    时间上不可能成立。
    """
    remaining = list(edges)
    while True:
        adj: dict[str, list[str]] = {e: [] for e in active}
        edge_of_pair: dict[tuple[str, str], dict] = {}
        for rel in remaining:
            adj[rel["before"]].append(rel["after"])
            edge_of_pair[(rel["before"], rel["after"])] = rel

        color: dict[str, int] = {e: 0 for e in active}  # 0 白 1 灰 2 黑
        stack: list[str] = []
        cycle_pairs: list[tuple[str, str]] | None = None

        def dfs(u: str) -> bool:
            nonlocal cycle_pairs
            color[u] = 1
            stack.append(u)
            for v in adj[u]:
                if color[v] == 0:
                    if dfs(v):
                        return True
                elif color[v] == 1:
                    start = stack.index(v)
                    nodes = stack[start:] + [v]
                    cycle_pairs = [(nodes[i], nodes[i + 1])
                                   for i in range(len(nodes) - 1)]
                    return True
            stack.pop()
            color[u] = 2
            return False

        found = False
        for e in active:
            if color[e] == 0 and dfs(e):
                found = True
                break
        if not found:
            return remaining

        idx = len(conflicts)
        loop_nodes = [p[0] for p in cycle_pairs] + [cycle_pairs[-1][1]]
        chain = [rel_step_of[id(edge_of_pair[p])] for p in cycle_pairs]
        loop_text = " → ".join(f"「{names[x]}」" for x in loop_nodes)
        conflicts.append({
            "kind": "cycle", "events": list(dict.fromkeys(loop_nodes)),
            "message": f"「先于」关系形成闭环：{loop_text}，时间上不可能同时成立",
            "chain": chain,
        })
        cycle_set = set(cycle_pairs)
        kept = []
        for rel in remaining:
            if (rel["before"], rel["after"]) in cycle_set:
                view = edge_view_of[(rel["before"], rel["after"])]
                view["status"] = "cycle"
                view["conflict_indexes"].append(idx)
                for eid in (rel["before"], rel["after"]):
                    result_events[eid]["conflict_indexes"].append(idx)
            else:
                kept.append(rel)
        remaining = kept
