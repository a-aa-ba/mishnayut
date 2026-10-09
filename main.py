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

# =========================================================================
# פונקציות עזר לפרוטוקול ימות המשיח ולוגים
# =========================================================================

def log_ivr(call_id: str, message: str):
    """רישום לוג נקי בעברית בלבד לטרמינל השרת"""
    print(f"📞 [שיחה: {call_id}] {message}")

def clean_tts(text: str) -> str:
    """ניקוי מוחלט של פסיקים ונקודות המפסיקים את הקראת הטקסט בימות המשיח"""
    if not text:
        return ""
    for ch in [".", ",", ":", ";", '"', "'", "&", "=", "/", "\\"]:
        text = text.replace(ch, " ")
    return " ".join(text.split())

def yemot_read(text: str, var_name: str, max_d=10, min_d=1, timeout=7):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,{max_d},{min_d},{timeout},No"

def yemot_menu_read(text: str, var_name: str, timeout=5):
    """פקודת תפריט המונעת השמעת 'לא הוקשה בחירה' ומחכה 5 שניות לשידור חוזר"""
    cleaned = clean_tts(text)
    # ערך 11: השמעה 1 | ערך 12: Ok (לא להשמיע הודעת שגיאה) | ערך 13: None
    return f"read=t-{cleaned}={var_name},no,1,1,{timeout},No,,,,,1,Ok,None"

def yemot_record_no_menu(text: str, var_name: str):
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,record,,,no"

def yemot_msg(text: str, go_to="hangup"):
    cleaned = clean_tts(text)
    return f"id_list_message=t-{cleaned}&go_to_folder={go_to}"

def get_restart_folder(p: dict) -> str:
    ext = p.get("ApiExtension", "").strip("/")
    return f"/{ext}" if ext else "/"

# =========================================================================
# תמלול קבצי קול
# =========================================================================
def transcribe_yemot_audio(file_path: str, call_id: str):
    log_ivr(call_id, f"מתחיל הורדת הקלטה ותמלול: {file_path}")

    if not YEMOT_TOKEN or not file_path:
        log_ivr(call_id, "שגיאה: חסר טוקן או נתיב הקלטה ריק")
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
            log_ivr(call_id, "שגיאה בהורדת קובץ האודיו משרתי ימות המשיח")
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
            log_ivr(call_id, f"זיהוי דיבור הצליח! הטקסט שפוענח: '{text.strip()}'")
            return text.strip()

    except sr.UnknownValueError:
        log_ivr(call_id, "זיהוי הדיבור לא הצליח לזהות מילים ברורות")
        return None
    except Exception as e:
        log_ivr(call_id, f"שגיאה בתהליך התמלול: {e}")
        return None

# =========================================================================
# הניתוב הראשי של השיחה
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
        log_ivr(call_id, "השיחה נותקה על ידי המאזין")
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    if call_id not in sessions:
        sessions[call_id] = {"caller_phone": p.get("ApiPhone", "")}
        log_ivr(call_id, f"שיחה חדשה נכנסה ממספר: {p.get('ApiPhone', 'לא ידוע')}")

    sess = sessions[call_id]
    restart_folder = get_restart_folder(p)

    # -------------------------------------------------------------
    # 1. תפריט פתיחה ראשי
    # -------------------------------------------------------------
    user_menu_input = p.get("user_menu", "").strip()

    if "user_menu" not in sess:
        if not user_menu_input or user_menu_input == "None":
            log_ivr(call_id, "השמעת תפריט ראשי למאזין והמתנה של 5 שניות להקשה")
            msg = "ברוך הבא לגמח משניות רייזמן להשאלה פרטי משתמש משתמש רשום הקש 1 לרישום משתמש חדש הקש 2 להשארת הודעה לגמח הקש 3"
            return send_yemot_response(yemot_menu_read(msg, "user_menu", timeout=5))
        
        sess["user_menu"] = user_menu_input
        log_ivr(call_id, f"המאזין הקיש בתפריט הראשי: מקש {user_menu_input}")

    menu_choice = sess.get("user_menu")

    # --- מקש 3: השארת הודעה לגמ"ח ---
    if menu_choice == "3":
        if "message_audio" not in p:
            log_ivr(call_id, "המאזין מופנה להקלטת הודעה קולית")
            return send_yemot_response(yemot_record_no_menu("הקלט את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", "message_audio"))

        if "msg_confirm" not in p:
            log_ivr(call_id, "ההודעה הוקלטה. מבקש אישור (1) או הקלטה חוזרת (2)")
            return send_yemot_response(yemot_read("לאישור הקש 1 להקלטה מחודשת הקש 2", "msg_confirm", 1, 1, 6))

        if p.get("msg_confirm") == "1":
            log_ivr(call_id, "המאזין אישר את ההודעה. נשלח מייל למנהל הגמ\"ח")
            audio_path = p.get("message_audio", "")
            download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:{audio_path}"
            send_admin_email(sess.get("caller_phone", ""), download_url)
            sessions.pop(call_id, None)
            return send_yemot_response(yemot_msg("הודעתך נשמרה ונשלחה בהצלחה תודה ולהתראות", go_to=restart_folder))
        else:
            log_ivr(call_id, "המאזין בחר להקליט את ההודעה מחדש")
            return send_yemot_response(yemot_record_no_menu("הקלט שוב את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית", "message_audio"))

    # --- מקש 1: משתמש רשום (הקשת ת.ז. בלבד) ---
    if menu_choice == "1":
        if "id_num" not in p:
            log_ivr(call_id, "מבקש מהמאזין להקיש תעודת זהות")
            return send_yemot_response(yemot_read("הקש את מספר הזהות שלך ולאחריו הקש סולמית", "id_num", 9, 8, 10))

        sess["id_num"] = p.get("id_num")

        if "user_verified" not in sess:
            log_ivr(call_id, f"בודק במערכת תעודת זהות: {sess['id_num']}")
            try:
                res = requests.get(
                    f"{SCRIPT_URL}?action=verify_registered&id_number={sess['id_num']}",
                    timeout=6
                ).json()
            except Exception:
                res = {"success": False}

            if res.get("success"):
                sess["user_verified"] = True
                sess["name"] = res.get("name", "משתמש רשום")
                sess["phone"] = res.get("phone", sess.get("caller_phone", ""))
                sess["user_type"] = "רשום"
                log_ivr(call_id, f"המשתמש אומת בהצלחה: {sess['name']} (טלפון: {sess['phone']})")
            else:
                log_ivr(call_id, f"תעודת זהות {sess['id_num']} לא נמצאה במערכת")
                sess.pop("id_num", None)
                return send_yemot_response(yemot_read("מספר הזהות אינו מופיע במערכת אנא הקישו שוב ולאחריו סולמית", "id_num", 9, 8, 10))

    # --- מקש 2: רישום משתמש חדש (4 שלבים) ---
    if menu_choice == "2":
        sess["user_type"] = "חדש"

        # שלב 1: ת.ז
        if "reg_id" not in p and "reg_id" not in sess:
            log_ivr(call_id, "רישום חדש - שלב 1: בקשת הקשת תעודת זהות בת 9 ספרות")
            return send_yemot_response(yemot_read("לרישום הקישו את מספר הזהות שלכם בן 9 ספרות ולאחריו סולמית", "reg_id", 9, 8, 10))

        if "reg_id" in p:
            sess["reg_id"] = p.get("reg_id")
            log_ivr(call_id, f"רישום חדש - נקלטה תעודת זהות: {sess['reg_id']}")

        # שלב 2: טלפון
        if "reg_phone" not in p and "reg_phone" not in sess:
            log_ivr(call_id, "רישום חדש - שלב 2: בקשת הקשת מספר טלפון")
            return send_yemot_response(yemot_read("הקישו את מספר הטלפון שלכם ולאחריו סולמית", "reg_phone", 10, 9, 10))

        if "reg_phone" in p:
            sess["reg_phone"] = p.get("reg_phone")
            log_ivr(call_id, f"רישום חדש - נקלט טלפון: {sess['reg_phone']}")

        # שלב 3: הקלטת שם
        if "reg_name_audio" not in p and "reg_name_text" not in sess:
            log_ivr(call_id, "רישום חדש - שלב 3: בקשת הקלטת שם מלא")
            return send_yemot_response(yemot_record_no_menu("אמרו בקול ברור את שמכם הפרטי ושם המשפחה ולאחר מכן הקשו סולמית", "reg_name_audio"))

        curr_name_audio = p.get("reg_name_audio")
        if curr_name_audio and sess.get("last_name_audio") != curr_name_audio:
            sess["last_name_audio"] = curr_name_audio
            transcribed_name = transcribe_yemot_audio(curr_name_audio, call_id)

            if not transcribed_name:
                log_ivr(call_id, "שם לא זוהה - מבקש מהמאזין לחזור על השם")
                sess.pop("reg_name_text", None)
                sess.pop("last_name_audio", None)
                return send_yemot_response(yemot_record_no_menu("הדיבור אינו ברור אנא נסו שנית ואמרו את שמכם המלא ולאחר מכן הקשו סולמית", "reg_name_audio"))

            sess["reg_name_text"] = transcribed_name

        if "reg_name_confirm" not in p and "name_approved" not in sess:
            log_ivr(call_id, f"מבקש אישור על השם שפוענח: '{sess.get('reg_name_text')}'")
            msg = f"השם שנקלט הוא {sess.get('reg_name_text', '')} לאישור הקשו 1 להקלטה מחודשת הקשו 2"
            return send_yemot_response(yemot_read(msg, "reg_name_confirm", 1, 1, 6))

        if p.get("reg_name_confirm") == "2":
            log_ivr(call_id, "המאזין בחר להקליט את השם מחדש")
            sess.pop("reg_name_text", None)
            sess.pop("last_name_audio", None)
            sess.pop("name_approved", None)
            return send_yemot_response(yemot_record_no_menu("אמרו שוב בקול ברור את שמכם המלא ולאחר מכן הקשו סולמית", "reg_name_audio"))

        sess["name_approved"] = True

        # שלב 4: הקלטת כתובת
        if "reg_addr_audio" not in p and "reg_addr_text" not in sess:
            log_ivr(call_id, "רישום חדש - שלב 4: בקשת הקלטת כתובת")
            return send_yemot_response(yemot_record_no_menu("אמרו בקול ברור את הכתובת המלאה שלכם ולאחר מכן הקשו סולמית", "reg_addr_audio"))

        curr_addr_audio = p.get("reg_addr_audio")
        if curr_addr_audio and sess.get("last_addr_audio") != curr_addr_audio:
            sess["last_addr_audio"] = curr_addr_audio
            transcribed_addr = transcribe_yemot_audio(curr_addr_audio, call_id)

            if not transcribed_addr:
                log_ivr(call_id, "כתובת לא זוהתה - מבקש מהמאזין לחזור עליה")
                sess.pop("reg_addr_text", None)
                sess.pop("last_addr_audio", None)
                return send_yemot_response(yemot_record_no_menu("הדיבור אינו ברור אנא נסו שנית ואמרו את הכתובת המלאה ולאחר מכן הקשו סולמית", "reg_addr_audio"))

            sess["reg_addr_text"] = transcribed_addr

        if "reg_addr_confirm" not in p and "addr_approved" not in sess:
            log_ivr(call_id, f"מבקש אישור על הכתובת שפוענחה: '{sess.get('reg_addr_text')}'")
            msg = f"הכתובת שנקלטה היא {sess.get('reg_addr_text', '')} לאישור הקשו 1 להקלטה מחודשת הקשו 2"
            return send_yemot_response(yemot_read(msg, "reg_addr_confirm", 1, 1, 6))

        if p.get("reg_addr_confirm") == "2":
            log_ivr(call_id, "המאזין בחר להקליט את הכתובת מחדש")
            sess.pop("reg_addr_text", None)
            sess.pop("last_addr_audio", None)
            sess.pop("addr_approved", None)
            return send_yemot_response(yemot_record_no_menu("אמרו שוב בקול ברור את הכתובת המלאה ולאחר מכן הקשו סולמית", "reg_addr_audio"))

        sess["addr_approved"] = True
        sess["name"] = sess.get("reg_name_text", "משתמש חדש")
        sess["phone"] = sess.get("reg_phone", sess.get("caller_phone", ""))
        sess["address"] = sess.get("reg_addr_text", "-")

        # שמירת המשתמש בשיטס
        if "user_saved" not in sess:
            log_ivr(call_id, f"שומר משתמש חדש בגיליון: {sess['name']} | ת.ז: {sess['reg_id']} | טלפון: {sess['phone']}")
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=register_casual&idNumber={sess['reg_id']}&name={sess['name']}&phone={sess['phone']}&address={sess['address']}&source=טלפוני",
                    timeout=6
                )
            except Exception:
                pass
            sess["user_saved"] = True

    # -------------------------------------------------------------
    # 2. בחירת כרך (מבנה 6 ספרות: 3 סניף, 1 סדר, 2 כרך)
    # -------------------------------------------------------------
    if "book_choice" not in p and "book_choice" not in sess:
        log_ivr(call_id, "השמעת תפריט בחירת כרך: 1 למספר סידורי, 2 לבחירה בשלבים, 9 להסבר")
        msg = "לבחירת כרך באמצעות מספר סידורי בן 6 ספרות הקש 1 לבחירה בשלבים לפי סניף סדר וכרך הקש 2 להסבר על המספר הסידורי הקש 9"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1, 6))

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")
        log_ivr(call_id, f"המאזין בחר באפשרות בחירת כרך: מקש {sess['book_choice']}")

    b_choice = sess.get("book_choice")

    # מקש 9: הסבר על מבנה 6 הספרות
    if b_choice == "9":
        log_ivr(call_id, "השמעת הסבר מלא על מבנה 6 הספרות")
        sess.pop("book_choice", None)
        msg = "הסבר על המספר הסידורי. המספר הסידורי מורכב משש ספרות המוטבעות על גב הכרך. שלוש הספרות הראשונות הן מספר הסניף. הספרה הרביעית היא מספר הסדר מ-1 עד 6 למשל זרעים 1 נשים 3. ושתי הספרות האחרונות הן מספר הכרך. לבחירה במספר סידורי בן 6 ספרות הקש 1 לבחירה בשלבים הקש 2"
        return send_yemot_response(yemot_read(msg, "book_choice", 1, 1, 8))

    # מקש 1: הקשה ישירה של 6 ספרות
    if b_choice == "1":
        if "serial_num" not in p:
            log_ivr(call_id, "מבקש מהמאזין להקיש את המספר הסידורי בן 6 הספרות")
            return send_yemot_response(yemot_read("אנא הקישו את המספר הסידורי בן שש הספרות ולאחריו סולמית", "serial_num", 6, 6, 10))

        raw_num = p.get("serial_num", "").strip()
        branch = raw_num[:3]
        seder_digit = raw_num[3:4]
        volume = raw_num[4:6]
        seder_name = SEDARIM.get(seder_digit, f"סדר {seder_digit}")

        sess["book_id"] = raw_num
        sess["seder_mishna"] = f"סניף {branch} - סדר {seder_name} - כרך {volume}"
        log_ivr(call_id, f"נקלט מספר סידורי: {raw_num} | פוענח: סניף {branch}, סדר {seder_name}, כרך {volume}")

    # מקש 2: בחירה ב-3 שלבים נפרדים
    elif b_choice == "2":
        # שלב א: סניף (3 ספרות)
        if "step_branch" not in p and "step_branch" not in sess:
            log_ivr(call_id, "בחירה בשלבים - שלב א: בקשת מספר סניף (3 ספרות)")
            return send_yemot_response(yemot_read("הקישו את מספר הסניף בן 3 ספרות ולאחריו סולמית", "step_branch", 3, 3, 8))

        if "step_branch" in p:
            sess["step_branch"] = p.get("step_branch")
            log_ivr(call_id, f"בחירה בשלבים - נקלט סניף: {sess['step_branch']}")

        # שלב ב: סדר (ספרה אחת 1-6)
        if "step_seder" not in p and "step_seder" not in sess:
            log_ivr(call_id, "בחירה בשלבים - שלב ב: בקשת בחירת סדר (1 עד 6)")
            msg = "הקישו את מספר הסדר. 1 זרעים, 2 מועד, 3 נשים, 4 נזיקין, 5 קדשים, 6 טהרות ולאחריו סולמית"
            return send_yemot_response(yemot_read(msg, "step_seder", 1, 1, 8))

        if "step_seder" in p:
            sess["step_seder"] = p.get("step_seder")
            s_name = SEDARIM.get(sess["step_seder"], sess["step_seder"])
            log_ivr(call_id, f"בחירה בשלבים - נבחר סדר: {sess['step_seder']} ({s_name})")

        # שלב ג: כרך (2 ספרות)
        if "step_vol" not in p and "step_vol" not in sess:
            log_ivr(call_id, "בחירה בשלבים - שלב ג: בקשת מספר כרך (2 ספרות)")
            return send_yemot_response(yemot_read("הקישו את מספר הכרך בן שתי ספרות ולאחריו סולמית", "step_vol", 2, 1, 8))

        if "step_vol" in p:
            sess["step_vol"] = p.get("step_vol")
            log_ivr(call_id, f"בחירה בשלבים - נקלט כרך: {sess['step_vol']}")

        # הרכבת המספר הסידורי המלא
        full_branch = str(sess.get("step_branch", "000")).zfill(3)
        seder_digit = str(sess.get("step_seder", "1"))
        full_vol = str(sess.get("step_vol", "01")).zfill(2)
        full_serial = f"{full_branch}{seder_digit}{full_vol}"

        s_name = SEDARIM.get(seder_digit, f"סדר {seder_digit}")
        sess["book_id"] = full_serial
        sess["seder_mishna"] = f"סניף {full_branch} - סדר {s_name} - כרך {full_vol}"
        log_ivr(call_id, f"הורכב מספר סידורי מלא: {full_serial} | {sess['seder_mishna']}")

    # -------------------------------------------------------------
    # 3. פעולה: השאלה או החזרה
    # -------------------------------------------------------------
    if "action_choice" not in p:
        log_ivr(call_id, "מבקש מהמאזין לבחור פעולה: 1 להשאלה, 2 להחזרה")
        return send_yemot_response(yemot_read("להשאלת המשנה הקש 1 להחזרת המשנה הקש 2", "action_choice", 1, 1, 6))

    act = p.get("action_choice")

    # ביצוע השאלה (מקש 1)
    if act == "1":
        log_ivr(call_id, f"המאזין בחר בהשאלה! שולח נתונים לגיליון עבור ספר: {sess.get('book_id')}")
        try:
            requests.get(
                f"{SCRIPT_URL}?action=borrow&bookId={sess.get('book_id', '-')}&sederMishna={sess.get('seder_mishna', '-')}"
                f"&name={sess.get('name', 'משאיל')}&phone={sess.get('phone', '')}&address={sess.get('address', '-')}"
                f"&userType={sess.get('user_type', 'כללי')}&source=טלפוני",
                timeout=6
            )
            log_ivr(call_id, "ההשאלה נרשמה בהצלחה בגיליון! סיום שיחה.")
        except Exception as e:
            log_ivr(call_id, f"שגיאה בעדכון השאלה בגיליון: {e}")

        sessions.pop(call_id, None)
        return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה תודה ולהתראות", go_to=restart_folder))

    # ביצוע החזרה (מקש 2)
    elif act == "2":
        log_ivr(call_id, f"המאזין בחר בהחזרה! מעדכן בגיליון עבור ספר: {sess.get('book_id')}")
        try:
            res = requests.get(
                f"{SCRIPT_URL}?action=return&bookId={sess.get('book_id', '-')}&phone={sess.get('phone', '')}&source=טלפוני",
                timeout=6
            ).json()
            is_success = res.get("success", False)
        except Exception:
            is_success = True

        sessions.pop(call_id, None)
        if is_success:
            log_ivr(call_id, "ההחזרה עודכנה בהצלחה בגיליון והועברה להיסטוריה! סיום שיחה.")
            return send_yemot_response(yemot_msg("העדכון נקלט בהצלחה תודה ולהתראות", go_to=restart_folder))
        else:
            log_ivr(call_id, "לא נמצאה השאלה פעילה עבור ספר זה")
            return send_yemot_response(yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת תודה ולהתראות", go_to=restart_folder))

    return send_yemot_response(yemot_msg("תודה ולהתראות"))

def send_yemot_response(text: str):
    return PlainTextResponse(text)

def send_admin_email(phone, file_url):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    admin_mail = os.environ.get("ADMIN_EMAIL", smtp_user)
    if not smtp_user or not smtp_pass: return
    try:
        msg = EmailMessage()
        msg["Subject"] = "הודעה קולית חדשה - גמ\"ח משניות"
        msg["From"] = smtp_user
        msg["To"] = admin_mail
        msg.set_content(f"שלום,\nהתקבלה הודעה קולית חדשה מאת טלפון: {phone}\nקישור להקלטה: {file_url}")
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
    return JSONResponse({"status": "online", "message": "שרת גמח משניות פעיל"})
