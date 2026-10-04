import os
import requests
import smtplib
from email.message import EmailMessage
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI(title="Gemach Reisman IVR Server")

SCRIPT_URL = os.environ.get(
    "SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbyMS9azkxb4nCw99g4Il5n8vIfEKJTUedb1A80vI51PqpQh2BnCPmQ9X0e_2eS3SZQ3hw/exec"
)

# זיכרון שיחות לפי ApiCallId
sessions = {}

SEDARIM = {
    "1": "זרעים", "2": "מועד", "3": "נשים",
    "4": "נזיקין", "5": "קדשים", "6": "טהרות"
}

def clean_tts(text: str) -> str:
    """ניקוי תווים שאסורים בימות המשיח: נקודות וגרשיים"""
    return text.replace(".", " - ").replace('"', '').replace("'", "")

def yemot_read(text: str, var_name: str, max_d=10, min_d=1, timeout=7):
    """קבלת הקשת מקשים נקייה לפי הפורמט של ימות המשיח"""
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,{max_d},{min_d},{timeout},No"

def yemot_record(text: str, var_name: str):
    """הקלטת קול לפי הפורמט הרשמי של ימות המשיח"""
    cleaned = clean_tts(text)
    return f"read=t-{cleaned}={var_name},no,record"

def yemot_msg(text: str, go_to="hangup"):
    """השמעת הודעה וניתוק"""
    cleaned = clean_tts(text)
    return f"id_list_message=t-{cleaned}&go_to_folder={go_to}"


# =========================================================================
# פונקציית ניהול השיחה
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

    # טיפול בניתוק השיחה
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
    # אפשרות 3: השארת הודעה לגמ"ח
    # -------------------------------------------------------------
    if menu_choice == "3":
        if "message_audio" not in p:
            return PlainTextResponse(yemot_record("הקלט את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

        if "msg_confirm" not in p:
            return PlainTextResponse(yemot_read("לאישור ההודעה הקישו 1, להקלטה מחדש הקישו 2", "msg_confirm", 1, 1))

        if p.get("msg_confirm") == "1":
            audio_url = p.get("message_audio", "לא צורף קישור")
            send_admin_email(sess.get("caller_phone", ""), audio_url)
            return PlainTextResponse(yemot_msg("הודעתך נשמרה ונשלחה בהצלחה, תודה ולהתראות"))
        else:
            # הקלטה מחדש
            return PlainTextResponse(yemot_record("הקלט שוב את הודעתך לאחר הצליל ולסיום הקש סולמית", "message_audio"))

    # -------------------------------------------------------------
    # אפשרות 1: משתמש נדרים פלוס
    # -------------------------------------------------------------
    if menu_choice == "1":
        if "id_num" not in p:
            return PlainTextResponse(yemot_read("הקש את מספר הזהות ולאחריו הקש סולמית", "id_num", 9, 8, 10))

        sess["id_num"] = p.get("id_num")

        if "nedarim_pass" not in p:
            return PlainTextResponse(yemot_read("הקש את סיסמתך בנדרים פלוס ולאחריו הקש סולמית", "nedarim_pass", 8, 4, 10))

        sess["nedarim_pass"] = p.get("nedarim_pass")

        # אימות מול השיטס
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
                # איפוס נתונים כדי שיקיש מחדש
                sess.pop("id_num", None)
                sess.pop("nedarim_pass", None)
                return PlainTextResponse(yemot_read("הפרטים אינם תואמים, הקש שוב את מספר הזהות ובסיום סולמית", "id_num", 9, 8, 10))

    # -------------------------------------------------------------
    # אפשרות 2: משתמש מזדמן
    # -------------------------------------------------------------
    if menu_choice == "2":
        sess["user_type"] = "מזדמן"

        if "name_audio" not in p:
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        if "name_confirm" not in p:
            return PlainTextResponse(yemot_read("לאישור השם הקישו 1, להקלטה מחדש הקישו 2", "name_confirm", 1, 1))

        if p.get("name_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית", "name_audio"))

        sess["name"] = "מזדמן מוקלט"

        if "casual_phone" not in p:
            return PlainTextResponse(yemot_read("הקש מספר פלאפון ולאחר מכן הקש סולמית", "casual_phone", 10, 9, 10))

        sess["phone"] = p.get("casual_phone")

        if "address_audio" not in p:
            return PlainTextResponse(yemot_record("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        if "addr_confirm" not in p:
            return PlainTextResponse(yemot_read("לאישור הכתובת הקישו 1, להקלטה מחדש הקישו 2", "addr_confirm", 1, 1))

        if p.get("addr_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית", "address_audio"))

        sess["address"] = "כתובת מוקלטת"

        # רישום בשיטס (פעם אחת בלבד)
        if "casual_registered" not in sess:
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=register_casual&name={sess['name']}&phone={sess['phone']}&address={sess['address']}",
                    timeout=5
                )
            except Exception:
                pass
            sess["casual_registered"] = True

    # -------------------------------------------------------------
    # 2. בחירת משנה (משותף לכולם)
    # -------------------------------------------------------------
    if "book_choice" not in p and "book_choice" not in sess:
        msg = "לבחירת משנה דרך המספר הסידורי הקש 1, לפרטים מלאים הקש 2, להסבר על המספר הסידורי הקש 9"
        return PlainTextResponse(yemot_read(msg, "book_choice", 1, 1))

    if "book_choice" in p:
        sess["book_choice"] = p.get("book_choice")

    b_choice = sess.get("book_choice")

    # הסבר שימוש (מקש 9)
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

        if "mishna_confirm" not in p:
            return PlainTextResponse(yemot_read("לאישור שם המשנה הקישו 1, להקלטה מחדש הקישו 2", "mishna_confirm", 1, 1))

        if p.get("mishna_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור שוב בקול ברור את שם המשנה ולאחר מכן הקש סולמית", "mishna_audio"))

        sess["book_id"] = "-"
        sess["seder_mishna"] = f"{sess.get('seder_name', '')} מוקלט"

    # -------------------------------------------------------------
    # 3. פעולה: השאלה (1) או החזרה (2)
    # -------------------------------------------------------------
    if "action_choice" not in p:
        return PlainTextResponse(yemot_read("להשאלת המשנה הקש 1, להחזרת המשנה הקש 2", "action_choice", 1, 1))

    act = p.get("action_choice")
    if act == "1":
        # השאלה
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
        # החזרה
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


# =========================================================================
# ניתובים לשרת
# =========================================================================
@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_endpoint(request: Request):
    return await handle_call(request)

@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def root_endpoint(request: Request):
    # אם ימות המשיח פונה ישירות לנתיב הראשי
    if request.query_params.get("ApiCallId") or request.query_params.get("ApiPhone"):
        return await handle_call(request)
    return JSONResponse({"status": "online", "message": "שרת גמח משניות רייזמן פעיל"})
