import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import speech_recognition as sr

app = FastAPI()

# --- התחברות ל-Google Sheets ---
# שמרו את ה-JSON של Service Account בקובץ credentials.json או כמשתנה סביבה
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
SHEET_NAME = "גמ\"ח משניות רייזמן"

def get_sheet(tab_name):
    return client.open(SHEET_NAME).worksheet(tab_name)

# זיכרון זמני לניהול שלבי השיחה (Session)
sessions = {}

# עזר: תגובה בפרוטוקול ימות המשיח (קריאה / השמעת הודעה)
def yemot_response(text="", read_var=None, max_digits=10, min_digits=1):
    if read_var:
        return f"read=t-{text}={read_var},no,{max_digits},{min_digits},7,Number"
    return f"id_list_message=t-{text}&go_to_folder=hangup"

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr_gateway(request: Request):
    params = dict(request.query_params)
    caller_id = params.get("ApiPhone", "")
    call_id = params.get("ApiCallId", caller_id)
    step = params.get("step", "menu")
    
    if call_id not in sessions:
        sessions[call_id] = {"phone": caller_id}
    session = sessions[call_id]

    # --- תפריט ראשי ---
    if step == "menu":
        user_input = params.get("menu_choice")
        if not user_input:
            msg = "שלום וברכה. להשאלת ספר הקישו 1. להחזרת ספר הקישו 2. לשמיעת הספרים המושאלים שלכם הקישו 3. לרישום הקישו 4. להשארת הודעה הקישו 8."
            return yemot_response(msg, read_var="menu_choice", max_digits=1, min_digits=1) + "&step=menu"
        
        session["target_action"] = user_input
        if user_input in ["1", "2", "3"]:
            # שאלת זיהוי טלפוני
            return yemot_response("לכניסה עם המספר ממנו התקשרתם הקישו 1, לכניסה עם מספר אחר הקישו 2.", read_var="auth_type", max_digits=1) + "&step=auth_check"
        elif user_input == "4":
            return PlainTextResponse("go_to_folder=/4")  # ניתוב לשלוחת הקלטה
        elif user_input == "8":
            return PlainTextResponse("go_to_folder=/8")

    # --- שלב זיהוי המספר ---
    if step == "auth_check":
        auth_type = params.get("auth_type")
        if auth_type == "2":
            return yemot_response("אנא הקישו את מספר הטלפון שלכם ובסיום סולמית", read_var="manual_phone", max_digits=10) + "&step=verify_user"
        else:
            session["phone"] = caller_id
            return await handle_user_routing(session)

    if step == "verify_user":
        session["phone"] = params.get("manual_phone", caller_id)
        return await handle_user_routing(session)

    # --- שלוחה 1: השאלת ספר ---
    if step == "borrow_book":
        book_id = params.get("book_id")
        if not book_id:
            return yemot_response("אנא הקש את מספר הספר אותו ברצונך להשאיל ובסיום הקש סולמית", read_var="book_id", max_digits=6) + "&step=borrow_book"
        
        session["book_id"] = book_id
        
        # בדיקת רישום לצינתוק
        u_sheet = get_sheet("משתמשים")
        users = u_sheet.get_all_records()
        current_user = next((u for u in users if str(u.get("מספר טלפון")) == session["phone"]), None)
        
        if not current_user or current_user.get("רשום לצינתוק") != "כן":
            return yemot_response("הינך מועבר לשלוחת רישום לקבלת צינתוק בתאריך ההחזרה.") + "&step=tzintuk_redirect"
        
        return await finalize_borrowing(session, current_user.get("שם מלא", "לא ידוע"))

    if step == "tzintuk_redirect":
        # וידוא רישום לאחר הצינתוק
        u_sheet = get_sheet("משתמשים")
        users = u_sheet.get_all_records()
        current_user = next((u for u in users if str(u.get("מספר טלפון")) == session["phone"]), None)
        if current_user and current_user.get("רשום לצינתוק") == "כן":
            return await finalize_borrowing(session, current_user.get("שם מלא", "לא ידוע"))
        else:
            return yemot_response("המערכת מזהה כי לא נרשמת. הינך מועבר לרישום מחדש.") + "&step=tzintuk_redirect"

    # --- שלוחה 2: החזרת ספר ---
    if step == "return_choose":
        chosen_index = int(params.get("ret_choice", "1")) - 1
        active_loans = session.get("user_loans", [])
        if 0 <= chosen_index < len(active_loans):
            row_to_return = active_loans[chosen_index]
            sheet = get_sheet("השאלות פעילות")
            hist_sheet = get_sheet("היסטוריה")
            
            # העברה להיסטוריה ומחיקה מהפעילים
            row_num = row_to_return["row_num"]
            row_vals = sheet.row_values(row_num)
            row_vals[5] = "הוחזר"
            row_vals.append(datetime.now().strftime("%d/%m/%Y"))
            hist_sheet.append_row(row_vals)
            sheet.delete_rows(row_num)
            
            return yemot_response("ההחזרה עודכנה בהצלחה. תודה רבה.")
        return yemot_response("שגיאה בבחירה.")

    return yemot_response("תודה שהתקשרתם לגמ\"ח.")

# --- פונקציות עזר פנימיות ---
async def handle_user_routing(session):
    u_sheet = get_sheet("משתמשים")
    users = u_sheet.get_all_records()
    user_exists = any(str(u.get("מספר טלפון")) == session["phone"] for u in users)

    if not user_exists:
        return yemot_response("אינך רשום במערכת. הינך מועבר להרשמה.") + "&step=menu&menu_choice=4"

    target = session.get("target_action")
    if target == "1":
        return yemot_response("אנא הקש את מספר הספר אותו ברצונך להשאיל ובסיום סולמית", read_var="book_id", max_digits=6) + "&step=borrow_book"
    
    elif target == "2":
        # החזרה
        loans_sheet = get_sheet("השאלות פעילות")
        records = loans_sheet.get_all_records()
        user_loans = [{"row_num": idx + 2, **rec} for idx, rec in enumerate(records) if str(rec.get("טלפון")) == session["phone"]]
        session["user_loans"] = user_loans
        
        if not user_loans:
            return yemot_response("אין ספרים מושאלים על שימך.")
        elif len(user_loans) == 1:
            return yemot_response(f"המערכת מזהה כי ספר {user_loans[0]['מספר ספר']} מושאל על ידך. להחזרת הספר הקישו 1.", read_var="ret_choice", max_digits=1) + "&step=return_choose"
        else:
            msg = "המערכת מזהה כי קיימים מספר ספרים המושאלים על ידך. "
            for i, l in enumerate(user_loans):
                msg += f"לספר מספר {l['מספר ספר']} הקישו {i+1}. "
            return yemot_response(msg, read_var="ret_choice", max_digits=2) + "&step=return_choose"

    elif target == "3":
        # שמיעת ספרים מושאלים
        loans_sheet = get_sheet("השאלות פעילות")
        records = loans_sheet.get_all_records()
        books = [str(r.get("מספר ספר")) for r in records if str(r.get("טלפון")) == session["phone"]]
        if not books:
            return yemot_response("לא נמצאו ספרים מושאלים בחשבונך.")
        return yemot_response("הספרים שברשותך הם: " + ", ".join(books))

async def finalize_borrowing(session, user_name):
    sheet = get_sheet("השאלות פעילות")
    now = datetime.now()
    return_date = now + timedelta(days=3)
    sheet.append_row([
        session["book_id"],
        user_name,
        session["phone"],
        now.strftime("%d/%m/%Y"),
        return_date.strftime("%d/%m/%Y"),
        "מושאל"
    ])
    return yemot_response("ההשאלה עודכנה בהצלחה. תאריך ההחזרה הוא בעוד 3 ימים. שלום ולהתראות.")

# --- שלוחה 4: פענוח הקלטת שם (SpeechRecognition) ---
@app.post("/upload_name")
async def upload_name(phone: str, file_path: str):
    recognizer = sr.Recognizer()
    with sr.AudioFile(file_path) as source:
        audio = recognizer.record(source)
    try:
        user_name = recognizer.recognize_google(audio, language="he-IL")
    except Exception:
        user_name = "לא זוהה"

    u_sheet = get_sheet("משתמשים")
    u_sheet.append_row([phone, user_name, datetime.now().strftime("%d/%m/%Y"), "לא"])
    return {"status": "ok", "name": user_name}

# --- שלוחה 8: שליחת הודעה מוקלטת למייל ---
@app.post("/send_voice_mail")
async def send_voice_mail(audio_url: str, phone: str):
    msg = EmailMessage()
    msg["Subject"] = "פנייה ממערכת ההשאלות"
    msg["From"] = os.getenv("EMAIL_USER", "gemach@gmail.com")
    msg["To"] = os.getenv("ADMIN_EMAIL", "admin@gmail.com")
    msg.set_content(f"התקבלה פנייה חדשה מאת הטלפון: {phone}\nקישור להקלטה: {audio_url}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS"))
        smtp.send_message(msg)
    return {"status": "sent"}
