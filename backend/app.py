import json
import os

from bottle import Bottle, request, response, run

from auth import authenticate, issue_token, read_user
from db import (
    AUDIT_LABELS,
    RevisionRejected,
    connect,
    current_setting,
    init_db,
    pin_algorithm,
    publish,
    revise,
)
from metric import (
    ALGORITHM_NAMES,
    CURRENT,
    DROP_BEDS,
    VOID_BATCH,
    classify_beds,
    normalize_algorithm,
    rate_label,
)

app = Bottle()

KIND_LABELS = {
    VOID_BATCH: "整批挡下",
    DROP_BEDS: "剔除超限床",
}

# 修订条目里用一句话把两种结果分开：整批挡下 / 丢掉该床继续算。
CARD_LABELS = {
    VOID_BATCH: "整批挡下",
    DROP_BEDS: "丢掉超限床",
}


def _json(payload, status=200):
    response.status = status
    response.content_type = "application/json; charset=utf-8"
    return json.dumps(payload, ensure_ascii=False)


def _user():
    return read_user(request.headers.get("Authorization"))


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


def _bed_payloads(beds, mode=None, pinned_name=""):
    marked = classify_beds(beds)
    has_over = any(bed["state"] == "over_limit" for bed in marked)
    rows = []
    for bed in marked:
        sown = bed["sown"]
        original_bp = None
        revised_bp = None
        if sown is not None and sown > 0:
            original_bp = (bed["sprouted"] * 10000) // sown
        if (
            bed["state"] == "normal"
            and (not has_over or mode == DROP_BEDS)
            and sown is not None
            and sown - bed["moldy"] > 0
        ):
            revised_bp = (bed["sprouted"] * 10000) // (sown - bed["moldy"])
        if bed["state"] == "over_limit":
            revised_note = "挡下，不入账"
        elif bed["state"] == "blank":
            revised_note = "播种粒空白，不进过限队列"
        elif not has_over:
            revised_note = "按现行口径入账"
        elif mode == VOID_BATCH:
            revised_note = "整批不作数，不入账"
        elif mode == DROP_BEDS:
            revised_note = "按现行口径入账"
        elif pinned_name:
            revised_note = f"已钉{pinned_name}，待修订"
        else:
            revised_note = "待钉剔床口径"
        rows.append(
            {
                "bed_no": bed["bed_no"],
                "sown": sown,
                "sprouted": bed["sprouted"],
                "moldy": bed["moldy"],
                "state": bed["state"],
                "excess": bed["excess"],
                "original_rate_bp": original_bp,
                "original_rate_label": rate_label(original_bp) if original_bp is not None else "",
                "revised_rate_bp": revised_bp,
                "revised_rate_label": rate_label(revised_bp) if revised_bp is not None else "",
                "revised_note": revised_note,
            }
        )
    return rows


def _publication_payload(row):
    kind = row.pop("revision_kind", None)
    algorithm = normalize_algorithm(kind)
    voided = algorithm == VOID_BATCH
    row["rate_label"] = rate_label(row["rate_bp"])
    row["published_at"] = _stamp(row["published_at"])
    row["current"] = row["protocol_version"] == CURRENT
    row["revision_kind"] = algorithm
    row["voided"] = voided
    row["kind_label"] = KIND_LABELS.get(algorithm, "") if algorithm else ""
    row["algorithm_name"] = ALGORITHM_NAMES.get(algorithm, "") if algorithm else ""
    row["original_rate_label"] = (
        rate_label(row["revised_from_rate_bp"])
        if row["revised_from_rate_bp"] is not None
        else ""
    )
    return row


def _dispute_payload(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "outcome": row["outcome"],
        "outcome_label": AUDIT_LABELS.get(row["outcome"], row["outcome"]),
        "detail": row["detail"],
        "requested_algorithm": row["requested_algorithm"],
        "requested_name": ALGORITHM_NAMES.get(
            normalize_algorithm(row["requested_algorithm"]), row["requested_algorithm"] or ""
        ),
        "setting_algorithm": row["setting_algorithm"],
        "setting_name": ALGORITHM_NAMES.get(
            normalize_algorithm(row["setting_algorithm"]), row["setting_algorithm"] or ""
        ),
        "created_at": _stamp(row["created_at"]),
    }


def _trial_payload(conn, trial_id):
    trial = conn.execute(
        "SELECT id, lot_code, crop_name, status FROM trials WHERE id = %s",
        (trial_id,),
    ).fetchone()
    if not trial:
        return None
    beds = conn.execute(
        "SELECT bed_no, sown, sprouted, moldy FROM beds WHERE trial_id = %s ORDER BY bed_no",
        (trial_id,),
    ).fetchall()
    publications = conn.execute(
        """
        SELECT id, protocol_version, tested, excluded_mold, sprouted, rate_bp, note,
               published_by, published_at, revision_kind, excluded_beds, revised_from_rate_bp
        FROM publications WHERE trial_id = %s ORDER BY id DESC
        """,
        (trial_id,),
    ).fetchall()
    publications = [_publication_payload(row) for row in publications]
    disputes = conn.execute(
        """
        SELECT id, username, requested_algorithm, setting_algorithm, outcome, detail, created_at
        FROM revision_audit WHERE trial_id = %s ORDER BY id DESC LIMIT 12
        """,
        (trial_id,),
    ).fetchall()
    setting = current_setting(conn, trial_id)
    if setting:
        setting = {
            "algorithm": setting["algorithm"],
            "algorithm_name": ALGORITHM_NAMES.get(setting["algorithm"], setting["algorithm"]),
            "pinned_by": setting["pinned_by"],
            "pinned_at": _stamp(setting["pinned_at"]),
            "day": setting["day"].isoformat() if setting["day"] else None,
        }
    latest_revision = next(
        (row for row in publications if row["revision_kind"]), None
    )
    revised_today = conn.execute(
        """
        SELECT COUNT(*) AS n FROM publications
        WHERE trial_id = %s AND revision_kind IS NOT NULL
          AND published_at >= date_trunc('day', NOW())
        """,
        (trial_id,),
    ).fetchone()["n"]
    mode = latest_revision["revision_kind"] if latest_revision and revised_today else None
    bed_rows = _bed_payloads(
        beds,
        mode,
        setting["algorithm_name"] if setting else "",
    )
    over = [bed for bed in bed_rows if bed["state"] == "over_limit"]
    return {
        "trial": trial,
        "beds": bed_rows,
        "over_limit": over,
        "publications": publications,
        "drop_setting": setting,
        "disputes": [_dispute_payload(row) for row in disputes],
        "counts": {
            "voided": sum(1 for row in publications if row["revision_kind"] == VOID_BATCH),
            "dropped": sum(1 for row in publications if row["revision_kind"] == DROP_BEDS),
            "setting_name": setting["algorithm_name"] if setting else "未钉",
        },
        "revised_today": revised_today,
        "revise_ready": bool(over) and bool(setting) and not revised_today,
        "blank_beds": [bed["bed_no"] for bed in bed_rows if bed["state"] == "blank"],
        "algorithms": [
            {"value": VOID_BATCH, "name": ALGORITHM_NAMES[VOID_BATCH], "label": CARD_LABELS[VOID_BATCH]},
            {"value": DROP_BEDS, "name": ALGORITHM_NAMES[DROP_BEDS], "label": CARD_LABELS[DROP_BEDS]},
        ],
    }


def _body():
    try:
        return json.loads(request.body.read() or b"{}")
    except json.JSONDecodeError:
        return None


def _algorithm(body):
    raw = body.get("algorithm") if isinstance(body, dict) else None
    return normalize_algorithm(raw)


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return _json({"status": "ok", "service": "germination-protocol"})


@app.post("/api/login")
def login():
    body = _body()
    if body is None:
        return _json({"detail": "请求不是合法的 JSON"}, 400)
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return _json({"detail": "用户名或密码错误"}, 401)
    return _json(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/trials")
def list_trials():
    if not _user():
        return _json({"detail": "未登录"}, 401)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.lot_code, t.crop_name, t.status,
                   COALESCE(b.over_limit, 0) AS over_limit_count,
                   COALESCE(p.voided, 0) AS voided,
                   COALESCE(p.dropped, 0) AS dropped,
                   COALESCE(p.revised_today, 0) AS revised_today,
                   s.algorithm AS setting_algorithm
            FROM trials t
            LEFT JOIN (
                SELECT trial_id, COUNT(*) AS over_limit
                FROM beds
                WHERE sown IS NOT NULL AND sprouted + moldy > sown
                GROUP BY trial_id
            ) b ON b.trial_id = t.id
            LEFT JOIN (
                SELECT trial_id,
                       COUNT(*) FILTER (WHERE revision_kind = %s) AS voided,
                       COUNT(*) FILTER (WHERE revision_kind = %s) AS dropped,
                       COUNT(*) FILTER (
                           WHERE revision_kind IS NOT NULL
                             AND published_at >= date_trunc('day', NOW())
                       ) AS revised_today
                FROM publications
                GROUP BY trial_id
            ) p ON p.trial_id = t.id
            LEFT JOIN drop_settings s
                   ON s.trial_id = t.id AND s.day = CURRENT_DATE
            ORDER BY t.id
            """,
            (VOID_BATCH, DROP_BEDS),
        ).fetchall()
    for row in rows:
        algorithm = normalize_algorithm(row.pop("setting_algorithm"))
        row["setting_algorithm"] = algorithm
        row["setting_name"] = ALGORITHM_NAMES.get(algorithm, "") if algorithm else "未钉"
        if row["over_limit_count"] <= 0:
            row["queue_state"] = ""
        elif not algorithm:
            row["queue_state"] = "未钉口径"
        elif row["revised_today"]:
            row["queue_state"] = "已修订"
        else:
            row["queue_state"] = "已钉待修订"
    return _json(rows)


@app.get("/api/trials/<trial_id:int>")
def trial_detail(trial_id):
    if not _user():
        return _json({"detail": "未登录"}, 401)
    with connect() as conn:
        payload = _trial_payload(conn, trial_id)
    if not payload:
        return _json({"detail": "没有这个批次"}, 404)
    return _json(payload)


@app.post("/api/trials/<trial_id:int>/drop-setting")
def pin_drop_setting(trial_id):
    user = _user()
    if not user:
        return _json({"detail": "未登录"}, 401)
    if user["role"] != "writer":
        return _json({"detail": "见证员不能钉剔床口径"}, 403)
    body = _body()
    if body is None:
        return _json({"detail": "请求不是合法的 JSON"}, 400)
    try:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM trials WHERE id = %s", (trial_id,)).fetchone()
            if not exists:
                return _json({"detail": "没有这个批次"}, 404)
            pin_algorithm(conn, trial_id, user["username"], _algorithm(body))
            payload = _trial_payload(conn, trial_id)
            conn.commit()
    except RevisionRejected as exc:
        return _json({"detail": exc.detail}, exc.status)
    return _json(payload)


@app.post("/api/trials/<trial_id:int>/revise")
def revise_over_limit(trial_id):
    user = _user()
    if not user:
        return _json({"detail": "未登录"}, 401)
    if user["role"] != "writer":
        return _json({"detail": "见证员不能修订超限批"}, 403)
    body = _body()
    if body is None:
        return _json({"detail": "请求不是合法的 JSON"}, 400)
    try:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM trials WHERE id = %s", (trial_id,)).fetchone()
            if not exists:
                return _json({"detail": "没有这个批次"}, 404)
            revise(conn, trial_id, user["username"], _algorithm(body))
            payload = _trial_payload(conn, trial_id)
            conn.commit()
    except RevisionRejected as exc:
        return _json(
            {"detail": exc.detail, "outcome": exc.outcome, "outcome_label": AUDIT_LABELS.get(exc.outcome, "")},
            exc.status,
        )
    return _json(payload)


@app.post("/api/trials/<trial_id:int>/publish")
def publish_current(trial_id):
    user = _user()
    if not user:
        return _json({"detail": "未登录"}, 401)
    if user["role"] != "writer":
        return _json({"detail": "见证员不能出数"}, 403)
    try:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM trials WHERE id = %s", (trial_id,)).fetchone()
            if not exists:
                return _json({"detail": "没有这个批次"}, 404)
            publish(conn, trial_id, user["username"], CURRENT)
            payload = _trial_payload(conn, trial_id)
            conn.commit()
    except ValueError as exc:
        return _json({"detail": str(exc)}, 400)
    return _json(payload)


init_db()


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), server="waitress")
