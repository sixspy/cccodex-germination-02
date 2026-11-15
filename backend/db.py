import os
import time

import psycopg
from psycopg.rows import dict_row

from metric import (
    ALGORITHM_NAMES,
    CURRENT,
    DROP_BEDS,
    LEGACY,
    VOID_BATCH,
    apply_revision,
    classify_beds,
    measure,
    normalize_algorithm,
    over_limit_beds,
)

LOCK_NAMESPACE = 4242

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id BIGSERIAL PRIMARY KEY,
    lot_code TEXT NOT NULL UNIQUE,
    crop_name TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS beds (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    bed_no INTEGER NOT NULL,
    sown INTEGER,
    sprouted INTEGER NOT NULL,
    moldy INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    protocol_version TEXT NOT NULL,
    tested INTEGER,
    excluded_mold INTEGER,
    sprouted INTEGER,
    rate_bp INTEGER,
    note TEXT NOT NULL,
    published_by TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revision_kind TEXT,
    excluded_beds TEXT,
    revised_from_rate_bp INTEGER
);

CREATE TABLE IF NOT EXISTS drop_settings (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    algorithm TEXT NOT NULL,
    day DATE NOT NULL DEFAULT CURRENT_DATE,
    pinned_by TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (trial_id, day)
);

CREATE TABLE IF NOT EXISTS revision_audit (
    id BIGSERIAL PRIMARY KEY,
    trial_id BIGINT NOT NULL REFERENCES trials(id),
    username TEXT NOT NULL,
    requested_algorithm TEXT,
    setting_algorithm TEXT,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

MIGRATIONS = (
    "ALTER TABLE beds ALTER COLUMN sown DROP NOT NULL",
    "ALTER TABLE publications ALTER COLUMN tested DROP NOT NULL",
    "ALTER TABLE publications ALTER COLUMN excluded_mold DROP NOT NULL",
    "ALTER TABLE publications ALTER COLUMN sprouted DROP NOT NULL",
    "ALTER TABLE publications ALTER COLUMN rate_bp DROP NOT NULL",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS revision_kind TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS excluded_beds TEXT",
    "ALTER TABLE publications ADD COLUMN IF NOT EXISTS revised_from_rate_bp INTEGER",
)

AUDIT_LABELS = {
    "not_pinned": "剔床盘未钉死",
    "algorithm_mismatch": "剔床跑偏",
    "contention": "剔床争用",
}


class RevisionRejected(Exception):
    def __init__(self, detail: str, status: int = 409, outcome: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.outcome = outcome


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                for statement in MIGRATIONS:
                    conn.execute(statement)
                count = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                _ensure_over_limit_demo(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _add_trial(conn, lot_code, crop_name, status, beds):
    trial = conn.execute(
        "INSERT INTO trials (lot_code, crop_name, status) VALUES (%s, %s, %s) RETURNING id",
        (lot_code, crop_name, status),
    ).fetchone()
    for bed_no, sown, sprouted, moldy in beds:
        conn.execute(
            """
            INSERT INTO beds (trial_id, bed_no, sown, sprouted, moldy)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (trial["id"], bed_no, sown, sprouted, moldy),
        )
    return trial["id"]


def _bed_rows(conn, trial_id: int):
    return conn.execute(
        "SELECT bed_no, sown, sprouted, moldy FROM beds WHERE trial_id = %s ORDER BY bed_no",
        (trial_id,),
    ).fetchall()


def _audit(conn, trial_id, username, requested, setting, outcome, detail):
    conn.execute(
        """
        INSERT INTO revision_audit (
            trial_id, username, requested_algorithm, setting_algorithm, outcome, detail
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (trial_id, username, requested, setting, outcome, detail),
    )


def current_setting(conn, trial_id: int):
    return conn.execute(
        """
        SELECT algorithm, pinned_by, pinned_at, day
        FROM drop_settings WHERE trial_id = %s AND day = CURRENT_DATE
        ORDER BY id DESC LIMIT 1
        """,
        (trial_id,),
    ).fetchone()


def pin_algorithm(conn, trial_id: int, username: str, algorithm):
    algorithm = normalize_algorithm(algorithm)
    if not algorithm:
        raise RevisionRejected(
            "剔床算法只能选整批不出数，或剔除超限床后用其余床入账", 400
        )
    beds = _bed_rows(conn, trial_id)
    if not beds:
        raise RevisionRejected("这个批次还没有床次", 400)
    if not over_limit_beds(beds):
        raise RevisionRejected("这个批次没有超限床，不需要钉剔床口径", 400)
    row = conn.execute(
        """
        INSERT INTO drop_settings (trial_id, algorithm, pinned_by, day)
        VALUES (%s, %s, %s, CURRENT_DATE)
        ON CONFLICT (trial_id, day) DO NOTHING
        RETURNING id
        """,
        (trial_id, algorithm, username),
    ).fetchone()
    if not row:
        existing = current_setting(conn, trial_id)
        name = ALGORITHM_NAMES.get(existing["algorithm"], existing["algorithm"]) if existing else "未知"
        raise RevisionRejected(f"剔床口径本日已钉死（{name}），明天才能改", 409)
    return current_setting(conn, trial_id)


def revise(conn, trial_id: int, username: str, algorithm):
    """修订超限批。调用方负责提交事务，事务结束时互斥锁自动释放。"""
    requested = normalize_algorithm(algorithm)
    if not requested:
        raise RevisionRejected(
            "剔床算法只能选整批不出数，或剔除超限床后用其余床入账", 400
        )
    beds = _bed_rows(conn, trial_id)
    if not beds:
        raise RevisionRejected("这个批次还没有床次", 400)
    if not over_limit_beds(beds):
        raise RevisionRejected("这个批次没有超限床，不需要剔床修订", 400)

    setting = current_setting(conn, trial_id)
    if not setting:
        _audit(conn, trial_id, username, requested, None, "not_pinned", "剔床盘未钉死，禁止修订超限批")
        conn.commit()
        raise RevisionRejected("剔床盘未钉死之前禁止修订超限批", 409, "not_pinned")

    locked = conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, %s) AS ok",
        (LOCK_NAMESPACE, trial_id),
    ).fetchone()["ok"]
    if not locked:
        _audit(
            conn,
            trial_id,
            username,
            requested,
            setting["algorithm"],
            "contention",
            "另一名检验员正在修订这一超限批，慢一步的修订未放行",
        )
        conn.commit()
        raise RevisionRejected(
            "剔床争用：另一名检验员正在修订这一超限批，请稍后重查", 409, "contention"
        )

    if requested != setting["algorithm"]:
        _audit(
            conn,
            trial_id,
            username,
            requested,
            setting["algorithm"],
            "algorithm_mismatch",
            "修订报文里的剔床算法跟剔床设置不符，超限批拒入账",
        )
        conn.commit()
        raise RevisionRejected(
            "剔床跑偏：修订报文里的剔床算法跟剔床设置不符，超限批拒入账",
            409,
            "algorithm_mismatch",
        )

    winner = conn.execute(
        """
        SELECT published_by FROM publications
        WHERE trial_id = %s AND revision_kind IS NOT NULL
          AND published_at >= date_trunc('day', NOW())
        ORDER BY id DESC LIMIT 1
        """,
        (trial_id,),
    ).fetchone()
    if winner:
        _audit(
            conn,
            trial_id,
            username,
            requested,
            setting["algorithm"],
            "contention",
            f"这一超限批本日已由 {winner['published_by']} 修订入账，慢一步的修订未放行",
        )
        conn.commit()
        raise RevisionRejected(
            f"剔床争用：这一超限批本日已由 {winner['published_by']} 修订入账，慢一步的修订未放行",
            409,
            "contention",
        )

    marked = classify_beds(beds)
    original_bp = _original_rate(marked)
    try:
        result = apply_revision(marked, requested)
    except ValueError as exc:
        raise RevisionRejected(str(exc), 400)

    _check_reconcile(marked, result)

    row = conn.execute(
        """
        INSERT INTO publications (
            trial_id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note,
            published_by, revision_kind, excluded_beds, revised_from_rate_bp
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            trial_id,
            CURRENT,
            result["tested"],
            result["excluded_mold"],
            result["sprouted"],
            result["rate_bp"],
            result["note"],
            username,
            requested,
            result["excluded_beds"],
            original_bp,
        ),
    ).fetchone()
    status = "超限批已整批挡下" if requested == VOID_BATCH else "已剔除超限床出数"
    conn.execute("UPDATE trials SET status = %s WHERE id = %s", (status, trial_id))
    return {"id": row["id"], "revision_kind": requested, "status": status}


def _original_rate(marked):
    sown = sum(bed["sown"] for bed in marked if bed["sown"] is not None)
    sprouted = sum(bed["sprouted"] for bed in marked if bed["sown"] is not None)
    if sown <= 0:
        return None
    return (sprouted * 10000) // sown


def _check_reconcile(marked, result):
    """修订后比率必须跟入账的播种数对得上。"""
    if result["voided"]:
        return
    accounted = [bed for bed in marked if bed["state"] != "over_limit" and bed["sown"] is not None]
    sown = sum(bed["sown"] for bed in accounted)
    moldy = sum(bed["moldy"] for bed in accounted)
    sprouted = sum(bed["sprouted"] for bed in accounted)
    if result["tested"] + result["excluded_mold"] != sown:
        raise RevisionRejected("修订比率对不上播种数：分母加霉粒应等于入账播种数", 409)
    if result["sprouted"] != sprouted:
        raise RevisionRejected("修订比率对不上播种数：发芽数与入账床不符", 409)
    if result["excluded_mold"] != moldy:
        raise RevisionRejected("修订比率对不上播种数：霉粒数与入账床不符", 409)
    if result["rate_bp"] != (result["sprouted"] * 10000) // result["tested"]:
        raise RevisionRejected("修订比率对不上播种数：比率与分子分母不符", 409)


def publish(conn, trial_id: int, username: str, protocol: str):
    beds = _bed_rows(conn, trial_id)
    if not beds:
        raise ValueError("这个批次还没有床次")
    over = over_limit_beds(beds)
    if over:
        names = ",".join(str(bed["bed_no"]) for bed in over)
        raise ValueError(f"超限床 {names} 已挡下：先钉剔床口径再修订，超限批不能直接出数")
    result = measure(beds, protocol)
    row = conn.execute(
        """
        INSERT INTO publications (
            trial_id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note, published_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            trial_id,
            result["protocol_version"],
            result["tested"],
            result["excluded_mold"],
            result["sprouted"],
            result["rate_bp"],
            result["note"],
            username,
        ),
    ).fetchone()
    if protocol != LEGACY:
        conn.execute("UPDATE trials SET status = %s WHERE id = %s", ("已按现行口径出数", trial_id))
    return row["id"]


def _seed(conn):
    wheat = _add_trial(
        conn,
        "WH-2408",
        "小麦",
        "已按现行口径出数",
        [(1, 100, 92, 3), (2, 100, 88, 5)],
    )
    publish(conn, wheat, "admin", "v2")
    rice = _add_trial(
        conn,
        "RC-2411",
        "水稻",
        "待按现行口径重算",
        [(1, 80, 70, 6), (2, 80, 64, 4)],
    )
    publish(conn, rice, "admin", LEGACY)
    conn.execute("UPDATE trials SET status = %s WHERE id = %s", ("待按现行口径重算", rice))


def _ensure_over_limit_demo(conn):
    """老库升级时补一个超限批，方便核对剔床流程。"""
    exists = conn.execute("SELECT id FROM trials WHERE lot_code = %s", ("SG-2413",)).fetchone()
    if exists:
        return
    _add_trial(
        conn,
        "SG-2413",
        "高粱",
        "有超限床，待钉剔床口径",
        [
            (1, 100, 86, 9),
            (2, 100, 90, 18),
            (3, None, 0, 0),
            (4, 80, 72, 5),
        ],
    )
