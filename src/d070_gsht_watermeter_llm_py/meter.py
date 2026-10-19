"""Water-meter reader: camera photo -> Gemini -> editable reading -> Google Sheet."""
import hmac
import io
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import streamlit as st
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from PIL import Image, ImageOps

MAX_JUMP = 150   # m3 between consecutive readings before we ask for confirmation


class Suspicious(Exception):
    """Unusually large jump: can be confirmed by pressing Send again."""


class LowerReading(Exception):
    """New reading is below the last saved one: always rejected."""

IST = ZoneInfo("Asia/Kolkata")
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sept", "oct", "nov", "dec"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
PROMPT = (
    "This is a photo of an Aquamet-style residential water meter with a 6-wheel "
    "black-on-white mechanical digit counter (cubic metres, e.g. 056728). "
    "The photo may be rotated by 90/180 degrees, blurry, or glary; first work out "
    "the orientation so the counter reads left to right. "
    "Read ONLY the six counter wheels. Ignore the three small red-pointer dials "
    "(x0.1, x0.01, x0.001), the serial number (e.g. 18100317) and all printed text. "
    "The LAST wheel is often caught mid-roll, showing two digits partly; in that "
    "case report the LOWER digit (the one that has not fully rolled over). "
    "Do not guess digits you cannot see; set low confidence instead. "
    'Reply ONLY with JSON: {"digits": "<exactly 6 digits, including leading zero>", '
    '"confidence": <0-1>}. If unreadable, {"digits": "", "confidence": 0}.'
)

st.set_page_config(page_title="Water Meter", page_icon="💧", layout="centered")

# ---------------------------------------------------------------- styling
st.markdown("""
<style>
.block-container {max-width: 520px; padding: 1rem 1rem 3rem;}
div[class*="st-key-btn_"] button, [data-testid="stCameraInput"] button,
div[class*="st-key-btn_"] [data-testid="stBaseButton-secondary"] {
    width: 100%; min-height: 3rem; border: none; border-radius: 12px;
    font-weight: 600; font-size: 1rem; color: #fff;
}
.st-key-btn_take button, [data-testid="stCameraInput"] button {background:#4f9d8f;}
.st-key-btn_retake button {background:#e0a458;}
.st-key-btn_submit button {background:#6a8fd8;}
.st-key-btn_send button {background:#7bb274;}
.st-key-btn_unlock button {background:#8a7fc9;}
.st-key-btn_clear button {background:#b0b7c3; min-height:2.2rem;}
div[class*="st-key-btn_"] button:disabled {opacity:.4; color:#fff;}
div[class*="st-key-btn_"] button:hover:not(:disabled),
[data-testid="stCameraInput"] button:hover {filter: brightness(.93); color:#fff;}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------- state
S = st.session_state
for k, v in {"unlocked": False, "camera_open": False, "photo": None,
             "cam_n": 0, "debug": [], "error": None, "success": None,
             "reading_input": ""}.items():
    S.setdefault(k, v)


def redact(text) -> str:
    """Hide URLs, spreadsheet IDs, e-mails and key material from anything shown in the UI."""
    t = str(text)
    secret_bits = []
    try:
        url = st.secrets["sheet"]["url"]
        m = re.search(r"/d/([A-Za-z0-9_-]+)", url)
        secret_bits += [url, m.group(1) if m else ""]
        secret_bits += [st.secrets["gemini"]["api_key"],
                        st.secrets["gcp_service_account"].get("client_email", "")]
    except Exception:
        pass
    for b in secret_bits:
        if b:
            t = t.replace(b, "<hidden>")
    t = re.sub(r"https?://\S+", "<url>", t)
    t = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", t)
    t = re.sub(r"[A-Za-z0-9_-]{30,}", "<id>", t)
    return t


def dbg(msg: str) -> None:
    S.debug.append(f"{datetime.now(IST):%H:%M:%S}  {redact(msg)}")


def clear_debug() -> None:          # on_click callback -> runs before re-render
    S.debug = []


# ---------------------------------------------------------------- backend
def prep_image(raw: bytes, max_side: int = 1600) -> bytes:
    """Apply EXIF rotation and downscale to keep upload fast and cheap."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def read_meter(image_bytes: bytes) -> str:
    image_bytes = prep_image(image_bytes)
    cfg = st.secrets["gemini"]
    dbg(f"LLM call: model={cfg['model']}, image={len(image_bytes)//1024} KB")
    client = genai.Client(api_key=cfg["api_key"])
    resp = client.models.generate_content(
        model=cfg["model"],
        contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json",
                                           temperature=0),
    )
    dbg(f"LLM raw response: {resp.text}")
    data = json.loads(resp.text)
    digits = re.sub(r"\D", "", str(data.get("digits", "")))
    dbg(f"Digits={digits!r}, confidence={data.get('confidence')}")
    # Leading zero(s) are ignored: "056728" -> "56728"
    return digits.lstrip("0")


def _step(name: str) -> None:
    S.last_step = name
    dbg(f"Step: {name}")


def explain_api_error(e: Exception) -> str:
    """Turn a gspread APIError into a message that says what failed and what to check."""
    code = getattr(getattr(e, "response", None), "status_code", "?")
    msg = f"Google rejected the request at step '{S.get('last_step', '?')}' (HTTP {code}): {e}"
    if code == 403:
        msg += ("\n\nCheck that the sheet is still shared as Editor with the service account "
                "(its client_email is in secrets.toml), "
                "that this app is using the same secrets as before, and that the "
                "Sheets API is enabled for that service account's project.")
    elif code == 429:
        msg += "\n\nGoogle rate limit reached; wait a minute and press Send again."
    return msg


def open_spreadsheet():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES)
    return gspread.authorize(creds).open_by_url(st.secrets["sheet"]["url"])


def get_month_tab(ss, now: datetime):
    name = f"{MONTHS[now.month - 1]}-{now.year}"
    m3 = MONTHS[now.month - 1][:3]
    # Google treats tab names case-insensitively, so match that way (and accept sep/sept).
    wanted = {name.lower(), f"{m3}-{now.year}".lower()}
    sheets = ss.worksheets()
    dbg("Existing tabs: " + ", ".join(repr(w.title) for w in sheets))
    for w in sheets:
        if w.title.strip().lower() in wanted:
            dbg(f"Using existing tab {w.title!r}")
            return w
    dbg(f"Tab '{name}' missing; creating from latest tab as template")
    ws = ss.duplicate_sheet(sheets[-1].id, new_sheet_name=name)
    ws.batch_clear(["A2:C"])      # keep headers + formula columns, drop old data
    return ws


def probe_edit_access(ss) -> None:
    """After a failed write, record in the debug panel whether the account has edit rights."""
    try:
        r = ss.client.http_client.request(
            "get", f"https://www.googleapis.com/drive/v3/files/{ss.id}",
            params={"fields": "capabilities/canEdit", "supportsAllDrives": "true"})
        can = r.json().get("capabilities", {}).get("canEdit")
        dbg(f"Access probe: service account canEdit={can} "
            "(False = Viewer/Commenter role; True = a protected range/tab is blocking the write)")
    except Exception as e:
        dbg(f"Access probe failed: {e!r}")


def last_reading(ws):
    """Last numeric value in column C (handles formatted text like '57,415')."""
    vals = [re.sub(r"\D", "", str(v)) for v in ws.col_values(3)[1:]]
    vals = [int(v) for v in vals if v]
    return vals[-1] if vals else None


def fill_formulas_down(ss, ws, row: int) -> None:
    """If column D of this row is empty, copy the formula columns (D onward) from the row above."""
    if row <= 2:
        return
    try:
        if ws.acell(f"D{row}").value:
            return
        ss.batch_update({"requests": [{"copyPaste": {
            "source": {"sheetId": ws.id, "startRowIndex": row - 2, "endRowIndex": row - 1,
                       "startColumnIndex": 3},
            "destination": {"sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                            "startColumnIndex": 3},
            "pasteType": "PASTE_FORMULA"}}]})
        dbg(f"Copied formulas D:… from row {row - 1} to row {row}")
    except Exception as e:      # data is already saved; don't fail the user
        dbg(f"Formula copy skipped: {e!r}")


def write_reading(reading: str, force: bool = False) -> str:
    now = datetime.now(IST)
    _step("open spreadsheet")
    sa = st.secrets["gcp_service_account"]
    dbg("Service account fingerprint: "
        f"{str(sa.get('client_email', ''))[:6]}... / key id ...{str(sa.get('private_key_id', ''))[-4:]}")
    ss = open_spreadsheet()
    _step("find or create month tab")
    ws = get_month_tab(ss, now)
    _step("read existing rows")
    row = len(ws.col_values(1)) + 1
    last = last_reading(ws)
    if last is None:        # first entry of a new month: use the highest last value in other tabs
        others = [last_reading(w) for w in ss.worksheets() if w.id != ws.id]
        others = [v for v in others if v is not None]
        last = max(others) if others else None
    dbg(f"Last saved reading: {last}")
    new = int(reading)
    if last is not None:
        if new < last:
            raise LowerReading(f"{new} is lower than the last saved reading ({last}). Not saved.")
        if new - last > MAX_JUMP and not force:
            raise Suspicious(f"{new} is {new - last} above the last reading ({last}).")
    _step(f"write row {row}")
    values = [[now.strftime("%d-%m-%Y"), now.strftime("%H:%M"), int(reading)]]
    try:
        ws.update(range_name=f"A{row}:C{row}", values=values, value_input_option="RAW")
    except gspread.exceptions.APIError as e:
        probe_edit_access(ss)       # logs whether the account can edit at all
        raise
    dbg(f"Wrote {values[0]} to '{ws.title}' row {row}")
    fill_formulas_down(ss, ws, row)
    return f"{ws.title}, row {row}"


# ---------------------------------------------------------------- handlers
def take_photo():
    S.camera_open = True


def retake():
    S.photo, S.camera_open = None, True
    S.cam_n += 1
    S.reading_input, S.error, S.success = "", None, None
    dbg("Retake pressed")


# ---------------------------------------------------------------- UI
if S.pop("reset_reading", False):
    S.reading_input = ""

st.title("💧 Water Meter")
S.debug_on = S.unlocked and st.radio("Debug", ["Off", "On"], horizontal=True, index=0) == "On"

if not S.unlocked:
    code = st.text_input("Enter passcode", type="password", max_chars=6,
                         label_visibility="visible")
    if st.button("Unlock", key="btn_unlock"):
        if hmac.compare_digest(code, str(st.secrets["passcode"])):
            S.unlocked = True
            dbg("Passcode accepted")
            st.rerun()
        else:
            dbg("Passcode rejected")
            st.error("Incorrect passcode")
else:
    has_photo = S.photo is not None
    c1, c2, c3 = st.columns(3)
    c1.button("Open Camera", key="btn_take", on_click=take_photo,
              disabled=has_photo)
    c2.button("Retake", key="btn_retake", on_click=retake, disabled=not has_photo)
    submit = c3.button("Submit Photo", key="btn_submit", disabled=not has_photo)

    if S.camera_open and not has_photo:
        shot = st.camera_input("Meter photo", key=f"cam_{S.cam_n}",
                               label_visibility="collapsed")
        if shot is not None:
            S.photo, S.camera_open = shot.getvalue(), False
            dbg(f"Photo captured ({len(S.photo)//1024} KB)")
            st.rerun()
    elif has_photo:
        st.image(S.photo, width="stretch")

    if submit:                        # re-submit clears any previous error
        S.error, S.success = None, None
        try:
            with st.spinner("Reading meter..."):
                S.reading_input = read_meter(S.photo)
            if not S.reading_input:
                S.error = "Could not read the meter. Edit the value or Retake."
        except Exception as e:
            dbg(f"LLM error: {e!r}")
            S.error = f"Reading failed: {e}"

    if has_photo:
        st.text_input("Meter reading (editable)", key="reading_input",
                      help="Check against the photo and correct if needed.")
        if st.button("Send Reading", key="btn_send"):
            S.error, S.success = None, None
            r = S.reading_input.strip()
            if not r.isdigit():
                S.error = "Enter a numeric reading."
            else:
                try:
                    with st.spinner("Saving..."):
                        where = write_reading(r, force=(S.get("confirm_val") == r))
                    S.confirm_val = None
                    S.success = f"Saved {int(r)} to {where}"
                    S.photo, S.camera_open = None, False
                    S.cam_n += 1
                    S.reset_reading = True      # cleared at top of next run, before the widget exists
                    st.rerun()
                except LowerReading as e:
                    S.confirm_val = None
                    dbg(f"Blocked: {e}")
                    S.error = str(e)
                except Suspicious as e:
                    S.confirm_val = r
                    dbg(f"Sanity check: {e}")
                    S.error = f"{e} Check the photo; press Send again to save anyway."
                except gspread.exceptions.APIError as e:
                    dbg(f"Sheet API error: {e!r}")
                    S.error = explain_api_error(e)
                except Exception as e:
                    dbg(f"Sheet error: {e!r}")
                    S.error = f"Saving failed: {e}"

    if S.error:
        st.error(redact(S.error))
    if S.success:
        st.success(redact(S.success))

if S.debug_on:
    st.divider()
    st.caption("Debug messages")
    st.button("Clear debug messages", key="btn_clear", on_click=clear_debug)
    st.code("\n".join(S.debug) or "(empty)", language=None)