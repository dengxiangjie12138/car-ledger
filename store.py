"""Transactional, serialized inventory and cash ledger for the local car ledger."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from datetime import date as Date, datetime, timedelta, timezone
from uuid import uuid4


TZ = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
STORE_CODE_PREFIX = "store.code:"
STORE_DOCUMENT_PREFIX = "store-document:"
LEDGER_CATEGORIES = {
    "income": ("甲方回款",),
    "expense": ("PCBA采购", "结构件采购", "研发耗材", "吃饭", "差旅", "出货耗材"),
}


def now():
    return datetime.now(TZ).isoformat(timespec="microseconds")


def uid():
    return str(uuid4())


def text(value, name="内容", required=False, limit=5000):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name}必须是文字")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写{name}")
    if len(value) > limit or "\x00" in value:
        raise ValueError(f"{name}过长或包含无效字符")
    return value


def money(value, allow_zero=True):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("金额必须按整数分提交")
    if value < (0 if allow_zero else 1) or value > 100_000_000_000_000:
        raise ValueError("金额超出允许范围")
    return value


def date(value=None):
    value = text(value or datetime.now(TZ).date().isoformat(), "日期", True, 10)
    try:
        parsed = Date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores (
 id TEXT PRIMARY KEY,name TEXT NOT NULL,contact TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',
 address TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('pcba','frame')),supplier TEXT NOT NULL,date TEXT NOT NULL,
 cost_cents INTEGER NOT NULL CHECK(cost_cents>=0),notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS components (
 id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('pcba','frame')),serial TEXT NOT NULL,batch_id TEXT NOT NULL REFERENCES batches(id),
 status TEXT NOT NULL CHECK(status IN ('available','claimed','installed','spare','repair','scrapped')),
 store_id TEXT REFERENCES stores(id),notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
 UNIQUE(kind,serial),CHECK(status<>'spare' OR store_id IS NOT NULL));
CREATE TABLE IF NOT EXISTS vehicles (
 id TEXT PRIMARY KEY,serial TEXT NOT NULL UNIQUE,frame_id TEXT NOT NULL UNIQUE REFERENCES components(id),
 pcba_id TEXT REFERENCES components(id),store_id TEXT REFERENCES stores(id),
 status TEXT NOT NULL CHECK(status IN ('ready','shipped','disassembled')),created_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
 CHECK(status<>'shipped' OR store_id IS NOT NULL),CHECK(status='disassembled' OR pcba_id IS NOT NULL));
CREATE UNIQUE INDEX IF NOT EXISTS one_current_pcba ON vehicles(pcba_id) WHERE status<>'disassembled';
CREATE TABLE IF NOT EXISTS ledger (
 id TEXT PRIMARY KEY,direction TEXT NOT NULL CHECK(direction IN ('income','expense')),date TEXT NOT NULL,
 category TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),counterparty TEXT NOT NULL,purpose TEXT NOT NULL,
 store_id TEXT REFERENCES stores(id),batch_id TEXT REFERENCES batches(id),notes TEXT NOT NULL DEFAULT '',
 voided INTEGER NOT NULL DEFAULT 0 CHECK(voided IN (0,1)),void_reason TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipments (
 id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),date TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('vehicle','spare','transfer')),notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
 voided INTEGER NOT NULL DEFAULT 0 CHECK(voided IN (0,1)),void_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS shipment_items (
 id TEXT PRIMARY KEY,shipment_id TEXT NOT NULL REFERENCES shipments(id),kind TEXT NOT NULL,item_id TEXT NOT NULL,
 serial TEXT NOT NULL,pcba_serial TEXT, before_json TEXT NOT NULL,after_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS movements (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,component_id TEXT NOT NULL REFERENCES components(id),
 action TEXT NOT NULL,from_status TEXT,to_status TEXT NOT NULL,store_id TEXT REFERENCES stores(id),date TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
 event TEXT NOT NULL,date TEXT NOT NULL,old_pcba_serial TEXT,new_pcba_serial TEXT,
 from_store_id TEXT REFERENCES stores(id),to_store_id TEXT REFERENCES stores(id),notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attachments (
 id TEXT PRIMARY KEY,owner_type TEXT NOT NULL CHECK(owner_type IN ('ledger','batch','store','vehicle','shipment')),
 owner_id TEXT NOT NULL,slot TEXT NOT NULL,filename TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL CHECK(size>=0),
 storage_name TEXT NOT NULL UNIQUE,notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,UNIQUE(owner_type,owner_id,slot));
CREATE TABLE IF NOT EXISTS credentials (
 id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),label TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_components_status ON components(kind,status);
CREATE INDEX IF NOT EXISTS idx_movements_component ON movements(component_id,seq);
CREATE INDEX IF NOT EXISTS idx_history_vehicle ON history(vehicle_id,seq);
CREATE INDEX IF NOT EXISTS idx_attachments_owner ON attachments(owner_type,owner_id);
"""


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "attachments").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "ledger.sqlite3", timeout=15, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=15000")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("台账版本不兼容，请使用对应版本的程序")
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except BaseException:
            # A failed constructor has no caller that can invoke close().
            # Release the file handle before restore rolls its directory back.
            self.db.close()
            raise

    def close(self):
        with self.lock:
            self.db.close()

    def _get(self, table, item_id, label="记录"):
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (text(item_id, label, True, 100),)).fetchone()
        if row is None:
            raise ValueError(f"找不到{label}")
        return dict(row)

    def _optional(self, table, item_id, label):
        if item_id:
            return self._get(table, item_id, label)["id"]
        return None

    def _insert(self, table, values):
        cols = list(values)
        self.db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [values[k] for k in cols])

    def _ids(self, data):
        values = data.get("ids")
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise ValueError("请选择 1 至 1000 个编号")
        result = [text(v, "编号", True, 100) for v in values]
        if len(set(result)) != len(result):
            raise ValueError("清单中有重复编号，请先移除重复项")
        return result

    def _kind(self, value):
        if value not in ("pcba", "frame"):
            raise ValueError("物料类型应为 PCBA 或结构件")
        return value

    def _component(self, item_id, kind=None):
        row = self._get("components", item_id, "库存编号")
        if kind and row["kind"] != kind:
            raise ValueError("所选编号的物料类型不符")
        return row

    def _bound(self, component_id):
        return self.db.execute("SELECT id FROM vehicles WHERE status<>'disassembled' AND (frame_id=? OR pcba_id=?)", (component_id, component_id)).fetchone() is not None

    def _move(self, component, status, store_id, action, day, notes=""):
        self.db.execute("UPDATE components SET status=?,store_id=?,revision=revision+1 WHERE id=?", (status, store_id, component["id"]))
        self._insert("movements", dict(id=uid(), component_id=component["id"], action=action, from_status=component.get("status"), to_status=status, store_id=store_id, date=day, notes=notes, created_at=now()))

    def _history(self, vehicle_id, event, day, old=None, new=None, from_store=None, to_store=None, notes=""):
        self._insert("history", dict(id=uid(), vehicle_id=vehicle_id,event=event,date=day,old_pcba_serial=old,new_pcba_serial=new,from_store_id=from_store,to_store_id=to_store,notes=notes,created_at=now()))

    def action(self, name: str, data: dict, request_id: str):
        if not isinstance(name, str) or not isinstance(data, dict):
            raise ValueError("业务操作格式不正确")
        request_id = text(request_id, "请求编号", True, 200)
        encoded = json.dumps([name, data], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                previous = self.db.execute("SELECT fingerprint,result_json FROM requests WHERE id=?", (request_id,)).fetchone()
                if previous:
                    if previous["fingerprint"] != fingerprint:
                        raise ValueError("该请求编号已用于其他操作，请刷新后重试")
                    self.db.commit()
                    return json.loads(previous["result_json"])
                methods = {
                    "store.save": self._store_save, "ledger.save": self._ledger_save, "ledger.void": self._ledger_void,
                    "batch.create": self._batch_create, "component.outbound": self._component_outbound,
                    "component.return": self._component_return, "component.dispose": self._component_dispose,
                    "component.transfer": self._component_transfer, "vehicle.assemble": self._vehicle_assemble,
                    "vehicle.replace": self._vehicle_replace, "vehicle.ship": self._vehicle_ship,
                    "vehicle.transfer": self._vehicle_transfer, "shipment.void": self._shipment_void,
                    "vehicle.disassemble": self._vehicle_disassemble,
                }
                if name not in methods:
                    raise ValueError("不支持的业务操作")
                result = methods[name](data)
                self._insert("requests", dict(id=request_id, fingerprint=fingerprint, result_json=json.dumps(result, ensure_ascii=False), created_at=now()))
                self.db.commit()
                return result
            except sqlite3.IntegrityError as exc:
                self.db.rollback()
                if "components.kind, components.serial" in str(exc):
                    raise ValueError("编号已存在，整批入库未保存，请检查重复编号") from None
                raise ValueError("操作与已有记录冲突，请刷新后核对编号、绑定及门店") from None
            except Exception:
                self.db.rollback()
                raise

    def _store_save(self, data):
        item_id = data.get("id")
        current = self._get("stores", item_id, "门店") if item_id else None
        item_id = current["id"] if current else uid()
        labels = {"name":"门店名称","contact":"联系人","phone":"电话","address":"地址","notes":"备注"}
        values = {k: text(data[k] if k in data else current[k] if current else "", labels[k],
                          k == "name", 5000 if k == "notes" else 500)
                  for k in labels}
        code_key = STORE_CODE_PREFIX + item_id
        code = text(data.get("code"), "门店编码", limit=120) if "code" in data else None
        if code:
            # action() holds BEGIN IMMEDIATE, so two connections cannot assign
            # the same external code between this check and the write.
            duplicate = self.db.execute(
                "SELECT 1 FROM settings WHERE substr(key,1,?)=? AND value=? AND key<>? LIMIT 1",
                (len(STORE_CODE_PREFIX), STORE_CODE_PREFIX, code, code_key)).fetchone()
            if duplicate:
                raise ValueError("门店编码已被其他门店使用，请填写不同编码")
        if current:
            self.db.execute("UPDATE stores SET name=?,contact=?,phone=?,address=?,notes=? WHERE id=?", (*values.values(), item_id))
        else:
            self._insert("stores", dict(id=item_id, **values, created_at=now()))
        if code is not None:
            if code:
                self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (code_key, code))
            else:
                self.db.execute("DELETE FROM settings WHERE key=?", (code_key,))
        return {"id":item_id}

    def _ledger_save(self, data):
        direction = data.get("direction")
        if direction not in ("income","expense"):
            raise ValueError("请选择收入或支出")
        item_id = data.get("id")
        row = self._get("ledger", item_id, "账目") if item_id else None
        if row and row["voided"]:
            raise ValueError("已作废账目不能编辑，请新增正确账目")
        category = text(data.get("category"), "分类", True, 200)
        unchanged_legacy = row and direction == row["direction"] and category == row["category"]
        if category not in LEDGER_CATEGORIES[direction] and not unchanged_legacy:
            raise ValueError("请选择当前收支方向提供的分类")
        values = dict(direction=direction, date=date(data.get("date")), category=category,
                      amount_cents=money(data.get("amount_cents"), False), notes=text(data.get("notes"), "备注"))
        if item_id:
            # Hidden v1 fields remain intact in historical records and backups.
            item_id = row["id"]
            self.db.execute("UPDATE ledger SET "+",".join(f"{k}=?" for k in values)+" WHERE id=?",[*values.values(),item_id])
        else:
            item_id=uid()
            self._insert("ledger",dict(id=item_id, **values, counterparty="", purpose="", store_id=None,
                                       batch_id=None, created_at=now()))
        self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?, '1')",
                        ("ledger.notes-edited:" + item_id,))
        return {"id":item_id}

    def _ledger_void(self,data):
        row=self._get("ledger",data.get("id"),"账目")
        if row["voided"]:
            raise ValueError("该账目已作废")
        reason=text(data.get("reason"),"作废原因",True)
        self.db.execute("UPDATE ledger SET voided=1,void_reason=? WHERE id=?",(reason,row["id"]))
        return {"id":row["id"]}

    def _batch_create(self,data):
        kind=self._kind(data.get("kind"))
        serials=data.get("serials")
        if not isinstance(serials,list) or not serials or len(serials)>1000:
            raise ValueError("请逐套输入 1 至 1000 个库存编号")
        serials=[text(s,"库存编号",True,200) for s in serials]
        if len(set(serials))!=len(serials):
            raise ValueError("入库清单有重复编号")
        day=date(data.get("date"))
        if data.get("paid_cents") not in (None, 0):
            raise ValueError("入库不再记账，请刷新页面后在收支账本登记实际支出")
        item_id=uid()
        # Keep the v1 columns only as neutral storage placeholders for old backups.
        self._insert("batches",dict(id=item_id,kind=kind,supplier="",date=day,cost_cents=0,notes=text(data.get("notes")),created_at=now()))
        component_ids=[]
        for serial in serials:
            component_id=uid()
            component_ids.append(component_id)
            self._insert("components",dict(id=component_id,kind=kind,serial=serial,batch_id=item_id,status="available",notes="",created_at=now()))
            self._insert("movements",dict(id=uid(),component_id=component_id,action="intake",from_status=None,to_status="available",store_id=None,date=day,notes="扫码入库",created_at=now()))
        result={"id":item_id,"component_ids":component_ids}
        return result

    def _shipment(self,kind,store_id,day,notes):
        item_id=uid()
        self._insert("shipments",dict(id=item_id,store_id=store_id,date=day,kind=kind,notes=notes,created_at=now()))
        return item_id

    def _shipment_item(self,shipment_id,kind,item_id,serial,before,after,pcba_serial=None):
        self._insert("shipment_items",dict(id=uid(),shipment_id=shipment_id,kind=kind,item_id=item_id,serial=serial,pcba_serial=pcba_serial,before_json=json.dumps(before,ensure_ascii=False),after_json=json.dumps(after,ensure_ascii=False)))

    def _component_outbound(self,data):
        kind=self._kind(data.get("kind"))
        purpose=data.get("purpose")
        if purpose not in ("assembly","spare"):
            raise ValueError("请选择装车领用或门店备件")
        store_id=self._optional("stores",data.get("store_id"),"门店") if purpose=="spare" else None
        if purpose=="spare" and not store_id:
            raise ValueError("门店备件出库必须选择门店")
        rows=[self._component(i,kind) for i in self._ids(data)]
        if any(r["status"]!="available" or self._bound(r["id"]) for r in rows):
            raise ValueError("清单中包含非在库编号，整批出库未保存")
        day=date(data.get("date")); notes=text(data.get("notes"))
        shipment_id=self._shipment("spare",store_id,day,notes) if purpose=="spare" else None
        for row in rows:
            self._move(row,"spare" if store_id else "claimed",store_id,"spare_outbound" if store_id else "assembly_claim",day,notes)
            if shipment_id:
                self._shipment_item(shipment_id,kind,row["id"],row["serial"],row,self._component(row["id"]))
        return {"id":shipment_id or rows[0]["id"],"shipment_id":shipment_id,"count":len(rows)}

    def _component_return(self,data):
        row=self._component(data.get("id"))
        if self._bound(row["id"]) or row["status"] not in ("claimed","spare","repair"):
            raise ValueError("仅未装车的已领用、门店备件或返修配件可退库")
        self._move(row,"available",None,"return",date(data.get("date")),text(data.get("notes")))
        return {"id":row["id"]}

    def _component_dispose(self,data):
        row=self._component(data.get("id"))
        status=data.get("status")
        if status not in ("repair","scrapped"):
            raise ValueError("请选择待返修或报废")
        if self._bound(row["id"]) or row["status"] in ("installed","scrapped") or row["status"]==status:
            raise ValueError("该配件当前状态不能执行此操作；已装车配件请先换板")
        self._move(row,status,row["store_id"] if status=="repair" else None,"repair" if status=="repair" else "scrap",date(data.get("date")),text(data.get("notes"),"处理原因",True))
        return {"id":row["id"]}

    def _component_transfer(self,data):
        rows=[self._component(i) for i in self._ids(data)]
        store_id=self._get("stores",data.get("store_id"),"目标门店")["id"]
        if any(r["status"]!="spare" or not r["store_id"] or r["store_id"]==store_id or self._bound(r["id"]) for r in rows) or len({r["store_id"] for r in rows})!=1:
            raise ValueError("请选择同一家门店的未装车备件，并选择不同的目标门店")
        day=date(data.get("date")); notes=text(data.get("notes"))
        shipment_id=self._shipment("transfer",store_id,day,notes)
        for row in rows:
            self._move(row,"spare",store_id,"spare_transfer",day,notes)
            self._shipment_item(shipment_id,row["kind"],row["id"],row["serial"],row,self._component(row["id"]))
        return {"id":shipment_id,"count":len(rows)}

    def _vehicle_assemble(self,data):
        frame=self._component(data.get("frame_id"),"frame")
        pcba=self._component(data.get("pcba_id"),"pcba")
        if self._bound(frame["id"]) or self._bound(pcba["id"]):
            raise ValueError("车架或 PCBA 已绑定其他车辆")
        same_claim=frame["status"]==pcba["status"]=="claimed" and frame["store_id"] is None and pcba["store_id"] is None
        same_spare=frame["status"]==pcba["status"]=="spare" and frame["store_id"]==pcba["store_id"] and frame["store_id"] is not None
        if not (same_claim or same_spare):
            raise ValueError("请先领用车架和 PCBA；两者应同为待装车，或同为一家门店的备件")
        store_id=frame["store_id"]
        existing=self.db.execute("SELECT * FROM vehicles WHERE frame_id=?",(frame["id"],)).fetchone()
        day=date(data.get("date")); notes=text(data.get("notes"))
        if existing:
            if existing["status"]!="disassembled":
                raise ValueError("该车架已登记车辆")
            item_id=existing["id"]
            self.db.execute("UPDATE vehicles SET pcba_id=?,store_id=?,status=?,revision=revision+1 WHERE id=?",(pcba["id"],store_id,"shipped" if store_id else "ready",item_id))
        else:
            item_id=uid()
            self._insert("vehicles",dict(id=item_id,serial=frame["serial"],frame_id=frame["id"],pcba_id=pcba["id"],store_id=store_id,status="shipped" if store_id else "ready",created_at=now()))
        self._move(frame,"installed",store_id,"assemble",day,notes)
        self._move(pcba,"installed",store_id,"assemble",day,notes)
        self._history(item_id,"assembled",day,new=pcba["serial"],to_store=store_id,notes=notes)
        return {"id":item_id}

    def _vehicle_replace(self,data):
        vehicle=self._get("vehicles",data.get("id"),"车辆")
        if vehicle["status"]=="disassembled":
            raise ValueError("已拆解车辆请先重新装车")
        new=self._component(data.get("pcba_id"),"pcba")
        if self._bound(new["id"]):
            raise ValueError("新 PCBA 已装在车辆上")
        if vehicle["store_id"]:
            valid=new["status"]=="spare" and new["store_id"]==vehicle["store_id"]
        else:
            valid=new["status"]=="claimed" and new["store_id"] is None
        if not valid:
            raise ValueError("请先领用新 PCBA；门店车辆须使用该店的备件，跨店备件请先调拨")
        disposition=data.get("old_disposition")
        if disposition not in ("repair","available","spare","scrapped"):
            raise ValueError("请选择旧板去向")
        if disposition=="spare" and not vehicle["store_id"]:
            raise ValueError("未出货车辆的旧板不能留作门店备件")
        old=self._component(vehicle["pcba_id"],"pcba")
        day=date(data.get("date")); notes=text(data.get("notes"),"换板原因",True)
        self.db.execute("UPDATE vehicles SET pcba_id=?,revision=revision+1 WHERE id=?",(new["id"],vehicle["id"]))
        old_store=vehicle["store_id"] if disposition in ("spare","repair") else None
        self._move(old,disposition,old_store,"replace_remove",day,notes)
        self._move(new,"installed",vehicle["store_id"],"replace_install",day,notes)
        self._history(vehicle["id"],"pcba_replaced",day,old=old["serial"],new=new["serial"],from_store=vehicle["store_id"],to_store=vehicle["store_id"],notes=notes)
        return {"id":vehicle["id"]}

    def _vehicle_snapshot(self,vehicle):
        return dict(vehicle=vehicle,frame=self._component(vehicle["frame_id"],"frame"),pcba=self._component(vehicle["pcba_id"],"pcba"))

    def _vehicle_dispatch(self,data,transfer):
        rows=[self._get("vehicles",i,"车辆") for i in self._ids(data)]
        store_id=self._get("stores",data.get("store_id"),"目标门店")["id"]
        expected="shipped" if transfer else "ready"
        if any(v["status"]!=expected or not v["pcba_id"] for v in rows):
            raise ValueError("转店须选择已出货车辆" if transfer else "只有完成配对的待出货成品车可以出货")
        if transfer and (len({v["store_id"] for v in rows})!=1 or any(v["store_id"]==store_id for v in rows)):
            raise ValueError("请选择同一家门店的车辆，并选择不同的目标门店")
        day=date(data.get("date"));notes=text(data.get("notes"))
        shipment_id=self._shipment("transfer" if transfer else "vehicle",store_id,day,notes)
        for vehicle in rows:
            before=self._vehicle_snapshot(vehicle)
            self.db.execute("UPDATE vehicles SET status='shipped',store_id=?,revision=revision+1 WHERE id=?",(store_id,vehicle["id"]))
            for component in (before["frame"],before["pcba"]):
                self._move(component,"installed",store_id,"vehicle_transfer" if transfer else "vehicle_ship",day,notes)
            self._history(vehicle["id"],"transferred" if transfer else "shipped",day,old=before["pcba"]["serial"],new=before["pcba"]["serial"],from_store=vehicle["store_id"],to_store=store_id,notes=notes)
            self._shipment_item(shipment_id,"vehicle",vehicle["id"],vehicle["serial"],before,self._vehicle_snapshot(self._get("vehicles",vehicle["id"])),before["pcba"]["serial"])
        return {"id":shipment_id,"count":len(rows)}

    def _vehicle_ship(self,data):
        return self._vehicle_dispatch(data,False)

    def _vehicle_transfer(self,data):
        return self._vehicle_dispatch(data,True)

    def _vehicle_disassemble(self,data):
        vehicle=self._get("vehicles",data.get("id"),"车辆")
        latest=self.db.execute("SELECT event FROM history WHERE vehicle_id=? ORDER BY seq DESC LIMIT 1",(vehicle["id"],)).fetchone()
        if vehicle["status"]!="ready" or vehicle["store_id"] or not latest or latest["event"]!="assembled":
            raise ValueError("仅尚未发生后续业务的本地成品车可撤销装车")
        day=date(data.get("date"));notes=text(data.get("notes"),"拆解原因",True)
        frame=self._component(vehicle["frame_id"]);pcba=self._component(vehicle["pcba_id"])
        self.db.execute("UPDATE vehicles SET status='disassembled',pcba_id=NULL,revision=revision+1 WHERE id=?",(vehicle["id"],))
        self._move(frame,"claimed",None,"disassemble",day,notes)
        self._move(pcba,"claimed",None,"disassemble",day,notes)
        self._history(vehicle["id"],"disassembled",day,old=pcba["serial"],notes=notes)
        return {"id":vehicle["id"]}

    def _shipment_void(self,data):
        shipment=self._get("shipments",data.get("id"),"出货记录")
        if shipment["voided"]:
            raise ValueError("该出货记录已撤销")
        reason=text(data.get("reason"),"撤销原因",True)
        items=[dict(r) for r in self.db.execute("SELECT * FROM shipment_items WHERE shipment_id=?",(shipment["id"],))]
        if not items:
            raise ValueError("该记录没有可撤销明细")
        for item in items:
            after=json.loads(item["after_json"])
            if item["kind"]=="vehicle":
                current=self._vehicle_snapshot(self._get("vehicles",item["item_id"],"车辆"))
                valid=all(current[k]["revision"]==after[k]["revision"] for k in ("vehicle","frame","pcba"))
            else:
                current=self._component(item["item_id"])
                valid=current["revision"]==after["revision"]
            if not valid:
                raise ValueError("相关车辆或配件已有后续操作，不能撤销这笔出货；请通过退库或转店更正")
        day=date(data.get("date"))
        for item in items:
            before=json.loads(item["before_json"])
            if item["kind"]=="vehicle":
                current=self._get("vehicles",item["item_id"])
                old=before["vehicle"]
                self.db.execute("UPDATE vehicles SET status=?,store_id=?,revision=revision+1 WHERE id=?",(old["status"],old["store_id"],current["id"]))
                for key in ("frame","pcba"):
                    old_component=before[key]
                    self._move(self._component(old_component["id"]),old_component["status"],old_component["store_id"],"shipment_void",day,reason)
                self._history(current["id"],"shipment_void",day,old=before["pcba"]["serial"],new=before["pcba"]["serial"],from_store=current["store_id"],to_store=old["store_id"],notes=reason)
            else:
                self._move(self._component(item["item_id"]),before["status"],before["store_id"],"shipment_void",day,reason)
        self.db.execute("UPDATE shipments SET voided=1,void_reason=? WHERE id=?",(reason,shipment["id"]))
        return {"id":shipment["id"]}

    def state(self):
        with self.lock:
            def rows(sql, params=()):
                return [dict(r) for r in self.db.execute(sql, params)]
            state={
                "stores":rows("SELECT * FROM stores ORDER BY created_at DESC"),
                "batches":rows("SELECT b.*,(SELECT COUNT(*) FROM components c WHERE c.batch_id=b.id) quantity FROM batches b ORDER BY date DESC,created_at DESC"),
                "components":rows("SELECT * FROM components ORDER BY created_at DESC"),
                "vehicles":rows("SELECT v.*,f.serial frame_serial,p.serial pcba_serial FROM vehicles v JOIN components f ON f.id=v.frame_id LEFT JOIN components p ON p.id=v.pcba_id ORDER BY v.created_at DESC"),
                "ledger":rows("SELECT * FROM ledger ORDER BY date DESC,created_at DESC"),
                "shipments":rows("SELECT * FROM shipments ORDER BY date DESC,created_at DESC"),
                "movements":rows("SELECT * FROM movements ORDER BY seq DESC"),
                "history":rows("SELECT * FROM history ORDER BY seq DESC"),
                "attachments":rows("SELECT id,owner_type,owner_id,slot,filename,mime,size,notes,created_at FROM attachments ORDER BY created_at DESC"),
                "credentials":rows("SELECT id,store_id,label,created_at FROM credentials WHERE substr(id,1,?)<>? ORDER BY created_at DESC",
                                   (len(STORE_DOCUMENT_PREFIX), STORE_DOCUMENT_PREFIX)),
            }
            codes = {r[0][len(STORE_CODE_PREFIX):]: r[1] for r in self.db.execute(
                "SELECT key,value FROM settings WHERE substr(key,1,?)=?", (len(STORE_CODE_PREFIX), STORE_CODE_PREFIX))}
            document_stores = {r["store_id"] for r in self.db.execute(
                "SELECT id,store_id FROM credentials WHERE substr(id,1,?)=?",
                (len(STORE_DOCUMENT_PREFIX), STORE_DOCUMENT_PREFIX))
                if r["id"] == STORE_DOCUMENT_PREFIX + r["store_id"]}
            for store in state["stores"]:
                store["code"] = codes.get(store["id"], "")
                store["has_document"] = store["id"] in document_stores
            prefix = "ledger.notes-edited:"
            edited_notes = {r[0][len(prefix):] for r in self.db.execute(
                "SELECT key FROM settings WHERE key LIKE 'ledger.notes-edited:%' AND value='1'")}
            for ledger in state["ledger"]:
                ledger["notes_edited"] = ledger["id"] in edited_notes
            items=rows("SELECT shipment_id,kind,item_id id,serial,pcba_serial FROM shipment_items")
            by_shipment={}
            for item in items:
                by_shipment.setdefault(item.pop("shipment_id"),[]).append(item)
            for shipment in state["shipments"]:
                shipment["items"]=by_shipment.get(shipment["id"],[])
            income=sum(r["amount_cents"] for r in state["ledger"] if r["direction"]=="income" and not r["voided"])
            expense=sum(r["amount_cents"] for r in state["ledger"] if r["direction"]=="expense" and not r["voided"])
            state["summary"]=dict(income_cents=income,expense_cents=expense,balance_cents=income-expense,pcba_available=sum(c["kind"]=="pcba" and c["status"]=="available" for c in state["components"]),frame_available=sum(c["kind"]=="frame" and c["status"]=="available" for c in state["components"]),claimed=sum(c["status"]=="claimed" for c in state["components"]),ready=sum(v["status"]=="ready" for v in state["vehicles"]),shipped=sum(v["status"]=="shipped" for v in state["vehicles"]),store_count=len(state["stores"]))
            return state

    def seed_demo(self):
        if self.db.execute("SELECT value FROM settings WHERE key='demo_seeded'").fetchone():
            return
        # Only called explicitly for the separate demo profile by the server.
        seed_step=0
        def run(action_name,**data):
            nonlocal seed_step
            seed_step+=1
            # Reuse completed v1 steps without replaying changed payloads. The
            # first 24 v2 steps retain their v1 business meanings and ordering.
            previous = self.db.execute("SELECT result_json FROM requests WHERE id=?", (f"demo-seed-v1-{seed_step}",)).fetchone()
            if previous:
                return json.loads(previous["result_json"])
            return self.action(action_name,data,f"demo-seed-v2-{seed_step}")
        a=run("store.save",name="拾光餐厅 · 科技园店",contact="陈店长",phone="138 0000 1024",address="深圳市南山区科技园 · 演示地址",notes="三台送餐车已安装；地图与安全池文件按资料项归档。")['id']
        b=run("store.save",name="山野食集 · 万象城店",contact="林经理",phone="139 0000 2048",address="深圳市罗湖区万象城 · 演示地址",notes="二楼环线，周三安排安装培训。")['id']
        c=run("store.save",name="禾间小馆 · 湖畔店",contact="周店长",phone="137 0000 4096",address="杭州市西湖区 · 演示地址",notes="待首批交付，安装图纸待确认。")['id']
        p=run("batch.create",kind="pcba",date="2026-10-08",serials=[f"PCB-2610-{i:04d}" for i in range(1,21)],notes="控制主板 V1.2，首批量产备货")
        f=run("batch.create",kind="frame",date="2026-10-10",serials=[f"CAR-2610-{i:04d}" for i in range(1,16)],notes="整套车架、外壳与安装支架")
        run("component.outbound",kind="pcba",ids=p["component_ids"][:7],purpose="assembly",date="2026-10-12",notes="首批装车领用")
        run("component.outbound",kind="frame",ids=f["component_ids"][:7],purpose="assembly",date="2026-10-12",notes="首批结构件领用")
        cars=[]
        for index in range(6):
            cars.append(run("vehicle.assemble",frame_id=f["component_ids"][index],pcba_id=p["component_ids"][index],date="2026-10-13",notes="整车装配与出厂检查通过")["id"])
        run("vehicle.ship",ids=cars[:3],store_id=a,date="2026-10-16",notes="首批 3 台整车，现场安装完成")
        run("vehicle.ship",ids=cars[3:5],store_id=b,date="2026-10-19",notes="第二批 2 台整车，安装完成")
        run("component.outbound",kind="pcba",ids=p["component_ids"][7:9],purpose="spare",store_id=a,date="2026-10-20",notes="售后备用主板 2 套")
        run("vehicle.replace",id=cars[0],pcba_id=p["component_ids"][7],old_disposition="repair",date="2026-10-22",notes="演示换板：原板接口排查，新板已通过测试")
        run("ledger.save",direction="income",date="2026-10-16",category="甲方回款",amount_cents=5940000,notes="科技园店 3 台小车货款")
        run("ledger.save",direction="income",date="2026-10-19",category="甲方回款",amount_cents=3960000,notes="万象城店 2 台小车货款")
        run("ledger.save",direction="income",date="2026-10-24",category="甲方回款",amount_cents=12800,notes="湖畔店安装辅料补款")
        for day,category,amount,notes in [
            ("2026-10-11","研发耗材",245800,"示波器探头、调试线与测试夹具"),
            ("2026-10-15","差旅",86000,"科技园店现场安装差旅"),
            ("2026-10-18","出货耗材",42600,"整车包装与保护材料"),
            ("2026-10-23","研发耗材",32900,"路由器与备用电源"),
        ]:
            run("ledger.save",direction="expense",date=day,category=category,amount_cents=amount,notes=notes)
        for batch, category, amount, day, notes in [
            (p, "PCBA采购", 1680000, "2026-10-08", "首批 PCBA 采购实际付款 · 20 套"),
            (f, "结构件采购", 1350000, "2026-10-10", "首批结构件采购实际付款 · 15 套"),
        ]:
            # An interrupted v1 seed may already have recorded these payments.
            # Keep the step count stable for resumable v2 runs even when skipped.
            if self.db.execute("SELECT 1 FROM ledger WHERE batch_id=?", (batch["id"],)).fetchone():
                seed_step += 1
                continue
            run("ledger.save", direction="expense", date=day, category=category, amount_cents=amount, notes=notes)
        self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('demo_seeded','2')")
        self.db.commit()
