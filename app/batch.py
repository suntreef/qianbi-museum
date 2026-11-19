"""服务端批量识别队列：照片先排队，后台线程限速逐张跑，人可以先走。

设计：单容器单机场景，一个 daemon 线程 + SQLite 队列表足够，NAS 上零额外依赖；
队列天然串行，与证书页抓取的限频（FETCH_INTERVAL_SECONDS）天然配合。
"""
import threading
import time
from pathlib import Path

from .ingest import ingest_label
from .models import get_conn

_worker_started = False
_lock = threading.Lock()


def _pick_next():
    with get_conn() as conn:
        return conn.execute(
            """SELECT bi.id, bi.path FROM batch_item bi
               JOIN batch_job b ON b.id = bi.job_id
               WHERE bi.status = 'pending' AND b.status = 'running'
               ORDER BY bi.id LIMIT 1""").fetchone()


def _process(row) -> None:
    item_id, status, msg = None, "fail", ""
    try:
        with open(row["path"], "rb") as f:
            data = f.read()
        res = ingest_label(data, dual_ocr=True)
        if res.get("dupe"):
            status = "dup"
            item_id = res["existing"]["id"]
            msg = f"重复：{res.get('cert_no')}"
        elif res.get("ok"):
            status = "ok"
            item_id = res["item_id"]
            msg = res.get("parsed_from") or ""
        else:
            msg = "识别失败"
    except Exception as e:
        status, msg = "fail", str(e)[:200]

    with get_conn() as conn:
        conn.execute("UPDATE batch_item SET status=?, item_id=?, message=? WHERE id=?",
                     (status, item_id, msg, row["id"]))
        conn.execute(
            """UPDATE batch_job SET done = done + 1,
                   ok = ok + ?, dup = dup + ?, fail = fail + ?
               WHERE id = (SELECT job_id FROM batch_item WHERE id = ?)""",
            (1 if status == "ok" else 0, 1 if status == "dup" else 0,
             1 if status == "fail" else 0, row["id"]))
        conn.execute(
            """UPDATE batch_job SET status = 'done'
               WHERE id = (SELECT job_id FROM batch_item WHERE id = ?)
                 AND done >= total""", (row["id"],))

    try:
        Path(row["path"]).unlink(missing_ok=True)   # 处理完即删临时文件
    except Exception:
        pass


def _worker() -> None:
    while True:
        try:
            row = _pick_next()
            if not row:
                time.sleep(2)
                continue
            _process(row)
        except Exception:
            time.sleep(2)


def start_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        threading.Thread(target=_worker, daemon=True,
                         name="batch-worker").start()
        _worker_started = True
