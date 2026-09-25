import os
import requests
import smtplib
from email.message import EmailMessage
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import speech_recognition as sr

app = FastAPI()

# 👇 הזינו פה פעם אחת את הקישור שקיבלתם מפריסת ה-Apps Script:
SCRIPT_URL = "https://script.google.com/macros/s/AKfycbx_YOUR_ID_HERE/exec"

sessions = {}

def yemot(text="", read_var=None, max_digits=10):
    if read_var:
        return f"read=t-{text}={read_var},no,{max_digits},1,7,Number"
    return f"id_list_message=t-{text}&go_to_folder=hangup"

@app.get("/ivr", response_class=PlainTextResponse)
async def ivr(request: Request):
    p = dict(request.query_params)
    caller = p.get("ApiPhone", "")
    call_id = p.get("ApiCallId", caller)
    step = p.get("step", "menu")

    if call_id not in sessions:
        sessions[call_id] = {"phone": caller}
    sess = sessions[call_id]

    # תפריט ראשי
    if step == "menu":
        choice = p.get("menu_choice")
        if not choice:
            return yemot("להשאלת ספר הקישו 1, להחזרה 2, לרשימת הספרים שלכם 3, לרישום 4, להשארת הודעה 8.", "menu_choice", 1) + "&step=menu"
        sess["action"] = choice
        if choice in ["1", "2", "3"]:
            return yemot("לכניסה עם המספר ממנו התקשרתם הקישו 1, למספר אחר הקישו 2.", "auth_type", 1) + "&step=auth"
        elif choice == "4":
            return PlainTextResponse("go_to_folder=/4")
        elif choice == "8":
            return PlainTextResponse("go_to_folder=/8")

    # זיהוי טלפון
    if step == "auth":
        if p.get("auth_type") == "2":
            return yemot("הקישו את מספר הטלפון שלכם ובסיום סולמית:", "manual_phone", 10) + "&step=verify"
        sess["phone"] = caller
        return check_and_route(sess)

    if step == "verify":
        sess["phone"] = p.get("manual_phone", caller)
        return check_and_route(sess)

    # 1. השאלה
    if step == "borrow":
        book_id = p.get("book_id")
        if not book_id:
            return yemot("הקישו את מספר הספר להשאלה ובסיום סולמית:", "book_id", 6) + "&step=borrow"
        
        # עדכון בשיטס ב-GET פשוט!
        requests.get(f"{SCRIPT_URL}?action=borrow&bookId={book_id}&phone={sess['phone']}&name={sess.get('name', 'משאיל')}")
        return yemot("ההשאלה עודכנה בהצלחה ל-3 ימים. להתראות!")

    # 2. החזרה
    if step == "return_choose":
        idx = int(p.get("ret_choice", "1")) - 1
        loans = sess.get("loans", [])
        if 0 <= idx < len(loans):
            book_to_return = loans[idx]
            requests.get(f"{SCRIPT_URL}?action=return&bookId={book_to_return}")
            return yemot("ההחזרה עודכנה בהצלחה, תודה רבה!")
        return yemot("שגיאה בבחירה.")

    return yemot("שלום ולהתראות.")

def check_and_route(sess):
    # בדיקת משתמש ישירות מול השיטס
    res = requests.get(f"{SCRIPT_URL}?action=check_user&phone={sess['phone']}").json()
    if not res.get("exists"):
        return yemot("אינכם רשומים במערכת. הנכם מועברים לרישום.") + "&step=menu&menu_choice=4"
    
    sess["name"] = res.get("name")
    action = sess["action"]

    if action == "1":
        return yemot("אנא הקישו את מספר הספר להשאלה ובסיום סולמית:", "book_id", 6) + "&step=borrow"

    if action == "2":
        # בדיקת ספרים מושאלים מהשיטס
        l_res = requests.get(f"{SCRIPT_URL}?action=get_loans&phone={sess['phone']}").json()
        loans = l_res.get("books", [])
        sess["loans"] = loans

        if not loans:
            return yemot("אין ספרים מושאלים על שימכם.")
        if len(loans) == 1:
            return yemot(f"זוהה ספר מושאל מספר {loans[0]}. להחזרתו הקישו 1.", "ret_choice", 1) + "&step=return_choose"
        
        msg = "קיימים מספר ספרים ברשותכם. "
        for i, b in enumerate(loans):
            msg += f"לספר {b} הקישו {i+1}. "
        return yemot(msg, "ret_choice", 2) + "&step=return_choose"

    if action == "3":
        l_res = requests.get(f"{SCRIPT_URL}?action=get_loans&phone={sess['phone']}").json()
        loans = l_res.get("books", [])
        if not loans:
            return yemot("אין ספרים מושאלים ברשותכם.")
        return yemot("הספרים שברשותכם הם: " + ", ".join(loans))

# שלוחה 4: פענוח שם ושמירה בשיטס
@app.post("/upload_name")
async def upload_name(phone: str, file_path: str):
    recognizer = sr.Recognizer()
    try:
        with sr.AudioFile(file_path) as source:
            audio = recognizer.record(source)
        name = recognizer.recognize_google(audio, language="he-IL")
    except:
        name = "משתמש חדש"
    
    requests.get(f"{SCRIPT_URL}?action=register_user&phone={phone}&name={name}")
    return {"status": "ok", "name": name}
