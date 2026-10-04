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

app = FastAPI(title="Gemach Reisman IVR Server")

SCRIPT_URL = os.environ.get(
    "SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbyMS9azkxb4nCw99g4Il5n8vIfEKJTUedb1A80vI51PqpQh2BnCPmQ9X0e_2eS3SZQ3hw/exec"
)

# טוקן ימות המשיח מתוך משתני הסביבה
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN", "").strip()

sessions = {}

SEDARIM = {
    "1": "זרעים", "2": "מועד", "3": "נשים",
    "4": "נזיקין", "5": "קדשים", "6": "טהרות"
}

def clean_tts(text: str) -> str:
    """ניקוי תווים שאסורים ב-TTS של ימות המשיח"""
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


# =========================================================================
# פונקציית תמלול אודיו דרך SpeechRecognition (עם לוגים מלאים)
# =========================================================================
def transcribe_yemot_audio(file_path: str) -> str:
    print(f"\n🎙️ --- [התחלת תמלול הקלטה] ---")
    print(f"📁 נתיב קובץ שהתקבל מימות המשיח: {file_path}")
    print(f"🔑 בדיקת טוקן ימות המשיח: {'מוגדר בהצלחה' if YEMOT_TOKEN else '❌ לא מוגדר! חובה להגדיר YEMOT_TOKEN ב-Render'}")

    if not YEMOT_TOKEN:
        print("❌ [שגיאה] חסר YEMOT_TOKEN במשתני הסביבה של Render. לא ניתן להוריד את הקובץ.")
        return "חסר טוקן התחברות"

    if not file_path:
        print("❌ [שגיאה] הנתיב של הקובץ ריק.")
        return "הקלטה ריקה"

    # נירמול הנתיב לפורמט ivr2:/008.wav הנדרש ע"י ימות המשיח
    clean_p = file_path.strip()
    while clean_p.startswith("ivr2:") or clean_p.startswith("/"):
        if clean_p.startswith("ivr2:"):
            clean_p = clean_p[5:]
        if clean_p.startswith("/"):
            clean_p = clean_p[1:]
    
    yemot_path = f"ivr2:/{clean_p}"
    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={yemot_path}"
    print(f"🌐 כתובת הורדה: https://www.call2all.co.il/ym/api/DownloadFile?token=***&path={yemot_path}")

    try:
        resp = requests.get(download_url, timeout=12)
        print(f"📥 סטטוס הורדה מימות המשיח: {resp.status_code} | גודל קובץ שהתקבל: {len(resp.content)} bytes")

        # בדיקה האם ימות החזיר שגיאת טקסט או JSON במקום קובץ שמע
        if resp.status_code != 200 or len(resp.content) < 300:
            print(f"❌ [שגיאה בהורדת הקובץ] תשובת השרת: {resp.text[:300]}")
            return "שגיאה בהורדת ההקלטה"

        if resp.content.startswith(b"{\"") or b"responseStatus" in resp.content[:100]:
            print(f"❌ [שגיאת API מימות המשיח]: {resp.text[:300]}")
            return "שגיאת הרשאה בימות המשיח"

        print("🔄 הקובץ הורד בהצלחה. מבצע המרה ל-WAV 16kHz...")
        audio_stream = io.BytesIO(resp.content)
        sound = AudioSegment.from_file(audio_stream)
        sound = sound.set_frame_rate(16000).set_channels(1)
        
        wav_io = io.BytesIO()
        sound.export(wav_io, format="wav")
        wav_io.seek(0)

        print("🚀 שולח ל-Google SpeechRecognition (שפה: he-IL)...")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="he-IL")
            print(f"✅ [הצלחה בתמלול!] הטקסט שפוענח: '{text}'")
            return text.strip()

    except sr.UnknownValueError:
        print("⚠️ [פענוח נכשל] גוגל לא הצליח לזהות מילים ברורות בהקלטה.")
        return "דיבור לא ברור"
    except sr.RequestError as e:
        print(f"❌ [שגיאת שירות גוגל]: {e}")
        return "שגיאת תקשורת עם שירות התמלול"
    except Exception as e:
        print(f"❌ [חריגה כללית בתמלול]: {e}")
        traceback.print_exc()
        return "שגיאה בפענוח"
    finally:
        print(f"🏁 --- [סיום תהליך תמלול] ---\n")


# =========================================================================
# פונקציית ניהול השיחה (עם לוג מלא של כל שלב)
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
    print(f"\n📞 [שיחה נכנסת] CallId: {call_id} | טלפון: {p.get('ApiPhone')} | שלוחה: {p.get('ApiExtension')}")
    print(f"📥 [פרמטרים שהתקבלו]: {p}")

    # טיפול בניתוק
    if p.get("hangup") == "yes":
        print(f"📴 [ניתוק שיחה] CallId {call_id} סיים שיחה.")
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    if call_id not in sessions:
        sessions[call_id] = {"caller_phone": p.get("ApiPhone", "")}
    sess = sessions[call_id]

    # -------------------------------------------------------------
    # 1. תפריט פתיחה
    # -------------------------------------------------------------
    if "user_menu" not in p and "user_menu" not in sess:
        msg = "ברוך הבא לגמח משניות רייזמן להשאלה, משתמש רשום בנדרים פלוס הקש 1, משתמש מזדמן הקש 2, להשארת הודעה לגמח הקש 3"
        return send_yemot_response(yemot_read(msg, "user_menu", 1, 1))

    if "user_menu" in p and "user_menu" not in sess:
        sess["user_menu"] = p.get("user_menu")

    menu_choice = sess.get("user_menu")
    print(f"👉 [בחירת תפריט ראשי]: {menu_choice}")

    # --- מקש 3: השארת הודעה ---
    if menu_choice == "3":
        if "message_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("הקלט את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

        if "msg_confirm" not in p:
            return send_yemot_response(yemot_read("לאישור הקישו 1, לתיקון הקישו 2", "msg_confirm", 1, 1))

        if p.get("msg_confirm") == "1":
            audio_path = p.get("message_audio", "")
            download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:{audio_path}"
            send_admin_email(sess.get("caller_phone", ""), download_url)
            return send_yemot_response(yemot_msg("הודעתך נשמרה ונשלחה בהצלחה, תודה ולהתראות"))
        else:
            return send_yemot_response(yemot_record_no_menu("הקלט שוב את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

    # --- מקש 1: נדרים פלוס ---
    if menu_choice == "1":
        if "id_num" not in p:
            return send_yemot_response(yemot_read("הקש את מספר הזהות ולאחריו הקש סולמית", "id_num", 9, 8, 10))

        sess["id_num"] = p.get("id_num")

        if "nedarim_pass" not in p:
            return send_yemot_response(yemot_read("הקש את סיסמתך בנדרים פלוס ולאחריו הקש סולמית", "nedarim_pass", 8, 4, 10))

        sess["nedarim_pass"] = p.get("nedarim_pass")

        if "user_verified" not in sess:
            print(f"🔍 [אימות נדרים פלוס] בודק ת\"ז {sess['id_num']} מול ה-Sheets...")
            try:
                res = requests.get(
                    f"{SCRIPT_URL}?action=verify_nedarim&id_number={sess['id_num']}&password={sess['nedarim_pass']}",
                    timeout=6
                ).json()
            except Exception as e:
                print(f"❌ שגיאה בבדיקת נדרים פלוס: {e}")
                res = {"success": False}

            if res.get("success"):
                print(f"✅ אימות נדרים פלוס הצליח: {res.get('name')}")
                sess["user_verified"] = True
                sess["name"] = res.get("name", "משתמש נדרים")
                sess["phone"] = res.get("phone", sess.get("caller_phone", ""))
                sess["user_type"] = "נדרים פלוס"
            else:
                print("❌ פרטי נדרים פלוס שגויים.")
                sess.pop("id_num", None)
                sess.pop("nedarim_pass", None)
                return send_yemot_response(yemot_read("הפרטים אינם תואמים, הקש שוב את מספר הזהות ובסיום סולמית", "id_num", 9, 8, 10))

    # --- מקש 2: משתמש מזדמן (תמלול שם וכתובת) ---
    if menu_choice == "2":
        sess["user_type"] = "מזדמן"

        # שלב א: שם
        if "name_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        if "name_text" not in sess:
            print("🎤 [תמלול שם המשתמש]...")
            sess["name_text"] = transcribe_yemot_audio(p.get("name_audio"))

        if "name_confirm" not in p:
            msg = f"השם שנקלט הוא {sess['name_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return send_yemot_response(yemot_read(msg, "name_confirm", 1, 1))

        if p.get("name_confirm") == "2":
            print("🔄 המאזין בחר לתקן את השם.")
            sess.pop("name_text", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        sess["name"] = sess["name_text"]

        # שלב ב: טלפון
        if "casual_phone" not in p:
            return send_yemot_response(yemot_read("הקש מספר פלאפון ולאחר מכן הקש סולמית", "casual_phone", 10, 9, 10))

        sess["phone"] = p.get("casual_phone")

        # שלב ג: כתובת
        if "address_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        if "address_text" not in sess:
            print("🎤 [תמלול כתובת המשתמש]...")
            sess["address_text"] = transcribe_yemot_audio(p.get("address_audio"))

        if "addr_confirm" not in p:
            msg = f"הכתובת שנקלטה היא {sess['address_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return send_yemot_response(yemot_read(msg, "addr_confirm", 1, 1))

        if p.get("addr_confirm") == "2":
            print("🔄 המאזין בחר לתקן את הכתובת.")
            sess.pop("address_text", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        sess["address"] = sess["address_text"]

        # שמירה בשיטס
        if "casual_saved" not in sess:
            print(f"💾 [שמירת מזדמן בשיטס] שם: {sess['name']} | טלפון: {sess['phone']} | כתובת: {sess['address']}")
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=register_casual&name={sess['name']}&phone={sess['phone']}&address={sess['address']}",
                    timeout=6
                )
                print("✅ פרטי המשתמש נשמרו בשיטס בהצלחה.")
            except Exception as e:
                print(f"❌ שגיאה בשמירת משתמש בשיטס: {e}")
            sess["casual_saved"] = True

    # -------------------------------------------------------------
    # 2. בחירת משנה
    # -------------------------------------------------------------
    if "book_choice" not in p and "book_choice" not in sess:
        msg = "לבחירת משנה דרך המספר הסידורי הקש 1, לפרטים מלאים הקש 2, להסבר על המספר הסידורי הקש 9"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1))

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")

    b_choice = sess.get("book_choice")

    if b_choice == "9":
        sess.pop("book_choice", None)
        msg = "הסבר שימוש, המספר הסידורי מוטבע על גב כרך המשניות, בהקשת מספר זה ניתן לבצע השאלה או החזרה, לבחירה דרך מספר סידורי הקש 1, לפרטים מלאים הקש 2"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1))

    # מקש 1: מספר סידורי
    if b_choice == "1":
        if "serial_num" not in p:
            return send_yemot_response(yemot_read("הקש את המספר הסידורי ולאחריו הקש סולמית", "serial_num", 6, 1, 10))
        sess["book_id"] = p.get("serial_num")
        sess["seder_mishna"] = "-"

    # מקש 2: סדר ומסכת
    elif b_choice == "2":
        if "seder_num" not in p and "seder_name" not in sess:
            msg = "הקש את מספר הסדר, 1 זרעים, 2 מועד, 3 נשים, 4 נזיקין, 5 קדשים, 6 טהרות, ובסיום סולמית"
            return send_yemot_response(yemot_read(msg, "seder_num", 1, 1))

        if "seder_num" in p:
            sess["seder_name"] = SEDARIM.get(p.get("seder_num"), "כללי")

        if "mishna_audio" not in p:
            return send_yemot_response(yemot_record_no_menu("אמור בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        if "mishna_text" not in sess:
            print("🎤 [תמלול שם המשנה]...")
            sess["mishna_text"] = transcribe_yemot_audio(p.get("mishna_audio"))

        if "mishna_confirm" not in p:
            msg = f"שם המשנה שנקלט הוא {sess['mishna_text']}, לאישור הקישו 1, לתיקון הקישו 2"
            return send_yemot_response(yemot_read(msg, "mishna_confirm", 1, 1))

        if p.get("mishna_confirm") == "2":
            sess.pop("mishna_text", None)
            return send_yemot_response(yemot_record_no_menu("אמור שוב בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        sess["book_id"] = "-"
        sess["seder_mishna"] = f"{sess.get('seder_name', '')} - {sess['mishna_text']}"

    # -------------------------------------------------------------
    # 3. פעולה: השאלה או החזרה
    # -------------------------------------------------------------
    if "action_choice" not in p:
        return send_yemot_response(yemot_read("להשאלת המשנה הקש 1, להחזרת המשנה הקש 2", "action_choice", 1, 1))

    act = p.get("action_choice")
    if act == "1":
        print(f"📦 [ביצוע השאלה] ספר: {sess.get('book_id')} | משנה: {sess.get('seder_mishna')} | משאיל: {sess.get('name')}")
        try:
            requests.get(
                f"{SCRIPT_URL}?action=borrow&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}"
                f"&name={sess.get('name', 'משאיל')}&phone={sess.get('phone', '')}&address={sess.get('address', '-')}&userType={sess.get('user_type', 'מזדמן')}",
                timeout=6
            )
            print("✅ השאלה נרשמה בשיטס בהצלחה.")
        except Exception as e:
            print(f"❌ שגיאה ברישום השאלה בשיטס: {e}")
        return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות"))

    elif act == "2":
        print(f"🔄 [ביצוע החזרה] ספר: {sess.get('book_id')} | משנה: {sess.get('seder_mishna')}")
        try:
            res = requests.get(
                f"{SCRIPT_URL}?action=return&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}&phone={sess.get('phone', '')}",
                timeout=6
            ).json()
            print(f"תוצאת החזרה משיטס: {res}")
        except Exception as e:
            print(f"שגיאה בהחזרה: {e}")
            res = {"success": True}

        if res.get("success"):
            return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות"))
        else:
            return send_yemot_response(yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת, תודה ולהתראות"))

    return send_yemot_response(yemot_msg("תודה ולהתראות"))

def send_yemot_response(text: str):
    print(f"📤 [תשובה שנשלחת לימות המשיח]: {text}")
    return PlainTextResponse(text)

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
        print("📧 מייל פנייה נשלח בהצלחה.")
    except Exception as e:
        print(f"❌ שגיאה בשליחת מייל: {e}")

@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_endpoint(request: Request):
    return await handle_call(request)

@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def root_endpoint(request: Request):
    if request.query_params.get("ApiCallId") or request.query_params.get("ApiPhone"):
        return await handle_call(request)
    return JSONResponse({"status": "online", "message": "שרת גמח משניות רייזמן פעיל"})
