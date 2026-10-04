import os
import io
import requests
import smtplib
from email.message import EmailMessage
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
import speech_recognition as sr
from pydub import AudioSegment

app = FastAPI(title="Gemach Reisman IVR Server")

# קישור ל-Google Apps Script
SCRIPT_URL = os.environ.get(
    "SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbyMS9azkxb4nCw99g4Il5n8vIfEKJTUedb1A80vI51PqpQh2BnCPmQ9X0e_2eS3SZQ3hw/exec"
)

# טוקן ימות המשיח (מספר מערכת:סיסמה) לצורך הורדת הקלטות (אופציונלי אך מומלץ)
# לדוגמה: "0773137770:123456"
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "")

sessions = {}

SEDARIM = {
    "1": "זרעים", "2": "מועד", "3": "נשים",
    "4": "נזיקין", "5": "קדשים", "6": "טהרות"
}

def clean_tts(text: str) -> str:
    """ניקוי תווים אסורים בימות המשיח: נקודות, גרשיים וגרשים"""
    if not text:
        return ""
    return str(text).replace(".", " - ").replace('"', '').replace("'", "")

def yemot_read(text: str, var_name: str, max_d=10, min_d=1, timeout=7):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,{max_d},{min_d},{timeout},No"

def yemot_record(text: str, var_name: str):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,record"

def yemot_msg(text: str, go_to="hangup"):
    cleaned = clean_tts(text)
    return f"id_list_message=t-{cleaned}&go_to_folder={go_to}"


# =========================================================================
# פונקציית תמלול אודיו באמצעות Google SpeechRecognition
# =========================================================================
def transcribe_yemot_audio(file_path_or_url: str) -> str:
    """הורדת האודיו מימות המשיח ותמלול לעברית דרך SpeechRecognition"""
    if not file_path_or_url:
        return "לא זוהה"

    try:
        # בדיקה האם מדובר בקישור ישיר או בנתיב פנימי של ימות המשיח
        if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
            download_url = file_path_or_url
        else:
            # נתיב של ימות המשיח - הורדה דרך ה-API של ימות
            download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={file_path_or_url}"

        resp = requests.get(download_url, timeout=10)
        if resp.status_code != 200:
            return "הקלטה ללא תמלול"

        # המרת קובץ האודיו ל-WAV תואם (PCM 16-bit) בעזרת pydub
        audio_stream = io.BytesIO(resp.content)
        sound = AudioSegment.from_file(audio_stream)
        sound = sound.set_frame_rate(16000).set_channels(1)
        
        wav_io = io.BytesIO()
        sound.export(wav_io, format="wav")
        wav_io.seek(0)

        # תמלול ע"י ספריית SpeechRecognition של גוגל
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="he-IL")
            return text.strip()

    except sr.UnknownValueError:
        return "לא הצלחתי לפענח את הדיבור"
    except Exception as e:
        print(f"Transcription error: {e}")
        return "הקלטה קולית"


# =========================================================================
# ניהול שיחות טלפון (IVR Gateway)
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

    if p.get("hangup") == "yes":
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    if call_id not in sessions:
        sessions[call_id] = {"caller_phone": p.get("ApiPhone", "")}
    sess = sessions[call_id]

    # -------------------------------------------------------------
    # 1. תפריט פתיחה ראשי
    # -------------------------------------------------------------
    if "user_menu" not in p and "user_menu" not in sess:
        msg = "ברוך הבא לגמח משניות רייזמן להשאלה, משתמש רשום בנדרים פלוס הקש 1, משתמש מזדמן הקש 2, להשארת הודעה לגמח הקש 3"
        return PlainTextResponse(yemot_read(msg, "user_menu", 1, 1))

    if "user_menu" in p and "user_menu" not in sess:
        sess["user_menu"] = p.get("user_menu")

    menu_choice = sess.get("user_menu")

    # -------------------------------------------------------------
    # מקש 3: השארת הודעה לגמ"ח
    # -------------------------------------------------------------
    if menu_choice == "3":
        if "message_audio" not in p:
            return PlainTextResponse(yemot_record("הקלט את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

        if "msg_confirm" not in p:
            return PlainTextResponse(yemot_read("לאישור הקישו 1, לתיקון הקישו 2", "msg_confirm", 1, 1))

        if p.get("msg_confirm") == "1":
            audio_url = p.get("message_audio", "")
            send_admin_email(sess.get("caller_phone", ""), audio_url)
            return PlainTextResponse(yemot_msg("הודעתך נשמרה בהצלחה, תודה ולהתראות"))
        else:
            return PlainTextResponse(yemot_record("הקלט שוב את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

    # -------------------------------------------------------------
    # מקש 1: נדרים פלוס
    # -------------------------------------------------------------
    if menu_choice == "1":
        if "id_num" not in p:
            return PlainTextResponse(yemot_read("הקש את מספר הזהות ולאחריו הקש סולמית", "id_num", 9, 8, 10))

        sess["id_num"] = p.get("id_num")

        if "nedarim_pass" not in p:
            return PlainTextResponse(yemot_read("הקש את סיסמתך בנדרים פלוס ולאחריו הקש סולמית", "nedarim_pass", 8, 4, 10))

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
                return PlainTextResponse(yemot_read("הפרטים אינם תואמים, הקש שוב את מספר הזהות ובסיום סולמית", "id_num", 9, 8, 10))

    # -------------------------------------------------------------
    # מקש 2: משתמש מזדמן - הקלטה, תמלול, אישור והזנה לשיטס
    # -------------------------------------------------------------
    if menu_choice == "2":
        sess["user_type"] = "מזדמן"

        # שלב א: הקלטת שם
        if "name_audio" not in p:
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        # תמלול השם והשמעה לאישור
        if "name_text" not in sess:
            sess["name_text"] = transcribe_yemot_audio(p.get("name_audio"))

        if "name_confirm" not in p:
            msg = f"השם שנקלט הוא {sess['name_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return PlainTextResponse(yemot_read(msg, "name_confirm", 1, 1))

        if p.get("name_confirm") == "2":
            sess.pop("name_text", None)
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        sess["name"] = sess["name_text"]

        # שלב ב: מספר טלפון
        if "casual_phone" not in p:
            return PlainTextResponse(yemot_read("הקש מספר פלאפון ולאחר מכן הקש סולמית", "casual_phone", 10, 9, 10))

        sess["phone"] = p.get("casual_phone")

        # שלב ג: הקלטת כתובת
        if "address_audio" not in p:
            return PlainTextResponse(yemot_record("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        # תמלול הכתובת והשמעה לאישור
        if "address_text" not in sess:
            sess["address_text"] = transcribe_yemot_audio(p.get("address_audio"))

        if "addr_confirm" not in p:
            msg = f"הכתובת שנקלטה היא {sess['address_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return PlainTextResponse(yemot_read(msg, "addr_confirm", 1, 1))

        if p.get("addr_confirm") == "2":
            sess.pop("address_text", None)
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        sess["address"] = sess["address_text"]

        # שלב ד: הזנה לשיטס בסיום הרישום
        if "casual_saved" not in sess:
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=register_casual&name={sess['name']}&phone={sess['phone']}&address={sess['address']}",
                    timeout=6
                )
            except Exception:
                pass
            sess["casual_saved"] = True

    # -------------------------------------------------------------
    # 2. בחירת משנה
    # -------------------------------------------------------------
    if "book_choice" not in p and "book_choice" not in sess:
        msg = "לבחירת משנה דרך המספר הסידורי הקש 1, לפרטים מלאים הקש 2, להסבר על המספר הסידורי הקש 9"
        return PlainTextResponse(yemot_read(msg, "book_choice", 1, 1))

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")

    b_choice = sess.get("book_choice")

    if b_choice == "9":
        sess.pop("book_choice", None)
        msg = "הסבר שימוש, המספר הסידורי מוטבע על גב כרך המשניות, בהקשת מספר זה ניתן לבצע השאלה או החזרה, לבחירה דרך מספר סידורי הקש 1, לפרטים מלאים הקש 2"
        return PlainTextResponse(yemot_read(msg, "book_choice", 1, 1))

    # בחירה לפי מספר סידורי (מקש 1)
    if b_choice == "1":
        if "serial_num" not in p:
            return PlainTextResponse(yemot_read("הקש את המספר הסידורי ולאחריו הקש סולמית", "serial_num", 6, 1, 10))
        sess["book_id"] = p.get("serial_num")
        sess["seder_mishna"] = "-"

    # בחירה לפי סדר ומסכת (מקש 2)
    elif b_choice == "2":
        if "seder_num" not in p and "seder_name" not in sess:
            msg = "הקש את מספר הסדר, 1 זרעים, 2 מועד, 3 נשים, 4 נזיקין, 5 קדשים, 6 טהרות, ובסיום סולמית"
            return PlainTextResponse(yemot_read(msg, "seder_num", 1, 1))

        if "seder_num" in p:
            sess["seder_name"] = SEDARIM.get(p.get("seder_num"), "כללי")

        if "mishna_audio" not in p:
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        if "mishna_text" not in sess:
            sess["mishna_text"] = transcribe_yemot_audio(p.get("mishna_audio"))

        if "mishna_confirm" not in p:
            msg = f"שם המשנה שנקלט הוא {sess['mishna_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return PlainTextResponse(yemot_read(msg, "mishna_confirm", 1, 1))

        if p.get("mishna_confirm") == "2":
            sess.pop("mishna_text", None)
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        sess["book_id"] = "-"
        sess["seder_mishna"] = f"{sess.get('seder_name', '')} - {sess['mishna_text']}"

    # -------------------------------------------------------------
    # 3. פעולה: השאלה (1) או החזרה (2)
    # -------------------------------------------------------------
    if "action_choice" not in p:
        return PlainTextResponse(yemot_read("להשאלת המשנה הקש 1, להחזרת המשנה הקש 2", "action_choice", 1, 1))

    act = p.get("action_choice")
    if act == "1":
        # ביצוע השאלה
        try:
            requests.get(
                f"{SCRIPT_URL}?action=borrow&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}"
                f"&name={sess.get('name', 'משאיל')}&phone={sess.get('phone', '')}&address={sess.get('address', '-')}&userType={sess.get('user_type', 'מזדמן')}",
                timeout=6
            )
        except Exception:
            pass
        return PlainTextResponse(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות"))

    elif act == "2":
        # ביצוע החזרה
        try:
            res = requests.get(
                f"{SCRIPT_URL}?action=return&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}&phone={sess.get('phone', '')}",
                timeout=6
            ).json()
        except Exception:
            res = {"success": True}

        if res.get("success"):
            return PlainTextResponse(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות"))
        else:
            return PlainTextResponse(yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת, תודה ולהתראות"))

    return PlainTextResponse(yemot_msg("תודה ולהתראות"))


def send_admin_email(phone, file_url):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    admin_mail = os.environ.get("ADMIN_EMAIL", smtp_user)
    if not smtp_user or not smtp_pass: return
    try:
        msg = EmailMessage()
        msg["Subject"] = "פנייה ממערכת ההשאלות"
        msg["From"] = smtp_user
        msg["To"] = admin_mail
        msg.set_content(f"שלום,\nהתקבלה הודעה חדשה מאת טלפון: {phone}\nקישור להקלטה: {file_url}")
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
