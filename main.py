import os
import requests
import smtplib
from email.message import EmailMessage
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI(title="Gemach Reisman IVR Server")

# 👇 כתובת ה-Apps Script שלכם
SCRIPT_URL = os.environ.get(
    "SCRIPT_URL", 
    "https://script.google.com/macros/s/AKfycbyMS9azkxb4nCw99g4Il5n8vIfEKJTUedb1A80vI51PqpQh2BnCPmQ9X0e_2eS3SZQ3hw/exec"
)

# זיכרון שיחות פעילות
sessions = {}

SEDARIM = {
    "1": "זרעים", "2": "מועד", "3": "נשים",
    "4": "נזיקין", "5": "קדשים", "6": "טהרות"
}

def yemot_read(text: str, var_name: str, max_d=10, min_d=1, timeout=7):
    return f"read=t-{text}={var_name},no,{max_d},{min_d},{timeout},Number"

def yemot_msg(text: str, go_to="hangup"):
    return f"id_list_message=t-{text}&go_to_folder={go_to}"

def yemot_record(text: str, folder_name: str):
    return f"record=t-{text}={folder_name},no,yes"


# =========================================================================
# פונקציית הליבה שמטפלת בשיחות ימות המשיח
# =========================================================================
async def process_ivr(request: Request):
    # מיזוג פרמטרים מ-URL ומ-POST
    p = dict(request.query_params)
    if request.method == "POST":
        try:
            form_data = await request.form()
            p.update(dict(form_data))
        except Exception:
            pass

    call_id = p.get("ApiCallId", p.get("ApiPhone", "default_call"))

    # טיפול בניתוק שיחה מצד הלקוח (ניקוי זיכרון)
    if p.get("hangup") == "yes":
        sessions.pop(call_id, None)
        return PlainTextResponse("hangup")

    caller_phone = p.get("ApiPhone", "")
    step = p.get("step", "init")

    if call_id not in sessions:
        sessions[call_id] = {"caller_phone": caller_phone}
    sess = sessions[call_id]

    # --- שלב 1: פתיח ובחירת משתמש ---
    if step == "init":
        msg = "ברוך הבא לגמ\"ח משניות רייזמן להשאלה. משתמש רשום בנדרים פלוס, הקש 1. משתמש מזדמן, הקש 2. הודעה לגמ\"ח, הקש 3."
        return PlainTextResponse(yemot_read(msg, "user_menu", 1) + "&step=user_menu")

    if step == "user_menu":
        choice = p.get("user_menu")
        sess["user_menu"] = choice

        # מקש 3: השארת הודעה
        if choice == "3":
            return PlainTextResponse(yemot_record("הקלט את הודעתך לאחר הישמע הצליל ולסיום הקש סולמית.", "message_audio") + "&step=msg_confirm")

        # מקש 1: נדרים פלוס
        elif choice == "1":
            return PlainTextResponse(yemot_read("הקש את מספר הזהות ולאחריו הקש סולמית:", "id_num", 9, 7) + "&step=nedarim_id")

        # מקש 2: משתמש מזדמן
        elif choice == "2":
            sess["user_type"] = "מזדמן"
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית.", "name_audio") + "&step=casual_name_confirm")

        return PlainTextResponse(yemot_msg("מקש שגוי.") + "&step=init")

    # --- מקש 3: אישור הודעה ---
    if step == "msg_confirm":
        conf = p.get("message_audio_confirm")
        if conf == "1":
            file_url = p.get("message_audio_url", "לא צורף קישור")
            send_admin_email(sess.get("caller_phone", ""), file_url)
            return PlainTextResponse(yemot_msg("הודעתך נשמרה ונשלחה בהצלחה. תודה ולהתראות."))
        else:
            return PlainTextResponse(yemot_record("הקלט את הודעתך שוב לאחר הצליל ולסיום הקש סולמית.", "message_audio") + "&step=msg_confirm")

    # --- מקש 1: נדרים פלוס (ת"ז וסיסמה) ---
    if step == "nedarim_id":
        sess["id_num"] = p.get("id_num")
        return PlainTextResponse(yemot_read("הקש את סיסמתך בנדרים פלוס ולאחריו הקש סולמית:", "nedarim_pass", 8, 4) + "&step=nedarim_pass")

    if step == "nedarim_pass":
        sess["nedarim_pass"] = p.get("nedarim_pass")
        try:
            res = requests.get(
                f"{SCRIPT_URL}?action=verify_nedarim&id_number={sess['id_num']}&password={sess['nedarim_pass']}",
                timeout=8
            ).json()
        except Exception:
            res = {"success": False}

        if res.get("success"):
            sess["user_type"] = "נדרים פלוס"
            sess["name"] = res.get("name")
            sess["phone"] = res.get("phone", sess.get("caller_phone", ""))
            return PlainTextResponse(go_to_book_selection())
        else:
            return PlainTextResponse(yemot_msg("המערכת מזהה כי הפרטים אינם תואמים. אנא נסה שנית.") + "&step=user_menu&user_menu=1")

    # --- מקש 2: רישום משתמש מזדמן ---
    if step == "casual_name_confirm":
        if p.get("name_audio_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם פרטי ומשפחה ולאחר מכן הקש סולמית.", "name_audio") + "&step=casual_name_confirm")

        sess["name"] = "משתמש מזדמן (מוקלט)"
        return PlainTextResponse(yemot_read("הקש מספר פלאפון ולאחר מכן הקש סולמית:", "casual_phone", 10, 9) + "&step=casual_phone")

    if step == "casual_phone":
        sess["phone"] = p.get("casual_phone")
        return PlainTextResponse(yemot_record("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית.", "address_audio") + "&step=casual_address_confirm")

    if step == "casual_address_confirm":
        if p.get("address_audio_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור בקול ברור את הכתובת המלאה שלך ולאחר מכן הקש סולמית.", "address_audio") + "&step=casual_address_confirm")

        sess["address"] = "כתובת מוקלטת"
        try:
            requests.get(
                f"{SCRIPT_URL}?action=register_casual&name={sess['name']}&phone={sess['phone']}&address={sess['address']}",
                timeout=8
            )
        except Exception:
            pass

        return PlainTextResponse(go_to_book_selection())

    # --- שלב 2: בחירת משנה ---
    if step == "book_selection":
        b_choice = p.get("book_choice")
        if b_choice == "9":
            msg = "הסבר שימוש: המספר הסידורי מוטבע על גב כרך המשניות. בהקשת מספר זה ניתן להשאיל או להחזיר את הכרך באופן ישיר."
            return PlainTextResponse(yemot_msg(msg) + "&step=book_selection_menu")
        elif b_choice == "1":
            return PlainTextResponse(yemot_read("הקש את המספר הסידורי ולאחריו הקש סולמית:", "serial_num", 6, 1) + "&step=got_serial")
        elif b_choice == "2":
            msg = "הקש את מספר הסדר: 1 זרעים, 2 מועד, 3 נשים, 4 נזיקין, 5 קדשים, 6 טהרות, ולאחריו הקש סולמית:"
            return PlainTextResponse(yemot_read(msg, "seder_num", 1) + "&step=got_seder")

        return PlainTextResponse(go_to_book_selection())

    if step == "book_selection_menu":
        return PlainTextResponse(go_to_book_selection())

    if step == "got_serial":
        sess["book_id"] = p.get("serial_num")
        sess["seder_mishna"] = "-"
        return PlainTextResponse(go_to_action())

    if step == "got_seder":
        seder_idx = p.get("seder_num", "1")
        sess["seder_name"] = SEDARIM.get(seder_idx, "כללי")
        return PlainTextResponse(yemot_record("אמור בקול ברור את שם המשנה ולאחר מכן הקש סולמית.", "mishna_name_audio") + "&step=mishna_audio_confirm")

    if step == "mishna_audio_confirm":
        if p.get("mishna_name_audio_confirm") == "2":
            return PlainTextResponse(yemot_record("אמור בקול ברור את שם המשנה ולאחר מכן הקש סולמית.", "mishna_name_audio") + "&step=mishna_audio_confirm")

        sess["book_id"] = "-"
        sess["seder_mishna"] = f"{sess.get('seder_name', '')} (מוקלט)"
        return PlainTextResponse(go_to_action())

    # --- שלב 3: פעולה (השאלה או החזרה) ---
    if step == "action_choice":
        act = p.get("action_choice")
        if act == "1":
            # השאלה
            try:
                requests.get(
                    f"{SCRIPT_URL}?action=borrow&bookId={sess.get('book_id')}&sederMishna={sess.get('seder_mishna')}"
                    f"&name={sess.get('name')}&phone={sess.get('phone')}&address={sess.get('address', '-')}&userType={sess.get('user_type')}",
                    timeout=8
                )
            except Exception:
                pass
            return PlainTextResponse(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות."))

        elif act == "2":
            # החזרה
            try:
                res = requests.get(
                    f"{SCRIPT_URL}?action=return&bookId={sess.get('book_id')}&sederMishna={sess.get('seder_mishna')}&phone={sess.get('phone')}",
                    timeout=8
                ).json()
            except Exception:
                res = {"success": True}

            if res.get("success"):
                return PlainTextResponse(yemot_msg("העדכון נקלט בהצלחה, תודה ולהתראות."))
            else:
                return PlainTextResponse(yemot_msg("לא נמצאה השאלה פעילה מתאימה במערכת. תודה ולהתראות."))

    return PlainTextResponse(yemot_msg("תודה ולהתראות."))


def go_to_book_selection():
    msg = "לבחירת משנה דרך המספר הסידורי הקש 1. לפרטים מלאים הקש 2. להסבר על המספר הסידורי הקש 9."
    return yemot_read(msg, "book_choice", 1) + "&step=book_selection"

def go_to_action():
    msg = "להשאלת המשנה הקש 1. להחזרת המשנה הקש 2."
    return yemot_read(msg, "action_choice", 1) + "&step=action_choice"

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
# הגדרת הניתובים: תמיכה גם ב-`/` וגם ב-`/ivr`
# =========================================================================

# ניתוב עבור השלוחה הרגילה /ivr
@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_explicit(request: Request):
    return await process_ivr(request)

# ניתוב עבור הנתיב הראשי / (בדיוק מה שימות המשיח שלך מחייג אליו!)
@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def root_router(request: Request):
    # אם זו קריאה של ימות המשיח (יש בה פרמטרים של טלפון או שיחה)
    if request.query_params.get("ApiCallId") or request.query_params.get("ApiPhone"):
        return await process_ivr(request)
    
    # אם זו בדיקת תקינות רגילה של דפדפן / Render
    return JSONResponse({"status": "online", "message": "שרת גמ\"ח משניות רייזמן פעיל ותקין"})
