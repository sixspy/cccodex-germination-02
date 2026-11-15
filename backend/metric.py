CURRENT = "v2"
LEGACY = "v1"

VOID_BATCH = "void_batch"
DROP_BEDS = "drop_beds"

ALGORITHM_NAMES = {
    VOID_BATCH: "整批不出数",
    DROP_BEDS: "剔除超限床",
}

ALGORITHM_ALIASES = {
    VOID_BATCH: ("void_batch", "整批不出数", "整批挡下"),
    DROP_BEDS: ("drop_beds", "剔除超限床"),
}


def rate_label(basis_points) -> str:
    if basis_points is None:
        return ""
    whole = basis_points // 100
    frac = basis_points % 100
    return f"{whole}.{frac:02d}%"


def normalize_algorithm(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    for key, aliases in ALGORITHM_ALIASES.items():
        if text in aliases:
            return key
    return None


def bed_state(bed: dict) -> str:
    """播种粒空白的床不进过限队列；发芽加霉粒超过播种数的床记为超限。"""
    if bed["sown"] is None:
        return "blank"
    if bed["sprouted"] + bed["moldy"] > bed["sown"]:
        return "over_limit"
    return "normal"


def classify_beds(beds: list[dict]) -> list[dict]:
    marked = []
    for bed in beds:
        state = bed_state(bed)
        row = dict(bed)
        row["state"] = state
        row["excess"] = (
            bed["sprouted"] + bed["moldy"] - bed["sown"]
            if state == "over_limit"
            else 0
        )
        marked.append(row)
    return marked


def over_limit_beds(beds: list[dict]) -> list[dict]:
    return [bed for bed in classify_beds(beds) if bed["state"] == "over_limit"]


def measure(beds: list[dict], protocol: str):
    sown = 0
    sprouted = 0
    moldy = 0
    blank = 0
    for bed in beds:
        if bed["sown"] is None:
            blank += 1
            continue
        if bed["moldy"] > bed["sown"] or bed["sprouted"] > bed["sown"]:
            raise ValueError("某一床的霉粒或发芽数超过了播种数")
        if bed["sprouted"] + bed["moldy"] > bed["sown"]:
            raise ValueError("发芽数与霉粒数加在一起超过了播种数")
        sown += bed["sown"]
        sprouted += bed["sprouted"]
        moldy += bed["moldy"]
    if protocol == LEGACY:
        if sown <= 0:
            raise ValueError("旧口径的分母是零")
        basis = (sprouted * 10000) // sown
        note = "旧口径把霉粒留在分母里"
        if blank:
            note += f"；播种粒空白的 {blank} 床未入账"
        return {
            "protocol_version": LEGACY,
            "tested": sown,
            "excluded_mold": 0,
            "sprouted": sprouted,
            "rate_bp": basis,
            "note": note,
        }
    tested = sown - moldy
    if tested <= 0:
        raise ValueError("剔除霉粒后没有可计种子")
    if sprouted > tested:
        raise ValueError("发芽数大于剔除霉粒后的分母")
    basis = (sprouted * 10000) // tested
    note = "现行口径的分母是播种粒数减去霉粒数"
    if blank:
        note += f"；播种粒空白的 {blank} 床未入账"
    return {
        "protocol_version": CURRENT,
        "tested": tested,
        "excluded_mold": moldy,
        "sprouted": sprouted,
        "rate_bp": basis,
        "note": note,
    }


def apply_revision(beds: list[dict], algorithm: str) -> dict:
    """按剔床算法给出修订结果。

    - 整批不出数：整批不作数，超限粒不进任何分母。
    - 剔除超限床：整床丢掉，其余床按现行口径入账。
    """
    marked = classify_beds(beds)
    over = [bed for bed in marked if bed["state"] == "over_limit"]
    if not over:
        raise ValueError("这个批次没有超限床")
    excluded = [bed["bed_no"] for bed in over]
    excluded_text = ",".join(str(no) for no in excluded)

    if algorithm == VOID_BATCH:
        return {
            "voided": True,
            "tested": None,
            "excluded_mold": None,
            "sprouted": None,
            "rate_bp": None,
            "excluded_beds": excluded_text,
            "note": (
                f"整批不出数：超限床 {excluded_text} 挡下，全批不入账，"
                "超限粒不进分母"
            ),
        }
    if algorithm != DROP_BEDS:
        raise ValueError("未知的剔床算法")

    remaining = [bed for bed in marked if bed["state"] != "over_limit"]
    if not any(bed["sown"] is not None for bed in remaining):
        raise ValueError("剔除超限床后没有可入账的床")
    measured = measure(remaining, CURRENT)
    return {
        "voided": False,
        "tested": measured["tested"],
        "excluded_mold": measured["excluded_mold"],
        "sprouted": measured["sprouted"],
        "rate_bp": measured["rate_bp"],
        "excluded_beds": excluded_text,
        "note": (
            f"剔除超限床 {excluded_text} 后，其余床按现行口径入账，"
            "超限粒不进分母"
        ),
    }
