import base64
import re
import time
import uuid

from io import BytesIO

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from requests.utils import dict_from_cookiejar, cookiejar_from_dict

# ----------------- GST URLs ----------------- #
GST_BASE = "https://services.gst.gov.in/"
GST_SEARCH_PAGE = GST_BASE + "services/searchtp"
GST_CAPTCHA = GST_BASE + "services/captcha"
GST_TAXPAYER = GST_BASE + "services/api/search/taxpayerDetails"
GST_GOOD_SERVICE = GST_BASE + "services/api/search/goodservice"

app = Flask(__name__)

# Enable CORS for all /gst/* endpoints (frontend can call from any origin)
CORS(
    app,
    supports_credentials=True,
    resources={
        r"/*": {
            "origins": [
                "http://localhost:5173",
                "https://dev-navlik.omnierp.ai/"
            ]
        }
    }
)
# -------- Session Storage -------- #
SESSION_TTL = 300  # 5 min TTL
SESSION_STORE = {}  # sessionId → cookie jar

GSTIN_REGEX = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$",
    re.IGNORECASE,
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
        sid
        for sid, d in SESSION_STORE.items()
        if now - d["created_at"] > SESSION_TTL
    ]
    for sid in expired:
        SESSION_STORE.pop(sid, None)


# --------------------------------------------------------------------
#  START GST SESSION → SET COOKIES + GET CAPTCHA
# --------------------------------------------------------------------
def start_session():
    """
    1) Hit GST search page to get cookies
    2) Fetch captcha image
    """
    s = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0 Chrome/122 Safari/537.36"}

    # Load page to get cookies
    s.get(GST_SEARCH_PAGE, headers=headers)

    # Get captcha image
    cap = s.get(GST_CAPTCHA, headers=headers)
    cap.raise_for_status()

    img_b64 = (
        "data:image/png;base64,"
        + base64.b64encode(cap.content).decode()
    )

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
    admin_off = parse_jurisdiction(
        raw.get("ctj"), "JURISDICTION - CENTER"
    )
    other_off = parse_jurisdiction(
        raw.get("stj"), "JURISDICTION - STATE"
    )
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
#  API : INIT SESSION → get captcha + sessionId
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

        # NOTE: using captchaImageBase64 so frontend can use data.captchaImageBase64
        return jsonify(
            {
                "success": True,
                "sessionId": sid,
                "captchaImageBase64": captcha_b64,
            }
        )

    except Exception:
        return jsonify({"success": False, "error": "INIT_FAILED"}), 500


# --------------------------------------------------------------------
#  API : REFRESH CAPTCHA
# --------------------------------------------------------------------
@app.post("/gst/captcha")
def api_captcha():
    data = request.get_json() or {}
    sid = data.get("sessionId")

    if not sid or sid not in SESSION_STORE:
        return (
            jsonify({"success": False, "error": "SESSION_EXPIRED"}),
            410,
        )

    sess = SESSION_STORE[sid]
    s = requests.Session()
    s.cookies = cookiejar_from_dict(sess["cookies"])

    cap = s.get(GST_CAPTCHA)
    if cap.status_code != 200:
        return (
            jsonify({"success": False, "error": "CAPTCHA_FAILED"}),
            500,
        )

    img_b64 = (
        "data:image/png;base64,"
        + base64.b64encode(cap.content).decode()
    )

    sess["cookies"] = dict_from_cookiejar(s.cookies)
    sess["created_at"] = time.time()

    # Again: same key as init
    return jsonify({"success": True, "captchaImageBase64": img_b64})


# --------------------------------------------------------------------
#  API : VERIFY GST
# --------------------------------------------------------------------
@app.post("/gst/verify")
def api_verify():
    try:
        payload = request.get_json() or {}
        gstin = payload.get("gstin", "").upper().strip()
        captcha = payload.get("captcha", "").strip()
        sid = payload.get("sessionId")

        # Check if GSTIN format is invalid - return 400 error
        if not validate_gstin(gstin):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "The GSTIN/UIN that you have entered is invalid. Please enter a valid GSTIN/UIN."
                    }
                ),
                400,
            )

        # Check if captcha is empty
        if not captcha:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Enter valid letters shown in the image below"
                    }
                ),
                400,
            )

        if sid not in SESSION_STORE:
            return (
                jsonify(
                    {"success": False, "error": "SESSION_EXPIRED"}
                ),
                410,
            )

        session = requests.Session()
        session.cookies = cookiejar_from_dict(
            SESSION_STORE[sid]["cookies"]
        )

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
            "Origin": "https://services.gst.gov.in",
            "Referer": "https://services.gst.gov.in/services/searchtp",
        }

        # ------- Taxpayer details -------
        r1 = session.post(
            GST_TAXPAYER,
            json={"gstin": gstin, "captcha": captcha},
            headers=headers,
        )

        # Log the response for debugging (remove in production)
        print(f"========== GST API DEBUG ==========")
        print(f"Status Code: {r1.status_code}")
        print(f"Response Headers: {dict(r1.headers)}")
        print(f"Response Text: {r1.text}")
        print(f"Input GSTIN: {gstin}")
        print(f"Input Captcha: {captcha}")
        print(f"===================================")

        # Handle HTTP error responses (400, 500, etc.)
        if r1.status_code != 200:
            error_text = r1.text.strip()
            
            print(f"ERROR DETECTED - Analyzing: '{error_text}'")
            
            # Check if it's a captcha error
            # GST API returns plain text error messages for captcha failures
            if "captcha" in error_text.lower():
                print("CAPTCHA ERROR DETECTED")
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Enter valid letters shown in the image below"
                        }
                    ),
                    400,
                )
            
            # Check if it's a GSTIN error
            if "gstin" in error_text.lower() or "invalid" in error_text.lower():
                print("GSTIN ERROR DETECTED")
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "The GSTIN/UIN that you have entered is invalid. Please enter a valid GSTIN/UIN."
                        }
                    ),
                    400,
                )
            
            # Generic error
            print("GENERIC ERROR")
            return (
                jsonify(
                    {
                        "success": False,
                        "error": error_text or "GST verification failed"
                    }
                ),
                400,
            )

        data = r1.json()

        # Check if there's an errorCode in the response (GST API standard error format)
        if "errorCode" in data:
            error_code = data.get("errorCode", "")
            error_message = data.get("message", "")
            
            print(f"Error Code detected: {error_code}, Message: {error_message}")
            
            # SWEB_9000 is the captcha validation error code
            if error_code == "SWEB_9000":
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Enter valid letters shown in the image below"
                        }
                    ),
                    400,
                )
            
            # Handle other error codes
            return (
                jsonify(
                    {
                        "success": False,
                        "error": error_message or f"Verification failed (Error: {error_code})"
                    }
                ),
                400,
            )

        # Check if there's an error in the response (wrong captcha or GSTIN not found)
        if "error" in data:
            error_detail = data.get("error", {})
            
            # error_detail could be a string or dict
            if isinstance(error_detail, dict):
                error_message = error_detail.get("message", "")
                error_code = error_detail.get("code", "")
            else:
                error_message = str(error_detail)
                error_code = ""
            
            print(f"Error detected - Message: {error_message}, Code: {error_code}")
            
            # Handle invalid captcha error - check multiple conditions
            if (
                "captcha" in error_message.lower() 
                or "captcha" in error_code.lower()
                or error_code == "INVALID_CAPTCHA"
            ):
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "400 Enter valid letters shown in the image below"
                        }
                    ),
                    400,
                )
            
            # Handle GSTIN not found or other errors
            return (
                jsonify(
                    {
                        "success": False,
                        "error": error_message or "GSTIN verification failed"
                    }
                ),
                400,
            )
        
        # Check if GSTIN is valid but doesn't exist in database
        # The GST API returns empty/null for these key fields when GSTIN is not found
        if not data.get("gstin") or not data.get("lgnm"):
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "400 The GSTIN/UIN that you have entered is invalid. Please enter a valid GSTIN/UIN."
                    }
                ),
                400,
            )



        # ------- Goods & Services -------
        r2 = session.get(
            f"{GST_GOOD_SERVICE}?gstin={gstin}", headers=headers
        )
        goods_json = r2.json() if r2.status_code == 200 else {}

        mapped = map_vendor(data, goods_json)

        return jsonify(
            {
                "success": True,
                "vendor": mapped,
                "raw": data,
                "goodsService": goods_json,
            }
        )

    except Exception:
        return (
            jsonify({"success": False, "error": "VERIFY_FAILED"}),
            500,
        )


# ----------------- RUN SERVER (local dev) ----------------- #
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
