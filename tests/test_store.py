"""Business invariants; tests use only temporary fixture ledgers."""
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from store import Store, STORE_DOCUMENT_PREFIX


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ("domain_run_" + uuid4().hex)
        self.root.mkdir()
        self.store = Store(self.root)
        self.day = "2026-10-25"
        self.a = self.run_action("store.save", name="甲店")["id"]
        self.b = self.run_action("store.save", name="乙店")["id"]

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root)

    def run_action(self, action_name, **data):
        return self.store.action(action_name, data, str(uuid4()))

    def batch(self, kind, count=10, prefix=None, **extra):
        return self.run_action("batch.create", kind=kind, date=self.day, serials=[f"{prefix or kind}-{i:04d}" for i in range(count)], **extra)

    def claim(self, kind, ids):
        return self.run_action("component.outbound", kind=kind, ids=ids, purpose="assembly", date=self.day)

    def vehicle(self):
        p=self.batch("pcba", 1)
        f=self.batch("frame", 1)
        self.claim("pcba", p["component_ids"])
        self.claim("frame", f["component_ids"])
        v=self.run_action("vehicle.assemble", frame_id=f["component_ids"][0], pcba_id=p["component_ids"][0], date=self.day)
        return v["id"], p["component_ids"][0], f["component_ids"][0]

    def test_store_code_persists_and_partial_edits_preserve_hidden_fields(self):
        result = self.run_action("store.save", name="门店原名", code="  000Ab  ",
            contact="原联系人", phone="010-0000", address="原地址", notes="原资料")
        ident = result["id"]
        self.run_action("store.save", id=ident, name="门店新名")
        row = next(s for s in self.store.state()["stores"] if s["id"] == ident)
        self.assertEqual(row["code"], "000Ab")
        self.assertEqual((row["contact"], row["phone"], row["address"], row["notes"]),
                         ("原联系人", "010-0000", "原地址", "原资料"))
        self.run_action("store.save", id=ident, code="000009")
        self.run_action("store.save", id=ident, contact="")
        self.store.close()
        self.store = Store(self.root)
        row = next(s for s in self.store.state()["stores"] if s["id"] == ident)
        self.assertEqual((row["name"], row["code"], row["contact"]), ("门店新名", "000009", ""))
        self.assertEqual((row["phone"], row["address"], row["notes"]), ("010-0000", "原地址", "原资料"))
        self.assertFalse(row["has_document"])
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_store_codes_are_exact_unique_and_can_be_cleared(self):
        self.run_action("store.save", id=self.a, code="000A")
        self.run_action("store.save", id=self.a, code="000A")
        self.run_action("store.save", id=self.b, code="000a")
        with self.assertRaisesRegex(ValueError, "编码已被"):
            self.run_action("store.save", id=self.b, name="不应保存的改名", code="000A")
        self.assertEqual(self.store._get("stores", self.b)["name"], "乙店")
        before = len(self.store.state()["stores"])
        with self.assertRaisesRegex(ValueError, "编码已被"):
            self.run_action("store.save", name="不应创建", code="000A")
        self.assertEqual(len(self.store.state()["stores"]), before)
        self.run_action("store.save", id=self.a, code="")
        self.run_action("store.save", id=self.b, code="000A")
        self.run_action("store.save", name="空编码门店一", code="")
        self.run_action("store.save", name="空编码门店二", code="  ")
        state = {s["id"]: s for s in self.store.state()["stores"]}
        self.assertEqual(state[self.a]["code"], "")
        self.assertEqual(state[self.b]["code"], "000A")
        for code in (123, "x" * 121, "bad\x00code"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.run_action("store.save", id=self.b, code=code)
        self.assertEqual(next(s for s in self.store.state()["stores"] if s["id"] == self.b)["code"], "000A")

    def test_two_connections_cannot_assign_same_store_code(self):
        second = Store(self.root)
        barrier = threading.Barrier(2)
        results = []
        def execute(store, store_id):
            barrier.wait()
            try:
                store.action("store.save", {"id": store_id, "code": "000-shared"}, str(uuid4()))
                results.append("ok")
            except ValueError:
                results.append("rejected")
        threads = [threading.Thread(target=execute, args=(store, store_id))
                   for store, store_id in ((self.store, self.a), (second, self.b))]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertCountEqual(results, ["ok", "rejected"])
            self.assertEqual(sum(s["code"] == "000-shared" for s in self.store.state()["stores"]), 1)
        finally:
            second.close()

    def test_store_document_metadata_is_private_and_not_an_ordinary_credential(self):
        self.store.db.execute("INSERT INTO credentials VALUES(?,?,?,?,?)",
            (STORE_DOCUMENT_PREFIX + self.a, self.a, "私密门店文档", "ciphertext-private-doc", self.day))
        self.store.db.execute("INSERT INTO credentials VALUES(?,?,?,?,?)",
            (STORE_DOCUMENT_PREFIX + self.b, self.a, "归属不匹配的文档", "ciphertext-other", self.day))
        self.store.db.execute("INSERT INTO credentials VALUES(?,?,?,?,?)",
            ("normal-credential", self.a, "路由器", "ciphertext-password", self.day))
        self.store.db.commit()
        state = self.store.state()
        stores = {s["id"]: s for s in state["stores"]}
        self.assertTrue(stores[self.a]["has_document"])
        self.assertFalse(stores[self.b]["has_document"])
        self.assertEqual([c["id"] for c in state["credentials"]], ["normal-credential"])
        self.assertNotIn("ciphertext-", json.dumps(state))
        self.assertNotIn("私密门店文档", json.dumps(state, ensure_ascii=False))

    def test_ten_in_three_assembled_then_shipped_remains_seven(self):
        p=self.batch("pcba"); f=self.batch("frame")
        self.claim("pcba",p["component_ids"][:3]);self.claim("frame",f["component_ids"][:3])
        cars=[]
        for i in range(3):
            cars.append(self.run_action("vehicle.assemble",frame_id=f["component_ids"][i],pcba_id=p["component_ids"][i],date=self.day)["id"])
        self.assertEqual(self.store.state()["summary"]["pcba_available"],7)
        shipment=self.run_action("vehicle.ship",ids=cars,store_id=self.a,date=self.day)
        state=self.store.state()
        self.assertEqual(state["summary"]["pcba_available"],7)
        self.assertEqual(state["summary"]["frame_available"],7)
        self.assertEqual(state["summary"]["shipped"],3)
        self.assertEqual(len(state["shipments"][0]["items"]),3)
        self.assertEqual(state["shipments"][0]["id"],shipment["id"])

    def test_intake_and_cash_are_independent(self):
        self.batch("pcba", supplier="旧页面供应商", cost_cents=123400, paid_cents=0)
        self.batch("frame")
        state=self.store.state()
        self.assertEqual(len(state["ledger"]),0)
        self.assertEqual(state["summary"]["expense_cents"],0)
        self.assertTrue(all(b["cost_cents"] == 0 and b["supplier"] == "" for b in state["batches"]))
        self.run_action("ledger.save",direction="expense",category="PCBA采购",amount_cents=50000,notes="单独记录实际付款")
        self.assertEqual(self.store.state()["summary"]["expense_cents"],50000)
        self.assertEqual(self.store.state()["summary"]["pcba_available"],10)

    def test_legacy_paid_intake_is_rejected_before_any_write(self):
        for paid in (1, -1, 50000, "50000"):
            with self.subTest(paid=paid), self.assertRaisesRegex(ValueError,"入库不再记账"):
                self.batch("pcba",1,paid_cents=paid)
        state=self.store.state()
        self.assertEqual(state["batches"],[])
        self.assertEqual(state["components"],[])
        self.assertEqual(state["ledger"],[])

    def test_serials_preserve_zero_and_type_scope_and_rollback(self):
        p=self.run_action("batch.create",kind="pcba",supplier="供应商",date=self.day,cost_cents=1,serials=["00001"])
        self.run_action("batch.create",kind="frame",supplier="供应商",date=self.day,cost_cents=1,serials=["00001"])
        with self.assertRaisesRegex(ValueError,"编号已存在"):
            self.run_action("batch.create",kind="pcba",date=self.day,serials=["00002","00001"])
        state=self.store.state()
        self.assertEqual(len(state["components"]),2)
        self.assertEqual(len(state["batches"]),2)
        self.assertEqual(len(state["ledger"]),0)
        self.assertEqual(self.store._component(p["component_ids"][0])["serial"],"00001")

    def test_idempotency_and_request_collision(self):
        data=dict(kind="pcba",date=self.day,serials=["001"])
        a=self.store.action("batch.create",data,"same-request")
        b=self.store.action("batch.create",data,"same-request")
        self.assertEqual(a,b)
        self.assertEqual(len(self.store.state()["ledger"]),0)
        with self.assertRaisesRegex(ValueError,"请求编号"):
            self.store.action("batch.create",{**data,"serials":["002"]},"same-request")

    def test_batch_outbound_all_or_nothing_and_no_duplicate(self):
        p=self.batch("pcba",3)
        self.claim("pcba",p["component_ids"][:1])
        with self.assertRaises(ValueError):
            self.claim("pcba",p["component_ids"])
        self.assertEqual(self.store.state()["summary"]["pcba_available"],2)
        with self.assertRaisesRegex(ValueError,"重复编号"):
            self.claim("pcba",[p["component_ids"][1]]*2)

    def test_two_connections_cannot_outbound_same_component(self):
        p=self.batch("pcba",1)
        second=Store(self.root)
        gate=threading.Barrier(2)
        results=[]
        def execute(store):
            gate.wait()
            try:
                store.action("component.outbound",dict(kind="pcba",ids=p["component_ids"],purpose="assembly",date=self.day),str(uuid4()))
                results.append("ok")
            except ValueError:
                results.append("rejected")
        threads=[threading.Thread(target=execute,args=(s,)) for s in (self.store,second)]
        for t in threads:t.start()
        for t in threads:t.join(20)
        second.close()
        self.assertCountEqual(results,["ok","rejected"])
        self.assertEqual(self.store.state()["summary"]["claimed"],1)

    def test_pairing_requires_claim_and_unique_pcba(self):
        p=self.batch("pcba",1);f=self.batch("frame",2)
        with self.assertRaisesRegex(ValueError,"先领用"):
            self.run_action("vehicle.assemble",frame_id=f["component_ids"][0],pcba_id=p["component_ids"][0])
        self.claim("pcba",p["component_ids"]);self.claim("frame",f["component_ids"])
        self.run_action("vehicle.assemble",frame_id=f["component_ids"][0],pcba_id=p["component_ids"][0])
        with self.assertRaisesRegex(ValueError,"已绑定"):
            self.run_action("vehicle.assemble",frame_id=f["component_ids"][1],pcba_id=p["component_ids"][0])

    def test_spare_replace_history_and_transfer_without_second_outbound(self):
        v,old,frame=self.vehicle()
        original=self.run_action("vehicle.ship",ids=[v],store_id=self.a,date=self.day)
        p=self.batch("pcba",2,prefix="new")
        self.run_action("component.outbound",kind="pcba",ids=p["component_ids"],purpose="spare",store_id=self.a,date=self.day)
        available=self.store.state()["summary"]["pcba_available"]
        self.run_action("vehicle.replace",id=v,pcba_id=p["component_ids"][0],old_disposition="repair",date=self.day,notes="测试换板")
        self.assertEqual(self.store._component(old)["status"],"repair")
        self.assertEqual(self.store.state()["summary"]["pcba_available"],available)
        self.run_action("vehicle.transfer",ids=[v],store_id=self.b,date=self.day)
        self.assertEqual(self.store._get("vehicles",v)["store_id"],self.b)
        self.assertEqual(self.store._component(p["component_ids"][0])["store_id"],self.b)
        history=self.store.state()["history"]
        self.assertIn("pcba_replaced",[h["event"] for h in history])
        self.assertIn("transferred",[h["event"] for h in history])
        with self.assertRaisesRegex(ValueError,"后续操作"):
            self.run_action("shipment.void",id=original["id"],reason="不能覆盖后续业务")

    def test_cross_store_spare_requires_transfer(self):
        v,_,_=self.vehicle()
        self.run_action("vehicle.ship",ids=[v],store_id=self.a)
        p=self.batch("pcba",1,prefix="spare")
        self.run_action("component.outbound",kind="pcba",ids=p["component_ids"],purpose="spare",store_id=self.b)
        with self.assertRaisesRegex(ValueError,"跨店"):
            self.run_action("vehicle.replace",id=v,pcba_id=p["component_ids"][0],old_disposition="repair",notes="替换")
        self.run_action("component.transfer",ids=p["component_ids"],store_id=self.a)
        self.run_action("vehicle.replace",id=v,pcba_id=p["component_ids"][0],old_disposition="spare",notes="替换")

    def test_spare_pairing_same_store_only(self):
        p=self.batch("pcba",1);f=self.batch("frame",1)
        self.run_action("component.outbound",kind="pcba",ids=p["component_ids"],purpose="spare",store_id=self.a)
        self.run_action("component.outbound",kind="frame",ids=f["component_ids"],purpose="spare",store_id=self.b)
        with self.assertRaisesRegex(ValueError,"一家门店"):
            self.run_action("vehicle.assemble",frame_id=f["component_ids"][0],pcba_id=p["component_ids"][0])
        self.run_action("component.transfer",ids=p["component_ids"],store_id=self.b)
        v=self.run_action("vehicle.assemble",frame_id=f["component_ids"][0],pcba_id=p["component_ids"][0])
        self.assertEqual(self.store._get("vehicles",v["id"])["status"],"shipped")

    def test_shipment_void_restores_and_keeps_original(self):
        v,pcba,frame=self.vehicle()
        shipment=self.run_action("vehicle.ship",ids=[v],store_id=self.a)
        self.run_action("shipment.void",id=shipment["id"],reason="门店选错")
        self.assertEqual(self.store._get("vehicles",v)["status"],"ready")
        self.assertIsNone(self.store._component(pcba)["store_id"])
        self.assertEqual(self.store.state()["shipments"][0]["voided"],1)
        self.assertIn("shipment_void",[h["event"] for h in self.store.state()["history"]])
        self.run_action("vehicle.ship",ids=[v],store_id=self.b)
        self.assertEqual(len(self.store.state()["shipments"]),2)

    def test_spare_shipment_void_rejected_after_return(self):
        p=self.batch("pcba",1)
        shipment=self.run_action("component.outbound",kind="pcba",ids=p["component_ids"],purpose="spare",store_id=self.a)
        self.run_action("component.return",id=p["component_ids"][0])
        with self.assertRaisesRegex(ValueError,"后续操作"):
            self.run_action("shipment.void",id=shipment["id"],reason="测试")

    def test_disassemble_reassemble_same_identity(self):
        v,p,f=self.vehicle()
        self.run_action("vehicle.disassemble",id=v,notes="重新装配")
        self.assertEqual(self.store._get("vehicles",v)["status"],"disassembled")
        self.assertEqual(self.store._component(p)["status"],"claimed")
        result=self.run_action("vehicle.assemble",frame_id=f,pcba_id=p)
        self.assertEqual(result["id"],v)
        self.assertEqual(len(self.store.state()["history"]),3)

    def test_ledger_edit_void_and_money_validation(self):
        with self.assertRaisesRegex(ValueError,"整数分"):
            self.run_action("ledger.save",direction="income",category="甲方回款",amount_cents=1.5)
        entry=self.run_action("ledger.save",direction="income",category="甲方回款",amount_cents=100,notes="首笔回款")
        self.run_action("ledger.save",id=entry["id"],direction="income",category="甲方回款",amount_cents=200,notes="回款更正")
        self.run_action("ledger.void",id=entry["id"],reason="错误录入")
        self.assertEqual(self.store.state()["summary"]["income_cents"],0)
        self.assertEqual(len(self.store.state()["ledger"]),1)

    def test_minimal_ledger_fields_and_category_choices(self):
        entry=self.run_action("ledger.save",direction="income",category="甲方回款",amount_cents=100,date=self.day,
                              counterparty="忽略",purpose="忽略",store_id=self.a,batch_id="不再接受的新关联")
        row=self.store._get("ledger",entry["id"])
        self.assertEqual((row["counterparty"],row["purpose"],row["store_id"],row["batch_id"]),("","",None,None))
        self.assertTrue(self.store.state()["ledger"][0]["notes_edited"])
        for category in ("PCBA采购","结构件采购","研发耗材","吃饭","差旅","出货耗材"):
            self.run_action("ledger.save",direction="expense",category=category,amount_cents=1)
        for direction,category in (("income","供应商退款"),("income","差旅"),("expense","甲方回款"),("expense","自定义类别")):
            with self.subTest(direction=direction,category=category), self.assertRaisesRegex(ValueError,"分类"):
                self.run_action("ledger.save",direction=direction,category=category,amount_cents=1)

    def test_legacy_ledger_edit_preserves_hidden_history_and_unchanged_category(self):
        batch=self.batch("pcba",1)
        ident=str(uuid4())
        self.store._insert("ledger",dict(id=ident,direction="income",date=self.day,category="整车销售",amount_cents=100,
            counterparty="原往来方",purpose="原用途",store_id=self.a,batch_id=batch["id"],notes="原备注",created_at=self.day))
        self.store.db.commit()
        self.assertFalse(next(l for l in self.store.state()["ledger"] if l["id"] == ident)["notes_edited"])
        self.run_action("ledger.save",id=ident,direction="income",date=self.day,category="整车销售",amount_cents=200,
            notes="新的备注",counterparty="不要覆盖",purpose="不要覆盖",store_id=self.b,batch_id=None)
        row=self.store._get("ledger",ident)
        self.assertEqual((row["counterparty"],row["purpose"],row["store_id"],row["batch_id"]),("原往来方","原用途",self.a,batch["id"]))
        self.assertEqual((row["notes"],row["amount_cents"]),("新的备注",200))
        self.assertTrue(next(l for l in self.store.state()["ledger"] if l["id"] == ident)["notes_edited"])
        with self.assertRaisesRegex(ValueError,"分类"):
            self.run_action("ledger.save",id=ident,direction="expense",category="整车销售",amount_cents=200)
        self.run_action("ledger.save",id=ident,direction="income",category="甲方回款",amount_cents=200,notes="改为现有分类")
        self.assertEqual(self.store._get("ledger",ident)["category"],"甲方回款")

    def test_edited_legacy_notes_can_be_cleared_and_remain_authoritative_after_restart(self):
        ident=str(uuid4())
        self.store._insert("ledger",dict(id=ident,direction="income",date=self.day,category="整车销售",amount_cents=100,
            counterparty="历史往来方",purpose="历史用途不应重新冒出来",store_id=None,batch_id=None,notes="历史备注",created_at=self.day))
        self.store.db.commit()
        self.run_action("ledger.save",id=ident,direction="income",category="整车销售",amount_cents=100,notes="用户新备注")
        self.run_action("ledger.save",id=ident,direction="income",category="整车销售",amount_cents=100,notes="")
        self.store.close()
        self.store=Store(self.root)
        row=next(l for l in self.store.state()["ledger"] if l["id"] == ident)
        self.assertEqual(row["notes"],"")
        self.assertTrue(row["notes_edited"])
        self.assertEqual(row["purpose"],"历史用途不应重新冒出来")
        self.assertEqual(self.store.db.execute("SELECT value FROM settings WHERE key=?",("ledger.notes-edited:"+ident,)).fetchone()[0],"1")

    def test_note_edit_marker_rolls_back_with_failed_ledger_save(self):
        ident=str(uuid4())
        self.store._insert("ledger",dict(id=ident,direction="income",date=self.day,category="整车销售",amount_cents=100,
            counterparty="",purpose="保留旧用途",store_id=None,batch_id=None,notes="旧备注",created_at=self.day))
        self.store.db.commit()
        original=self.store._insert
        def fail_request(table,values):
            if table == "requests":
                raise RuntimeError("simulated request write failure")
            return original(table,values)
        with patch.object(self.store,"_insert",side_effect=fail_request):
            with self.assertRaisesRegex(RuntimeError,"simulated"):
                self.run_action("ledger.save",id=ident,direction="income",category="整车销售",amount_cents=100,notes="未提交备注")
        row=next(l for l in self.store.state()["ledger"] if l["id"] == ident)
        self.assertEqual(row["notes"],"旧备注")
        self.assertFalse(row["notes_edited"])

    def test_persistence_and_no_secret_leak(self):
        self.batch("pcba",1)
        self.store.db.execute("INSERT INTO credentials VALUES(?,?,?,?,?)",("cred",self.a,"路由器","ciphertext-SECRET","2026-10-25"))
        self.store.db.commit()
        self.store.close()
        self.store=Store(self.root)
        result=self.store.state()
        self.assertEqual(result["summary"]["pcba_available"],1)
        self.assertNotIn("ciphertext-SECRET",json.dumps(result))
        self.assertNotIn("payload",result["credentials"][0])
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0],1)

    def test_failed_initialization_closes_connection_before_directory_rollback(self):
        candidate=self.root / "restore-candidate"
        fixture=Store(candidate)
        fixture.action("store.save",{"name":"需保留的恢复资料"},"original-fixture")
        fixture.close()
        original_connect=sqlite3.connect
        opened=[]
        def capture_connection(*args,**kwargs):
            conn=original_connect(*args,**kwargs)
            opened.append(conn)
            return conn
        with patch("store.sqlite3.connect",side_effect=capture_connection), patch("store.SCHEMA","INVALID SCHEMA STATEMENT;"):
            with self.assertRaises(sqlite3.OperationalError):
                Store(candidate)
        self.assertEqual(len(opened),1)
        # Retain a strong reference: passing must not depend on garbage collection.
        with self.assertRaisesRegex(sqlite3.ProgrammingError,"closed"):
            opened[0].execute("SELECT 1")
        rolled_back=candidate.with_name("restore-rolled-back")
        candidate.rename(rolled_back)
        recovered=Store(rolled_back)
        try:
            self.assertEqual(recovered.state()["stores"][0]["name"],"需保留的恢复资料")
        finally:
            recovered.close()

    def test_demo_seed_is_idempotent(self):
        self.store.seed_demo()
        before=self.store.state()
        self.store.seed_demo()
        self.assertEqual(self.store.state(),before)
        self.assertEqual(before["summary"]["pcba_available"],11)
        self.assertEqual(before["summary"]["frame_available"],8)
        self.assertEqual(before["summary"]["shipped"],5)
        self.assertEqual(len(before["ledger"]),9)
        self.assertTrue(all(b["supplier"] == "" and b["cost_cents"] == 0 for b in before["batches"]))
        self.assertTrue(all(l["category"] in (("甲方回款",) if l["direction"] == "income" else ("PCBA采购","结构件采购","研发耗材","吃饭","差旅","出货耗材")) for l in before["ledger"]))

    def test_completed_v1_demo_is_not_reseeded(self):
        self.store.db.execute("INSERT INTO settings VALUES('demo_seeded','1')")
        self.store.db.commit()
        before=self.store.state()
        self.store.seed_demo()
        self.assertEqual(self.store.state(),before)

    def test_interrupted_v2_demo_can_resume_without_duplicates(self):
        original=self.store.action
        def interrupt(action_name,data,request_id):
            if request_id == "demo-seed-v2-6":
                raise RuntimeError("simulated interruption")
            return original(action_name,data,request_id)
        with patch.object(self.store,"action",side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError,"interruption"):
                self.store.seed_demo()
        self.store.seed_demo()
        state=self.store.state()
        self.assertEqual(len(state["components"]),35)
        self.assertEqual(len(state["ledger"]),9)
        self.assertEqual(state["summary"]["expense_cents"],3437300)

    def test_interrupted_v1_demo_reuses_old_requests_and_existing_payment(self):
        self.store.action("store.save",dict(name="拾光餐厅 · 科技园店"),"demo-seed-v1-1")
        p=self.store.action("batch.create",dict(kind="pcba",date="2026-10-08",
            serials=[f"PCB-2610-{i:04d}" for i in range(1,21)]),"demo-seed-v1-4")
        self.store.db.execute("UPDATE requests SET fingerprint='old-payload-fingerprint' WHERE id='demo-seed-v1-4'")
        self.store.db.execute("UPDATE batches SET supplier='旧供应商',cost_cents=1680000 WHERE id=?",(p["id"],))
        self.store._insert("ledger",dict(id=str(uuid4()),direction="expense",date="2026-10-08",category="PCBA采购",
            amount_cents=1680000,counterparty="旧供应商",purpose="旧自动付款",store_id=None,batch_id=p["id"],notes="原始资料",created_at=self.day))
        self.store.db.commit()
        self.store.seed_demo()
        state=self.store.state()
        self.assertEqual(len(state["components"]),35)
        self.assertEqual(len(state["batches"]),2)
        self.assertEqual(len(state["ledger"]),9)
        self.assertEqual(state["summary"]["expense_cents"],3437300)
        self.assertEqual(len([l for l in state["ledger"] if l["batch_id"] == p["id"]]),1)
        before=state
        self.store.seed_demo()
        self.assertEqual(self.store.state(),before)


if __name__=="__main__":
    unittest.main()
