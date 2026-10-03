"""推算引擎 + 持久层关键规则测试。运行：python -m pytest（若装了 pytest）
或：python tests/test_app.py（标准库 unittest 方式，无需任何依赖）。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.compute import PayloadError  # noqa: E402
from app import compute as C  # noqa: E402
from app import db            # noqa: E402

BASE_EVENTS = [
    {"id": "a", "title": "告警", "start_min": "2026-10-03T08:00",
     "start_max": "2026-10-03T08:30", "duration_minutes": 30},
    {"id": "b", "title": "定位", "start_min": "2026-10-03T08:00",
     "start_max": "2026-10-03T10:00", "duration_minutes": 60},
]
BASE_RELATIONS = [{"before": "a", "after": "b"}]


def payload(events=None, relations=None):
    import copy
    return {"events": copy.deepcopy(events if events is not None else BASE_EVENTS),
            "relations": copy.deepcopy(
                relations if relations is not None else BASE_RELATIONS)}


class ComputeTests(unittest.TestCase):

    def test_basic_propagation(self):
        r = C.compute(payload())
        self.assertEqual(r["status"], "ok")
        w = r["events"]["b"]["start_window"]
        # a 最早 08:00 开始、持续 30 分钟 => b 最早被推到 08:30
        self.assertEqual(w["min"], "2026-10-03T08:30")
        self.assertEqual(w["max"], "2026-10-03T10:00")
        # 依据链应包含输入、时长、关系三类步骤
        kinds = [s["kind"] for s in w["min_chain"]]
        self.assertEqual(kinds, ["input_lower", "duration", "relation"])
        # 结束窗口
        self.assertEqual(r["events"]["b"]["end_window"]["min"], "2026-10-03T09:30")

    def test_upper_bound_propagation(self):
        # a(30min) -> b；b 最晚 08:50 必须开始、b 输入上界就是 08:50
        # => a 的最晚开始由 08:30 被压到 08:20（可行：a 最早结束 08:30 <= 08:50）
        p = payload()
        p["events"][1]["start_max"] = "2026-10-03T08:50"
        r = C.compute(p)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["events"]["a"]["start_window"]["max"], "2026-10-03T08:20")
        self.assertEqual(r["events"]["b"]["start_window"]["min"], "2026-10-03T08:30")

    def test_relation_conflict(self):
        import copy
        p = payload()
        p["events"][0]["duration_minutes"] = 130  # a 最早 10:10 才结束 > b 最晚 10:00
        r = C.compute(p)
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflicts"][0]["kind"], "relation")
        self.assertEqual(set(r["conflicts"][0]["events"]), {"a", "b"})
        # 输入仍然保留
        self.assertEqual(r["events"]["a"]["duration_minutes"], 130)
        self.assertEqual(r["relations"][0]["status"], "conflict")
        self.assertTrue(r["events"]["a"]["conflict_indexes"])

    def test_input_range_conflict(self):
        p = payload()
        p["events"][0].update(start_min="2026-10-03T09:00",
                              start_max="2026-10-03T08:00")
        r = C.compute(p)
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflicts"][0]["kind"], "input_range")
        # 与之相关的关系暂不参与推算，而不是被当成关系冲突
        self.assertEqual(r["relations"][0]["status"], "skipped")
        self.assertTrue(r["relations"][0]["reason"])
        self.assertTrue(r["events"]["a"]["input_conflict"])

    def test_draft_event_and_skipped_relation(self):
        p = payload()
        p["events"].append({"id": "c", "title": "待定事件"})  # 什么都没填
        p["relations"].append({"before": "b", "after": "c"})
        r = C.compute(p)
        self.assertEqual(r["status"], "draft")
        self.assertFalse(r["conflicts"])
        self.assertTrue(r["events"]["c"]["draft"])
        self.assertIsNone(r["events"]["c"]["start_window"])
        self.assertIn("缺少", r["events"]["c"]["reason"])
        self.assertEqual(r["relations"][1]["status"], "skipped")
        self.assertIn("暂存草稿", r["relations"][1]["reason"])
        # 不相关的关系照常参与
        self.assertEqual(r["relations"][0]["status"], "ok")

    def test_draft_only_missing_one_field(self):
        p = payload()
        p["events"].append({"id": "c", "title": "只填了时间",
                            "start_min": "2026-10-03T10:00",
                            "start_max": "2026-10-03T11:00"})
        r = C.compute(p)
        self.assertTrue(r["events"]["c"]["draft"])
        self.assertIn("持续时长", r["events"]["c"]["reason"])

    def test_cycle(self):
        p = payload()
        p["relations"].append({"before": "b", "after": "a"})
        r = C.compute(p)
        self.assertEqual(r["status"], "conflict")
        cyc = [c for c in r["conflicts"] if c["kind"] == "cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertEqual(set(cyc[0]["events"]), {"a", "b"})
        # 闭环描述应首尾闭合
        self.assertIn("「告警」", cyc[0]["message"])
        self.assertTrue(all(x["status"] == "cycle" for x in r["relations"]))

    def test_three_node_chain_cumulative(self):
        p = payload()
        p["events"].append({"id": "c", "title": "恢复",
                            "start_min": "2026-10-03T08:00",
                            "start_max": "2026-10-03T12:00",
                            "duration_minutes": 20})
        p["relations"].append({"before": "b", "after": "c"})
        r = C.compute(p)
        w = r["events"]["c"]["start_window"]
        # a:30 + b:60 => 08:00 + 90min
        self.assertEqual(w["min"], "2026-10-03T09:30")

    def test_validation_errors(self):
        bad_cases = [
            ("not a dict", PayloadError),
            ({"events": [{"start_max": "x"}]}, PayloadError),
            ({"events": [{"id": "a"}, {"id": "a"}], "relations": []}, PayloadError),
            ({"events": [{"id": "a"}], "relations": [
                {"before": "a", "after": "b"}]}, PayloadError),
            ({"events": [{"id": "a"}], "relations": [
                {"before": "a", "after": "a"}]}, PayloadError),
            ({"events": [{"id": "a", "start_min": "2026-10-03T08:00",
                          "start_max": "2026-10-03T09:00",
                          "duration_minutes": -5}], "relations": []}, PayloadError),
        ]
        for body, exc in bad_cases:
            with self.assertRaises(exc):
                C.compute(body)

    def test_duplicate_relations_deduped(self):
        p = payload()
        p["relations"].append({"before": "a", "after": "b"})
        r = C.compute(p)
        self.assertEqual(len(r["relations"]), 1)

    def test_empty_payload_ok(self):
        r = C.compute({"events": [], "relations": []})
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["conflicts"], [])


class PersistenceTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = db.connect(self.tmp.name)
        db.init_db(self.conn, seed_demo=False)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_version_chain_and_payload_result_separation(self):
        rid = db.create_retro(self.conn, "t")
        p = payload()
        v1 = db.save_version(self.conn, rid, 0, p, C.compute(p))
        self.assertEqual(v1["version"], 1)
        self.assertIn("payload", v1)           # 原始输入
        self.assertIn("result", v1)            # 推算结果
        self.assertIsNot(v1["payload"], p)
        # 列中确实分开存
        row = self.conn.execute(
            "SELECT payload, result FROM versions WHERE retro_id=? AND version=1",
            (rid,)).fetchone()
        import json
        self.assertNotEqual(row["payload"], row["result"])
        self.assertEqual(json.loads(row["result"])["status"], "ok")

    def test_optimistic_concurrency(self):
        rid = db.create_retro(self.conn, "t")
        db.save_version(self.conn, rid, 0, payload(), C.compute(payload()))
        # 用户 A 已基于 v1 又保存成 v2
        db.save_version(self.conn, rid, 1, payload(), C.compute(payload()))
        # 用户 B 仍拿着 base_version=1 提交 → 拒绝
        with self.assertRaises(db.VersionConflict):
            db.save_version(self.conn, rid, 1, payload(), C.compute(payload()))
        latest = db.get_latest_version(self.conn, rid)
        self.assertEqual(latest["version"], 2)  # 没有被旧提交覆盖

    def test_conflict_version_cannot_finalize(self):
        rid = db.create_retro(self.conn, "t")
        p = payload()
        p["events"][0]["duration_minutes"] = 130
        db.save_version(self.conn, rid, 0, p, C.compute(p))
        with self.assertRaises(db.NotFinalizable):
            db.finalize_version(self.conn, rid, 1)
        self.assertFalse(db.get_retro(self.conn, rid)["is_final"])

    def test_finalize_ok_and_new_save_unfinalizes(self):
        rid = db.create_retro(self.conn, "t")
        db.save_version(self.conn, rid, 0, payload(), C.compute(payload()))
        db.finalize_version(self.conn, rid, 1)
        self.assertTrue(db.get_retro(self.conn, rid)["is_final"])
        # 定稿后继续编辑保存 => 产生新版本且自动取消定稿标记
        db.save_version(self.conn, rid, 1, payload(), C.compute(payload()))
        retro = db.get_retro(self.conn, rid)
        self.assertEqual(retro["current_version"], 2)
        self.assertFalse(retro["is_final"])

    def test_history_versions_listed(self):
        rid = db.create_retro(self.conn, "t")
        for i in range(3):
            db.save_version(self.conn, rid, i, payload(), C.compute(payload()))
        vs = db.list_versions(self.conn, rid)
        self.assertEqual([v["version"] for v in vs], [3, 2, 1])

    def test_seed_demo(self):
        tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp2.close()
        c2 = db.connect(tmp2.name)
        db.init_db(c2, seed_demo=True)
        retros = db.list_retros(c2)
        self.assertEqual(len(retros), 1)
        v = db.get_latest_version(c2, retros[0]["id"])
        self.assertGreaterEqual(len(v["payload"]["events"]), 4)
        self.assertEqual(v["result"]["status"], "draft")
        c2.close()
        os.unlink(tmp2.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
