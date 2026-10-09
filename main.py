import os
import io
import traceback
import requests
import smtplib
from email.message import EmailMessage
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

def clean_tts(text: str) -> str:
    if not text:
        return ""
    return str(text).replace(".", " - ").replace('"', '').replace("'", "")

def yemot_read(text: str, var_name: str, max_d=10, min_d=1, timeout=7):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,{max_d},{min_d},{timeout},No"

def yemot_record_no_menu(text: str, var_name: str):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,record,,,no"

def yemot_msg(text: str, go_to="hangup"):
    cleaned = clean_tts(text)
    return f"id_list_message=t-{cleaned}&go_to_folder={go_to}"

def get_restart_folder(p: dict) -> str:
    """זיהוי השלוחה הנוכחית לחזרה חלקה לתפריט הראשי ללא ניתוק"""
    ext = p.get("ApiExtension", "").strip("/")
    return f"/{ext}" if ext else "/"


# =========================================================================
# פונקציית תמלול אודיו דרך SpeechRecognition
# =========================================================================
def transcribe_yemot_audio(file_path: str):
    print(f"\n🎙️ --- [התחלת תמלול הקלטה] ---")
    print(f"📁 נתיב קובץ: {file_path}")

    if not YEMOT_TOKEN or not file_path:
        print("❌ חסר טוקן או נתיב ריק.")
        return None

    clean_p = file_path.strip()
    while clean_p.startswith("ivr2:") or clean_p.startswith("/"):
        if clean_p.startswith("ivr2:"): clean_p = clean_p[5:]
        if clean_p.startswith("/"): clean_p = clean_p[1:]
    
    yemot_path = f"ivr2:/{clean_p}"
    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={yemot_path}"

    try:
        resp = requests.get(download_url, timeout=12)
        if resp.status_code != 200 or len(resp.content) < 300 or resp.content.startswith(b"{\""):
            print("❌ שגיאה בהורדת הקובץ משרת ימות המשיח.")
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
            print(f"✅ [הצלחה בתמלול!] הטקסט: '{text}'")
            return text.strip()

    except sr.UnknownValueError:
        print("⚠️ [גוגל לא זיהה מילים ברורות]")
        return None
    except Exception as e:
        print(f"❌ שגיאה בתמלול: {e}")
        return None
    finally:
        print(f"🏁 --- [סיום תהליך תמלול] ---\n")


# =========================================================================
# ניהול השיחה (מעודכן לפי דפי האפיון 1, 2 ו-3)
# =========================================================================
async def handle_call(request: Request):
    p = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            p.update(dict(form))
        except Exception:
            pass

    call_id = p.get("ApiCallId", p.get("ApiPhone", "default_call"))
    print(f"\n📞 [שיחה] CallId: {call_id} | פרמטרים: {p}")

    if p.get("hangup") == "yes":
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    if call_id not in sessions:
        sessions[call_id] = {"caller_phone": p.get("ApiPhone", "")}
    sess = sessions[call_id]

    restart_folder = get_restart_folder(p)

    # -------------------------------------------------------------
    # 1. תפריט פתיחה (דף 1: פרטי משתמש)
    # -------------------------------------------------------------
    if "user_menu" not in p and "user_menu" not in sess:
        msg = "ברוך הבא לגמח משניות רייזמן להשאלה. פרטי משתמש: משתמש רשום בנדרים פלוס, הקש 1. משתמש מזדמן, הקש 2. הודעה לגמח, הקש 3"
        return send_yemot_response(yemot_read(msg, "user_menu", 1, 1))

    if "user_menu" in p and "user_menu" not in sess:
        sess["user_menu"] = p.get("user_menu")

    menu_choice = sess.get("user_menu")

    # --- מקש 3: הודעה לגמ"ח (דף 1) ---
    if menu_choice == "3":
        if "message_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("הקלט את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", "message_audio"))

        if "msg_confirm" not in p:
            return send_yemot_response(yemot_read("לאישור הקש 1, להקלטה מחודשת הקש 2", "msg_confirm", 1, 1))

        if p.get("msg_confirm") == "1":
            audio_path = p.get("message_audio", "")
            download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:{audio_path}"
            send_admin_email(sess.get("caller_phone", ""), download_url)
            
            sessions.pop(call_id, None)
            return send_yemot_response(yemot_msg("הודעתך נשמרה ונשלחה בהצלחה, תודה ולהתראות", go_to=restart_folder))
        else:
            # הקלטה מחודשת
            return send_yemot_response(yemot_record_no_menu("הקלט את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", "message_audio"))

    # --- מקש 1: משתמש רשום בנדרים פלוס (דף 1) ---
    if menu_choice == "1":
        if "id_num" not in p:
            return send_yemot_response(yemot_read("הקש את מספר הזהות ולאחריו הקש סולמית", "id_num", 9, 8, 10))

        sess["id_num"] = p.get("id_num")

        if "nedarim_pass" not in p:
            return send_yemot_response(yemot_read("הקש את סיסמתך בנדרים פלוס ולאחריו הקש סולמית", "nedarim_pass", 8, 4, 10))

        sess["nedarim_pass"] = p.get("nedarim_pass")

        if "user_verified" not in sess:
            try:
                res = requests.get(
                    f"{SCRIPT_URL}?action=verify_nedarim&id_number={sess['id_num']}&password={sess['nedarim_pass']}",
                    timeout=6
                ).json()
            except Exception:
                res = {"success": False}

            if res.get("success"):
                sess["user_verified"] = True
                sess["name"] = res.get("name", "משתמש נדרים")
                sess["phone"] = res.get("phone", sess.get("caller_phone", ""))
                sess["user_type"] = "נדרים פלוס"
            else:
                sess.pop("id_num", None)
                sess.pop("nedarim_pass", None)
                return send_yemot_response(yemot_read("הפרטים אינם תואמים, אנא הקש שוב את מספר הזהות ולאחריו סולמית", "id_num", 9, 8, 10))

    # --- מקש 2: משתמש מזדמן (דף 1 ודף 2) ---
    if menu_choice == "2":
        sess["user_type"] = "מזדמן"

        # שלב א: שם פרטי ומשפחה
        if "name_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        curr_name_audio = p.get("name_audio")
        if curr_name_audio and sess.get("last_processed_name_audio") != curr_name_audio:
            sess["last_processed_name_audio"] = curr_name_audio
            transcribed_name = transcribe_yemot_audio(curr_name_audio)

            if not transcribed_name:
                sess.pop("name_text", None)
                return send_yemot_response(yemot_record_no_menu("הדיבור אינו ברור, אנא אמור שוב בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))
            
            sess["name_text"] = transcribed_name

        if "name_confirm" not in p:
            msg = f"השם שנקלט הוא {sess.get('name_text', '')}, לאישור הקש 1, להקלטה מחודשת הקש 2"
            return send_yemot_response(yemot_read(msg, "name_confirm", 1, 1))

        if p.get("name_confirm") == "2":
            sess.pop("name_text", None)
            sess.pop("last_processed_name_audio", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        sess["name"] = sess.get("name_text", "מזדמן")

        # שלב ב: מספר טלפון (דף 2)
        if "casual_phone" not in p:
            return send_yemot_response(yemot_read("הקש מספר פלאפון ולאחר מכן הקש סולמית", "casual_phone", 10, 9, 10))

        sess["phone"] = p.get("casual_phone")

        # שלב ג: כתובת מלאה (דף 2)
        if "address_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        curr_addr_audio = p.get("address_audio")
        if curr_addr_audio and sess.get("last_processed_addr_audio") != curr_addr_audio:
            sess["last_processed_addr_audio"] = curr_addr_audio
            transcribed_addr = transcribe_yemot_audio(curr_addr_audio)

            if not transcribed_addr:
                sess.pop("address_text", None)
                return send_yemot_response(yemot_record_no_menu("הדיבור אינו ברור, אנא אמור שוב בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

            sess["address_text"] = transcribed_addr

        if "addr_confirm" not in p:
            msg = f"הכתובת שנקלטה היא {sess.get('address_text', '')}, לאישור הקש 1, להקלטה מחודשת הקש 2"
            return send_yemot_response(yemot_read(msg, "addr_confirm", 1, 1))

        if p.get("addr_confirm") == "2":
            sess.pop("address_text", None)
            sess.pop("last_processed_addr_audio", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        sess["address"] = sess.get("address_text", "-")

        # שמירה ראשונית של המשתמש בשיטס
        if "casual_saved" not in sess:
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=register_casual&name={sess['name']}&phone={sess['phone']}&address={sess['address']}&source=טלפוני",
                    timeout=6
                )
            except Exception:
                pass
            sess["casual_saved"] = True

    # -------------------------------------------------------------
    # 2. בחירת משנה (דף 2)
    # -------------------------------------------------------------
    if "book_choice" not in p and "book_choice" not in sess:
        msg = "לבחירת משנה דרך המספר הסידורי הקש 1, לפרטים מלאים הקש 2, להסבר על המספר הסידורי הקש 9"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1))

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")

    b_choice = sess.get("book_choice")

    # מקש 9: הסבר על המספר הסידורי (דף 2)
    if b_choice == "9":
        sess.pop("book_choice", None)
        msg = "הסבר על המספר הסידורי: המספר הסידורי מוטבע על גב כרך המשניות, ובהקשת מספר זה ניתן לבצע השאלה או החזרה. לבחירת משנה דרך המספר הסידורי הקש 1, לפרטים מלאים הקש 2"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1))

    # מקש 1: דרך המספר הסידורי (דף 2)
    if b_choice == "1":
        if "serial_num" not in p:
            return send_yemot_response(yemot_read("הקש את המספר הסידורי ולאחריו הקש סולמית", "serial_num", 6, 1, 10))
        sess["book_id"] = p.get("serial_num")
        sess["seder_mishna"] = "-"

    # מקש 2: פרטים מלאים - סדר ומסכת (דף 2)
    elif b_choice == "2":
        if "seder_num" not in p and "seder_name" not in sess:
            msg = "הקש את מספר הסדר: 1 זרעים, 2 מועד, 3 נשים, 4 נזיקין, 5 קדשים, 6 טהרות, ובסיום סולמית"
            return send_yemot_response(yemot_read(msg, "seder_num", 1, 1))

        if "seder_num" in p:
            sess["seder_name"] = SEDARIM.get(p.get("seder_num"), "כללי")

        if "mishna_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        curr_mishna_audio = p.get("mishna_audio")
        if curr_mishna_audio and sess.get("last_processed_mishna_audio") != curr_mishna_audio:
            sess["last_processed_mishna_audio"] = curr_mishna_audio
            transcribed_mishna = transcribe_yemot_audio(curr_mishna_audio)

            if not transcribed_mishna:
                sess.pop("mishna_text", None)
                return send_yemot_response(yemot_record_no_menu("הדיבור אינו ברור, אנא אמור שוב בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

            sess["mishna_text"] = transcribed_mishna

        if "mishna_confirm" not in p:
            msg = f"שם המשנה שנקלט הוא {sess.get('mishna_text', '')}, לאישור הקש 1, להקלטה מחודשת הקש 2"
            return send_yemot_response(yemot_read(msg, "mishna_confirm", 1, 1))

        if p.get("mishna_confirm") == "2":
            sess.pop("mishna_text", None)
            sess.pop("last_processed_mishna_audio", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        sess["book_id"] = "-"
        sess["seder_mishna"] = f"{sess.get('seder_name', '')} - {sess.get('mishna_text', '')}"

    # -------------------------------------------------------------
    # 3. פעולה: השאלה או החזרה (דף 3)
    # -------------------------------------------------------------
    if "action_choice" not in p:
        return send_yemot_response(yemot_read("להשאלת המשנה הקש 1, להחזרת המשנה הקש 2", "action_choice", 1, 1))

    act = p.get("action_choice")
    if act == "1":
        # ביצוע השאלה
        try:
            requests.get(
                f"{SCRIPT_URL}?action=borrow&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}"
                f"&name={sess.get('name', 'משאיל')}&phone={sess.get('phone', '')}&address={sess.get('address', '-')}"
                f"&userType={sess.get('user_type', 'מזדמן')}&source=טלפוני",
                timeout=6
            )
        except Exception:
            pass

        sessions.pop(call_id, None)
        # דף 3: "העדכון נקלט בהצלחה, תודה ולהתראות"
        return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות", go_to=restart_folder))

    elif act == "2":
        # ביצוע החזרה
        try:
            res = requests.get(
                f"{SCRIPT_URL}?action=return&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}&phone={sess.get('phone', '')}&source=טלפוני",
                timeout=6
            ).json()
        except Exception:
            res = {"success": True}

        sessions.pop(call_id, None)
        if res.get("success"):
            return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות", go_to=restart_folder))
        else:
            return send_yemot_response(yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת, תודה ולהתראות", go_to=restart_folder))

    return send_yemot_response(yemot_msg("תודה ולהתראות"))


def send_yemot_response(text: str):
    print(f"📤 [תשובה לימות המשיח]: {text}")
    return PlainTextResponse(text)

def send_admin_email(phone, file_url):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    admin_mail = os.environ.get("ADMIN_EMAIL", smtp_user)
    if not smtp_user or not smtp_pass: return
    try:
        msg = EmailMessage()
        msg["Subject"] = "הודעה חדשה מתא קולי - גמ\"ח משניות"
        msg["From"] = smtp_user
        msg["To"] = admin_mail
        msg.set_content(f"שלום,\nהתקבלה הודעה קולית חדשה מאת טלפון: {phone}\nקישור להאזנה להקלטה: {file_url}")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(smtp_user, smtp_pass)
            smtp.send_message(msg)
    except Exception:
        pass

@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_endpoint(request: Request):
    return await handle_call(request)

@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def root_endpoint(request: Request):
    if request.query_params.get("ApiCallId") or request.query_params.get("ApiPhone"):
        return await handle_call(request)
    return JSONResponse({"status": "online", "message": "שרת גמח משניות רייזמן פעיל"})
