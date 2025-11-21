import base64
import re
import time
import uuid
from io import BytesIO

from flask import Flask, jsonify, request
import requests
from requests.utils import dict_from_cookiejar, cookiejar_from_dict

# ----------------- GST URLs ----------------- #
GST_BASE = "https://services.gst.gov.in/"
GST_SEARCH_PAGE = GST_BASE + "services/searchtp"
GST_CAPTCHA = GST_BASE + "services/captcha"
GST_TAXPAYER = GST_BASE + "services/api/search/taxpayerDetails"
GST_GOOD_SERVICE = GST_BASE + "services/api/search/goodservice"

app = Flask(__name__)

# -------- Session Storage -------- #
SESSION_TTL = 300  # 5 min TTL
SESSION_STORE = {}  # sessionId → cookie jar

GSTIN_REGEX = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$", re.IGNORECASE
)

CORE_BUSINESS_MAP = {
    "SPO": "Service Provider and Others",
    "MFR": "Manufacturer",
    "TRD": "Trader",
    "WOR": "Works Contractor",
    "OTH": "Others",
}


# --------------------------------------------------------------------
#  VALIDATE GSTIN FORMAT
# --------------------------------------------------------------------
def validate_gstin(gstin: str) -> bool:
    return bool(GSTIN_REGEX.match(gstin or ""))


# --------------------------------------------------------------------
#  CLEAN EXPIRED SESSIONS
# --------------------------------------------------------------------
def cleanup_sessions():
    now = time.time()
    expired = [
        sid for sid, d in SESSION_STORE.items()
        if now - d["created_at"] > SESSION_TTL
    ]
    for sid in expired:
        SESSION_STORE.pop(sid, None)


# --------------------------------------------------------------------
#  START GST SESSION → SET COOKIES + GET CAPTCHA
# --------------------------------------------------------------------
def start_session():
    s = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 Chrome/122 Safari/537.36"}

    s.get(GST_SEARCH_PAGE, headers=headers)  # load cookies

    cap = s.get(GST_CAPTCHA, headers=headers)
    cap.raise_for_status()

    img_b64 = "data:image/png;base64," + base64.b64encode(cap.content).decode()

    return s, img_b64


# --------------------------------------------------------------------
#  PARSE JURISDICTION STRING
# --------------------------------------------------------------------
def parse_jurisdiction(raw: str, tag: str) -> str:
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",")]
    return "\n".join([f"({tag})"] + parts)


# --------------------------------------------------------------------
#  PRINCIPAL PLACE ADDRESS
# --------------------------------------------------------------------
def parse_principal(pradr: dict) -> str:
    if not isinstance(pradr, dict):
        return ""

    # Case 1: flat string
    if "adr" in pradr and isinstance(pradr["adr"], str):
        return pradr["adr"]

    # Case 2: nested address fields
    addr = pradr.get("addr", {})
    parts = [
        addr.get("bno"),
        addr.get("bnm"),
        addr.get("flno"),
        addr.get("st"),
        addr.get("loc"),
        addr.get("dst"),
        addr.get("stcd"),
        addr.get("pncd"),
    ]
    return ", ".join([p for p in parts if p])


# --------------------------------------------------------------------
#  HSN / SAC LIST
# --------------------------------------------------------------------
def extract_hsn(goods_json: dict):
    if not isinstance(goods_json, dict):
        return []
    for key in ["bzsdtls", "bzsdts", "bzsDtlS", "BZSdTLS"]:
        if key in goods_json and isinstance(goods_json[key], list):
            return goods_json[key]
    return []


# --------------------------------------------------------------------
#  MAP RAW GST DATA → CLEAN JSON OUTPUT
# --------------------------------------------------------------------
def map_vendor(raw, goods_json):
    admin_off = parse_jurisdiction(raw.get("ctj"), "JURISDICTION - CENTER")
    other_off = parse_jurisdiction(raw.get("stj"), "JURISDICTION - STATE")
    principal = parse_principal(raw.get("pradr", {}))

    # Core Business (ntcrbs)
    core_code = (raw.get("ntcrbs") or "").upper().strip()
    core_business = CORE_BUSINESS_MAP.get(core_code, core_code)

    # Business Activities (nba)
    nba_list = raw.get("nba", []) or []
    business_activity = ", ".join(nba_list)

    # HSN/SAC
    hsn_clean = [
        {"code": item.get("saccd", ""), "desc": item.get("sdes", "")}
        for item in extract_hsn(goods_json)
    ]

    return {
        "gstin": raw.get("gstin"),
        "legalName": raw.get("lgnm"),
        "tradeName": raw.get("tradeNam"),
        "registrationDate": raw.get("rgdt"),
        "constitution": raw.get("ctb"),
        "status": raw.get("sts"),
        "taxpayerType": raw.get("dty"),
        "aadhaarAuth": raw.get("adhrVFlag"),
        "eKyc": raw.get("ekycVFlag"),
        "coreBusiness": core_business,
        "businessActivities": business_activity,
        "adminOffice": admin_off,
        "otherOffice": other_off,
        "principalPlace": principal,
        "hsnList": hsn_clean,
    }


# --------------------------------------------------------------------
#  API : INIT SESSION
# --------------------------------------------------------------------
@app.post("/gst/init")
def api_init():
    try:
        cleanup_sessions()
        session, captcha_b64 = start_session()

        sid = str(uuid.uuid4())
        SESSION_STORE[sid] = {
            "cookies": dict_from_cookiejar(session.cookies),
            "created_at": time.time(),
        }

        return jsonify({
            "success": True,
            "sessionId": sid,
            "captcha": captcha_b64
        })

    except Exception as e:
        return jsonify({"success": False, "error": "INIT_FAILED"}), 500


# --------------------------------------------------------------------
#  API : REFRESH CAPTCHA
# --------------------------------------------------------------------
@app.post("/gst/captcha")
def api_captcha():
    data = request.get_json() or {}
    sid = data.get("sessionId")

    if not sid or sid not in SESSION_STORE:
        return jsonify({"success": False, "error": "SESSION_EXPIRED"}), 410

    sess = SESSION_STORE[sid]
    s = requests.Session()
    s.cookies = cookiejar_from_dict(sess["cookies"])

    cap = s.get(GST_CAPTCHA)
    if cap.status_code != 200:
        return jsonify({"success": False, "error": "CAPTCHA_FAILED"}), 500

    img_b64 = "data:image/png;base64," + base64.b64encode(cap.content).decode()

    sess["cookies"] = dict_from_cookiejar(s.cookies)
    sess["created_at"] = time.time()

    return jsonify({"success": True, "captcha": img_b64})


# --------------------------------------------------------------------
#  API : VERIFY GST
# --------------------------------------------------------------------
@app.post("/gst/verify")
def api_verify():
    try:
        payload = request.get_json() or {}
        gstin = payload.get("gstin", "").upper().strip()
        captcha = payload.get("captcha", "")
        sid = payload.get("sessionId")

        if not validate_gstin(gstin):
            return jsonify({"success": False, "error": "INVALID_GSTIN"}), 400

        if sid not in SESSION_STORE:
            return jsonify({"success": False, "error": "SESSION_EXPIRED"}), 410

        session = requests.Session()
        session.cookies = cookiejar_from_dict(SESSION_STORE[sid]["cookies"])

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Origin": "https://services.gst.gov.in",
            "Referer": "https://services.gst.gov.in/services/searchtp"
        }

        # ------- Taxpayer details -------
        r1 = session.post(GST_TAXPAYER, json={
            "gstin": gstin,
            "captcha": captcha
        }, headers=headers)

        data = r1.json()

        if "error" in data:
            return jsonify({"success": False, "error": "CAPTCHA_INVALID"}), 400

        # ------- Goods & Services -------
        r2 = session.get(f"{GST_GOOD_SERVICE}?gstin={gstin}", headers=headers)
        goods_json = r2.json() if r2.status_code == 200 else {}

        mapped = map_vendor(data, goods_json)

        return jsonify({
            "success": True,
            "vendor": mapped,
            "raw": data,
            "goodsService": goods_json
        })

    except Exception as e:
        return jsonify({"success": False, "error": "VERIFY_FAILED"}), 500


# ----------------- RUN SERVER ----------------- #
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
