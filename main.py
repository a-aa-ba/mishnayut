import os
import io
import traceback
import requests
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import speech_recognition as sr
from pydub import AudioSegment
from pydub.effects import normalize

app = FastAPI(title="Gemach Reisman IVR Server")

SCRIPT_URL = os.environ.get(
    "SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbyMS9azkxb4nCw99g4Il5n8vIfEKJTUedb1A80vI51PqpQh2BnCPmQ9X0e_2eS3SZQ3hw/exec"
)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "").strip()
sessions = {}

SEDARIM = {
    "1": "זרעים", "2": "מועד", "3": "נשים",
    "4": "נזיקין", "5": "קדשים", "6": "טהרות"
}

def log_ivr(call_id: str, message: str):
    """רישום לוג עברי נקי לטרמינל"""
    print(f"📞 [שיחה: {call_id}] {message}")

def clean_tts(text: str) -> str:
    """ניקוי תווים מפריעים במנוע הדיבור"""
    if not text:
        return ""
    for ch in [".", ",", ":", ";", '"', "'", "&", "=", "/", "\\", "-"]:
        text = text.replace(ch, " ")
    return " ".join(text.split())

def format_sheets_text(val: str, zfill_len: int = 0) -> str:
    """שימור אפס מוביל עבור גוגל שיטס"""
    s = str(val or "").strip()
    if zfill_len > 0:
        s = s.zfill(zfill_len)
    return f"'{s}" if not s.startswith("'") else s

# =========================================================================
# פקודות ימות המשיח בשורה אחת לפי מסמך ה-API המצורף
# =========================================================================

def read(text: str, yemot_params: str) -> PlainTextResponse:
    """מחזיר פקודת read בשורה אחת עם פסיקים: read=t-הודעה=משתנה,ערכים..."""
    return PlainTextResponse(f"read=t-{clean_tts(text)}={yemot_params}")

def yemot_msg(text: str, go_to="hangup") -> PlainTextResponse:
    """השמעת הודעה סופית והעברה לשלוחה / ניתוק"""
    return PlainTextResponse(f"id_list_message=t-{clean_tts(text)}&go_to_folder={go_to}")

def get_restart_folder(p: dict) -> str:
    ext = p.get("ApiExtension", "").strip("/")
    return f"/{ext}" if ext else "/"

def send_admin_email(phone: str, file_url: str, call_id="כללי"):
    log_ivr(call_id, f"שולח בקשה לשליחת מייל עבור טלפון: {phone}")
    try:
        requests.get(
            SCRIPT_URL,
            params={
                "action": "send_message_email",
                "phone": format_sheets_text(phone),
                "file_url": file_url
            },
            timeout=10
        )
        log_ivr(call_id, "המייל נשלח בהצלחה דרך Apps Script!")
    except Exception as e:
        log_ivr(call_id, f"שגיאה בשליחת מייל: {e}")

def transcribe_yemot_audio(file_path: str, call_id: str):
    log_ivr(call_id, f"מתחיל הורדת הקלטה ותמלול: {file_path}")
    if not YEMOT_TOKEN or not file_path:
        return None

    clean_p = file_path.strip()
    while clean_p.startswith("ivr2:") or clean_p.startswith("/"):
        if clean_p.startswith("ivr2:"): clean_p = clean_p[5:]
        if clean_p.startswith("/"): clean_p = clean_p[1:]
    
    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:/{clean_p}"

    try:
        resp = requests.get(download_url, timeout=12)
        if resp.status_code != 200 or len(resp.content) < 300 or resp.content.startswith(b"{\""):
            return None

        sound = AudioSegment.from_file(io.BytesIO(resp.content))
        sound = normalize(sound)
        sound = sound.set_frame_rate(16000).set_channels(1)
        
        wav_io = io.BytesIO()
        sound.export(wav_io, format="wav")
        wav_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="he-IL")
            log_ivr(call_id, f"זיהוי דיבור הצליח: '{text.strip()}'")
            return text.strip()
    except Exception as e:
        log_ivr(call_id, f"שגיאה בתמלול: {e}")
        return None

# =========================================================================
# ניהול מהלך השיחה
# =========================================================================
async def handle_call(request: Request):
    p = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            p.update(dict(form))
        except Exception:
            pass

    call_id = p.get("ApiCallId", p.get("ApiPhone", "כללי"))

    if p.get("hangup") == "yes":
        log_ivr(call_id, "השיחה נותקה")
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    if call_id not in sessions:
        sessions[call_id] = {
            "caller_phone": p.get("ApiPhone", ""),
            "msg_attempt": 1,
            "name_attempt": 1,
            "addr_attempt": 1
        }
        log_ivr(call_id, f"שיחה חדשה ממספר: {p.get('ApiPhone', 'לא ידוע')}")

    sess = sessions[call_id]
    restart_folder = get_restart_folder(p)

    # 1. תפריט ראשי
    user_menu_input = p.get("user_menu", "").strip()
    if "user_menu" not in sess:
        if not user_menu_input or user_menu_input == "None":
            msg = "ברוך הבא לגמח משניות רייזמן להשאלה פרטי משתמש משתמש רשום הקש 1 לרישום משתמש חדש הקש 2 להשארת הודעה לגמח הקש 3"
            # הגדרה בשורה אחת עם פסיקים: משתנה, ללא שימוש קודם, 1 מקסימום, 1 מינימום, 5 שניות, ללא השמעה, מקשים 1-3, פעם 1, Ok אם ריק, None, ללא אישור
            return read(msg, "user_menu,no,1,1,5,NO,,,,1.2.3,1,Ok,None,,no")
        sess["user_menu"] = user_menu_input

    menu_choice = sess.get("user_menu")

    # --- שלוחה 3: השארת הודעה לגמ"ח ---
    if menu_choice == "3":
        msg_att = sess["msg_attempt"]
        msg_audio_var = f"message_audio_{msg_att}"
        msg_confirm_var = f"msg_confirm_{msg_att}"

        if msg_audio_var not in p and sess.get(f"msg_recorded_{msg_att}") is not True:
            # הגדרה בשורה אחת: משתנה, ללא שימוש קודם, record, תיקייה ברירת מחדל, ללא תפריט M3345, שמירה בניתוק
            return read("הקלט את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", f"{msg_audio_var},no,record,,,no,yes")

        if msg_audio_var in p:
            sess[f"msg_audio_path_{msg_att}"] = p.get(msg_audio_var)
            sess[f"msg_recorded_{msg_att}"] = True

        if msg_confirm_var not in p:
            # הגדרה בשורה אחת: מקש 1 או 2, 6 שניות, ללא השמעה, ללא אישור
            return read("לאישור ההקלטה ושליחתה הקש 1 להקלטה מחדש הקש 2", f"{msg_confirm_var},no,1,1,6,NO,,,,1.2,,,,,no")

        confirm_choice = p.get(msg_confirm_var)
        if confirm_choice == "1":
            raw_audio = sess.get(f"msg_audio_path_{msg_att}", "").replace("ivr2:", "").lstrip("/")
            download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:/{raw_audio}"
            send_admin_email(sess.get("caller_phone", ""), download_url, call_id)
            sessions.pop(call_id, None)
            return yemot_msg("הודעתך נשמרה ונשלחה בהצלחה תודה ולהתראות", go_to=restart_folder)
        elif confirm_choice == "2":
            sess["msg_attempt"] += 1
            new_att = sess["msg_attempt"]
            return read("הקלט שוב את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", f"message_audio_{new_att},no,record,,,no,yes")

    # --- שלוחה 1: משתמש רשום ---
    if menu_choice == "1":
        if "id_num" not in p:
            # ת.ז: עד 9 ספרות, מינימום 8, 10 שניות, השמעת ספרות, אישור M1353
            return read("הקש את מספר הזהות שלך ולאחריו הקש סולמית", "id_num,no,9,8,10,Digits,,,,,,,,yes")

        sess["id_num"] = str(p.get("id_num")).strip().zfill(9)

        if "user_verified" not in sess:
            try:
                res = requests.get(f"{SCRIPT_URL}?action=verify_registered&id_number={sess['id_num']}", timeout=6).json()
            except Exception:
                res = {"success": False}

            if res.get("success"):
                sess["user_verified"] = True
                sess["name"] = res.get("name", "משתמש רשום")
                sess["phone"] = res.get("phone", sess.get("caller_phone", ""))
                sess["user_type"] = "רשום"
            else:
                sess.pop("id_num", None)
                return read("מספר הזהות אינו מופיע במערכת אנא הקישו שוב ולאחריו סולמית", "id_num,no,9,8,10,Digits,,,,,,,,yes")

    # --- שלוחה 2: רישום משתמש חדש ---
    if menu_choice == "2":
        sess["user_type"] = "חדש"

        # ת.ז חדש
        if "reg_id" not in p and "id_num" not in sess:
            return read("לרישום הקישו את מספר הזהות שלכם בן תשע ספרות ולאחריו סולמית", "reg_id,no,9,8,10,Digits,,,,,,,,yes")

        if "reg_id" in p and "id_num" not in sess:
            entered_id = str(p.get("reg_id")).strip().zfill(9)
            try:
                check_existing = requests.get(f"{SCRIPT_URL}?action=verify_registered&id_number={entered_id}", timeout=6).json()
            except Exception:
                check_existing = {"success": False}

            if check_existing.get("success"):
                sess["id_num"] = entered_id
                sess["name"] = check_existing.get("name", "משתמש רשום")
                sess["phone"] = check_existing.get("phone", sess.get("caller_phone", ""))
                sess["user_type"] = "רשום"
                sess["user_verified"] = True
                sess["user_saved"] = True
                sess["skip_to_book"] = True
            else:
                sess["id_num"] = entered_id

        # טלפון
        if "reg_phone" not in p and "phone" not in sess and "id_num" in sess and not sess.get("skip_to_book"):
            return read("הקישו את מספר הטלפון שלכם ולאחריו סולמית", "reg_phone,no,10,9,10,Digits,,,,,,,,yes")

        if "reg_phone" in p and "phone" not in sess:
            sess["phone"] = str(p.get("reg_phone")).strip()

        # הקלטת שם
        if "name" not in sess and "phone" in sess and not sess.get("skip_to_book"):
            n_att = sess["name_attempt"]
            name_audio_var = f"reg_name_audio_{n_att}"
            name_confirm_var = f"reg_name_confirm_{n_att}"

            if name_audio_var not in p and sess.get(f"name_transcribed_{n_att}") is not True:
                return read("אמרו בקול ברור את שמכם הפרטי ושם המשפחה ולאחר מכן הקשו סולמית", f"{name_audio_var},no,record,,,no,yes")

            if name_audio_var in p and sess.get(f"name_transcribed_{n_att}") is not True:
                transcribed_name = transcribe_yemot_audio(p.get(name_audio_var), call_id)
                if not transcribed_name:
                    sess["name_attempt"] += 1
                    return read("הדיבור אינו ברור אנא נסו שנית ואמרו את שמכם המלא ולאחר מכן הקשו סולמית", f"reg_name_audio_{sess['name_attempt']},no,record,,,no,yes")

                sess[f"temp_name_{n_att}"] = transcribed_name
                sess[f"name_transcribed_{n_att}"] = True

            if name_confirm_var not in p:
                t_name = sess.get(f"temp_name_{n_att}", "")
                msg = f"השם שנקלט הוא {t_name} לאישור הקשו 1 להקלטה מחודשת הקשו 2"
                return read(msg, f"{name_confirm_var},no,1,1,6,NO,,,,1.2,,,,,no")

            name_choice = p.get(name_confirm_var)
            if name_choice == "1":
                sess["name"] = sess.get(f"temp_name_{n_att}", "משתמש חדש")
            elif name_choice == "2":
                sess["name_attempt"] += 1
                return read("אמרו שוב בקול ברור את שמכם המלא ולאחר מכן הקשו סולמית", f"reg_name_audio_{sess['name_attempt']},no,record,,,no,yes")

        # הקלטת כתובת
        if "address" not in sess and "name" in sess and not sess.get("skip_to_book"):
            a_att = sess["addr_attempt"]
            addr_audio_var = f"reg_addr_audio_{a_att}"
            addr_confirm_var = f"reg_addr_confirm_{a_att}"

            if addr_audio_var not in p and sess.get(f"addr_transcribed_{a_att}") is not True:
                return read("אמרו בקול ברור את הכתובת המלאה שלכם ולאחר מכן הקשו סולמית", f"{addr_audio_var},no,record,,,no,yes")

            if addr_audio_var in p and sess.get(f"addr_transcribed_{a_att}") is not True:
                transcribed_addr = transcribe_yemot_audio(p.get(addr_audio_var), call_id)
                if not transcribed_addr:
                    sess["addr_attempt"] += 1
                    return read("הדיבור אינו ברור אנא נסו שנית ואמרו את הכתובת המלאה ולאחר מכן הקשו סולמית", f"reg_addr_audio_{sess['addr_attempt']},no,record,,,no,yes")

                sess[f"temp_addr_{a_att}"] = transcribed_addr
                sess[f"addr_transcribed_{a_att}"] = True

            if addr_confirm_var not in p:
                t_addr = sess.get(f"temp_addr_{a_att}", "")
                msg = f"הכתובת שנקלטה היא {t_addr} לאישור הקשו 1 להקלטה מחודשת הקשו 2"
                return read(msg, f"{addr_confirm_var},no,1,1,6,NO,,,,1.2,,,,,no")

            addr_choice = p.get(addr_confirm_var)
            if addr_choice == "1":
                sess["address"] = sess.get(f"temp_addr_{a_att}", "-")
            elif addr_choice == "2":
                sess["addr_attempt"] += 1
                return read("אמרו שוב בקול ברור את הכתובת המלאה ולאחר מכן הקשו סולמית", f"reg_addr_audio_{sess['addr_attempt']},no,record,,,no,yes")

        # שמירת המשתמש
        if "user_saved" not in sess and "address" in sess:
            id_val = format_sheets_text(sess.get("id_num", "-"), 9)
            name_val = sess.get("name", "משתמש חדש")
            phone_val = format_sheets_text(sess.get("phone", sess.get("caller_phone", "")))
            addr_val = sess.get("address", "-")

            try:
                requests.get(
                    SCRIPT_URL,
                    params={
                        "action": "register_casual",
                        "id_number": id_val,
                        "name": name_val,
                        "phone": phone_val,
                        "address": addr_val,
                        "source": "טלפוני"
                    },
                    timeout=8
                )
            except Exception as e:
                log_ivr(call_id, f"שגיאה בשמירת משתמש: {e}")
            sess["user_saved"] = True

    # --- בחירת כרך ---
    if "book_choice" not in p and "book_choice" not in sess:
        msg = "לבחירת כרך באמצעות מספר סידורי בן שש ספרות הקש 1 לבחירה בשלבים לפי סניף סדר וכרך הקש 2 להסבר על המספר הסידורי הקש 9"
        if sess.get("skip_to_book"):
            msg = f"מספר זהות זה כבר רשום במערכת שלום ל {sess.get('name')} לבחירת כרך במספר סידורי הקש 1 לבחירה בשלבים הקש 2 להסבר הקש 9"
            sess.pop("skip_to_book", None)

        return read(msg, "book_choice,no,1,1,6,NO,,,,1.2.9,,,,,no")

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")

    b_choice = sess.get("book_choice")

    if b_choice == "9":
        sess.pop("book_choice", None)
        msg = "הסבר על המספר הסידורי המספר מורכב משש ספרות שלוש הספרות הראשונות הן מספר הסניף הספרה הרביעית היא מספר הסדר מאחת עד שש ושתי הספרות האחרונות הן מספר הכרך לבחירה במספר סידורי בן שש ספרות הקש אחת לבחירה בשלבים הקש שתיים"
        return read(msg, "book_choice,no,1,1,8,NO,,,,1.2,,,,,no")

    # מקש 1: מספר סידורי בן 6 ספרות
    if b_choice == "1":
        if "serial_num" not in p:
            return read("אנא הקישו את המספר הסידורי בן שש הספרות ולאחריו סולמית", "serial_num,no,6,6,10,NO,,,,,,,,no")

        raw_num = str(p.get("serial_num", "")).strip()
        branch = raw_num[:3]
        seder_digit = raw_num[3:4]
        volume = raw_num[4:6]
        seder_name = SEDARIM.get(seder_digit, f"סדר {seder_digit}")

        sess["book_id"] = raw_num
        sess["seder_mishna"] = f"סניף {branch} - סדר {seder_name} - כרך {volume}"

    # מקש 2: בחירה בשלבים
    elif b_choice == "2":
        if "step_branch" not in p and "step_branch" not in sess:
            return read("הקישו את מספר הסניף בן שלוש ספרות ולאחריו סולמית", "step_branch,no,3,3,8,Digits,,,,,,,,yes")

        if "step_branch" in p:
            sess["step_branch"] = p.get("step_branch")

        if "step_seder" not in p and "step_seder" not in sess:
            msg = "הקישו את מספר הסדר. 1 זרעים 2 מועד 3 נשים 4 נזיקין 5 קדשים 6 טהרות "
            return read(msg, "step_seder,no,1,1,8,NO,,,,1.2.3.4.5.6,,,,,no")

        if "step_seder" in p:
            sess["step_seder"] = p.get("step_seder")

        if "step_vol" not in p and "step_vol" not in sess:
            return read("הקישו את מספר הכרך בן שתי ספרות ולאחריו סולמית", "step_vol,no,2,1,8,Digits,,,,,,,,yes")

        if "step_vol" in p:
            sess["step_vol"] = p.get("step_vol")

        full_branch = str(sess.get("step_branch", "000")).zfill(3)
        seder_digit = str(sess.get("step_seder", "1"))
        full_vol = str(sess.get("step_vol", "01")).zfill(2)
        full_serial = f"{full_branch}{seder_digit}{full_vol}"

        s_name = SEDARIM.get(seder_digit, f"סדר {seder_digit}")
        sess["book_id"] = full_serial
        sess["seder_mishna"] = f"סניף {full_branch} - סדר {s_name} - כרך {full_vol}"

    # --- תפריט השאלה / החזרה ---
    if "action_choice" not in p:
        return read("להשאלת המשנה הקש 1 להחזרת המשנה הקש 2", "action_choice,no,1,1,6,NO,,,,1.2,,,,,no")

    act = p.get("action_choice")

    # השאלה
    if act == "1":
        try:
            requests.get(
                SCRIPT_URL,
                params={
                    "action": "borrow",
                    "bookId": sess.get("book_id", "-"),
                    "sederMishna": sess.get("seder_mishna", "-"),
                    "name": sess.get("name", "משאיל"),
                    "phone": format_sheets_text(sess.get("phone", "")),
                    "address": sess.get("address", "-"),
                    "userType": sess.get("user_type", "כללי"),
                    "id_number": format_sheets_text(sess.get("id_num", "-"), 9),
                    "source": "טלפוני"
                },
                timeout=8
            )
        except Exception as e:
            log_ivr(call_id, f"שגיאה בהשאלה: {e}")

        sessions.pop(call_id, None)
        return yemot_msg("העדכון נקלט בהצלחה תודה ולהתראות", go_to=restart_folder)

    # החזרה
    elif act == "2":
        try:
            res = requests.get(
                SCRIPT_URL,
                params={
                    "action": "return",
                    "bookId": sess.get("book_id", "-"),
                    "phone": format_sheets_text(sess.get("phone", "")),
                    "source": "טלפוני"
                },
                timeout=8
            ).json()
            is_success = res.get("success", False)
        except Exception:
            is_success = True

        sessions.pop(call_id, None)
        if is_success:
            return yemot_msg("העדכון נקלט בהצלחה תודה ולהתראות", go_to=restart_folder)
        else:
            return yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת תודה ולהתראות", go_to=restart_folder)

    return yemot_msg("תודה ולהתראות")

@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_endpoint(request: Request):
    return await handle_call(request)

@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def root_endpoint(request: Request):
    if request.query_params.get("ApiCallId") or request.query_params.get("ApiPhone"):
        return await handle_call(request)
    return JSONResponse({"status": "online", "message": "שרת גמח משניות פעיל"})
