from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO
from dotenv import load_dotenv
import os

# PDF Generation
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

# ---------------- GOOGLE SHEET SETUP ----------------
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = ServiceAccountCredentials.from_json_keyfile_name(
    os.getenv("SERVICE_ACCOUNT_FILE"), SCOPE
)

client = gspread.authorize(creds)
sheet = client.open_by_key(os.getenv("SPREADSHEET_ID"))

components_ws = sheet.worksheet("Components")
requests_ws = sheet.worksheet("Requests")
users_ws = sheet.worksheet("Users")

# ---------------- HELPERS ----------------

def get_components():
    return components_ws.get_all_records()

def get_requests():
    return requests_ws.get_all_records()

def get_user(email):
    users = users_ws.get_all_records()
    return next((u for u in users if u.get("Email", "").lower() == email.lower()), None)

def get_user_requests(user_email):
    rows = requests_ws.get("A2:L") or []  # Now reads up to column L
    out = []
    for row in rows:
        row += [""] * 12
        (
            req_id, email, uname, cid, cname, qty, purpose,
            status, req_at, appr_by, issued_at, returned_qty
        ) = row[:12]

        if email.lower() == user_email.lower():
            out.append({
                "Request_ID": req_id,
                "User_Email": email,
                "User_Name": uname,
                "Component_Name": cname,
                "Qty_Requested": qty,
                "Purpose": purpose,
                "Status": status,
                "Requested_At": req_at,
                "Issued_At": issued_at,
                "Returned_Qty": returned_qty or "0"
            })
    return out


def get_manager_requests():
    rows = requests_ws.get("A2:L") or []
    out = []
    for row in rows:
        row += [""] * 12
        out.append({
            "Request_ID": row[0],
            "User_Email": row[1],
            "User_Name": row[2],
            "Component_Name": row[4],
            "Qty_Requested": row[5],
            "Purpose": row[6],
            "Status": row[7],
            "Requested_At": row[8],
            "Approved_By": row[9],
            "Issued_At": row[10],
            "Returned_Qty": row[11] or "0"
        })
    return out

def get_approved_requests():
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (req_id, email, uname, cid, cname, qty, purpose, status, req_at, appr_by) = (row + [""] * 10)[:10]

        if status.lower() == "approved":
            out.append({
                "Request_ID": req_id,
                "User_Name": uname,
                "Purpose": purpose,
                "Component_Name": cname,
                "Qty_Requested": qty,   # FIXED
                "Requested_At": req_at
            })
    return out


def update_request_status(request_id, new_status, manager_name=""):
    try:
        cells = requests_ws.findall(str(request_id), in_column=1)
        if not cells:
            return False

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        updates = []

        for cell in cells:
            row = cell.row
            updates.extend([
                {"range": f"H{row}", "values": [[new_status]]},                            # Status → Column H
                {"range": f"J{row}", "values": [[ts if new_status in ["Approved","Rejected","Issued"] else ""]]},  # Timestamp → Column J
                {"range": f"K{row}", "values": [[manager_name if new_status in ["Approved","Rejected"] else ""]]},   # Approved By → Column K
            ])
            if new_status == "Issued":
                updates.append({"range": f"L{row}", "values": [[ts]]})  # Issued At → Column L

        requests_ws.batch_update({"valueInputOption": "RAW", "data": updates})
        return True

    except Exception as e:
        print("Update error:", e)
        return False

# ---------------- LOGIN DECORATOR ----------------
def login_required(role=None):
    def wrapper(fn):
        def inner(*args, **kwargs):
            if 'email' not in session:
                return redirect(url_for('login'))

            user = get_user(session['email'])
            if not user:
                session.pop('email', None)
                return redirect(url_for("login"))

            if role and role.lower() not in (user.get("Role") or "").lower():
                if "manager" not in (user.get("Role") or "").lower():
                    flash("Access denied", "danger")
                    return redirect(url_for('login'))

            return fn(user=user, *args, **kwargs)
        inner.__name__ = fn.__name__
        return inner
    return wrapper


# ---------------- ROUTES ----------------
@app.route("/")
def index():
    if "email" in session:
        user = get_user(session["email"])
        if not user:
            return redirect(url_for("login"))

        role = user.get("Role", "").lower()
        if "manager" in role:
            return redirect(url_for("manager_dashboard"))
        if "stock" in role:
            return redirect(url_for("stock_dashboard"))
        return redirect(url_for("user_dashboard"))

    return redirect(url_for("login"))

# ---------------- AUTH ROUTES ----------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        user = get_user(email)
        if user and user.get("Password") == password:
            session['email'] = email
            flash(f"Welcome back, {user.get('Name','') }!", "success")
            return redirect(url_for('index'))
        flash("Invalid email or password", "danger")
    return render_template("login.html")


@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        repassword = request.form.get('RePassword','')
        if password != repassword:
            flash("Passwords do not match!", "danger")
            return render_template("signup.html")
        if get_user(email):
            flash("Email already registered!", "danger")
            return render_template("signup.html")

        # Append row
        users_ws.append_row([
            request.form.get("EmployeeID",""),
            request.form.get("Name",""),
            request.form.get("Designation",""),
            request.form.get("Role",""),
            email,
            password
        ])
        flash("Account created successfully! Please login.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.pop("email", None)
    flash("Logged out successfully", "info")
    return redirect(url_for("login"))

# ---------------- USER DASHBOARD ----------------
@app.route("/user")
@login_required()
def user_dashboard(user):
    components = get_components()
    my_requests = get_user_requests(user.get("Email") or session.get("email"))
    return render_template("user_dashboard.html", user=user, components=components, requests=my_requests)


@app.route("/api/components")
def api_components():
    components = get_components()
    return jsonify([{
        "name": c.get("Name",""),
        "tray": c.get("Location","N/A"),
        "stock": int(c.get("Current_Stock",0) or 0)
    } for c in components])

# ---------------- SUBMIT REQUEST---------------- 
@app.route("/submit_request", methods=["POST"])
@login_required()
def submit_request(user):
    data = request.get_json(force=True)
    project = data.get("project", "").strip()
    items = data.get("items", [])

    if not project or not items:
        return jsonify({"error": "Project and items required"}), 400

    # READ ONLY NON-EMPTY CELLS FROM COLUMN A
    try:
        all_values = requests_ws.col_values(1)  # Entire column A
        req_ids = []
        for val in all_values[1:]:  # Skip header
            val = str(val).strip()
            if val.startswith("REQ") and val[3:].isdigit():
                req_ids.append(int(val[3:]))
        new_num = (max(req_ids) + 1) if req_ids else 1
    except:
        new_num = 1

    request_id = f"REQ{new_num:04d}"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    for item in items:
        requests_ws.append_row([
            request_id,
            user.get("Email"),
            user.get("Name"),
            "",
            item["name"],
            item["qty"],
            project,
            "Pending",
            ts,
            "", "", "0"
        ])

    return jsonify({"success": True, "request_id": request_id})

# ---------------- RETURN ITEM ----------------
@app.route("/return_item", methods=["POST"])
@login_required()
def return_item(user):
    try:
        data = request.get_json(force=True) or {}
        req_id = data.get("request_id")
        return_qty = int(data.get("return_qty", 0))

        if not req_id or return_qty <= 0:
            return jsonify({"error": "Invalid data"}), 400

        # SAFER: Use col_values to find all rows with this Request_ID
        col_a = requests_ws.col_values(1)  # All Request_IDs
        updates = []

        for idx, cell_value in enumerate(col_a):
            if str(cell_value).strip() == str(req_id):
                row = idx + 1  # +1 because list is 0-indexed, sheet is 1-indexed

                current_returned = int(requests_ws.cell(row, 12).value or "0")
                # Column L
                requested_qty = int(requests_ws.cell(row, 6).value or "0")                 # Column F

                new_returned = current_returned + return_qty
                new_status = "Returned" if new_returned >= requested_qty else "Partially Returned"

                updates.extend([
                    {"range": f"L{row}", "values": [[str(new_returned)]]},
                    {"range": f"H{row}", "values": [[new_status]]}
                ])

        if not updates:
            return jsonify({"error": "Request not found"}), 404

        requests_ws.batch_update({"valueInputOption": "RAW", "data": updates})
        return jsonify({"success": True})

    except Exception as e:
        print("RETURN ERROR:", str(e))
        return jsonify({"error": "Server error: " + str(e)}), 500
    

# ---------------- MANAGER DASHBOARD ----------------
@app.route("/manager")
@login_required("manager")
def manager_dashboard(user):
    requests = get_manager_requests()
    components = get_components()
    low_stock = [c for c in components if int(c.get("Current_Stock",0) or 0) <= int(c.get("Min_Stock",0) or 0)]
    return render_template("manager_dashboard.html", user=user, requests=requests, components=components, low_stock=low_stock)


@app.route("/approve/<req_id>")
@login_required("manager")
def approve_request(user, req_id):
    ok = update_request_status(req_id, "Approved", user.get("Name",""))
    flash(f"Request {req_id} approved!" if ok else "Failed to approve request", "success" if ok else "danger")
    return redirect(url_for("manager_dashboard"))


@app.route("/reject/<req_id>")
@login_required("manager")
def reject_request(user, req_id):
    ok = update_request_status(req_id, "Rejected", user.get("Name",""))
    flash(f"Request {req_id} rejected!" if ok else "Failed to reject request", "warning" if ok else "danger")
    return redirect(url_for("manager_dashboard"))


@app.route("/download_report")
@login_required("manager")
def download_report(user):
    requests_list = get_manager_requests()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    story.append(Paragraph("<b>Smart Inventory System – All Requests Report</b>", styles["Title"]))
    story.append(Paragraph(f"Generated by: {user.get('Name','')} • {datetime.now().strftime('%d %B %Y, %H:%M')}", styles["Normal"]))
    story.append(Spacer(1,20))

    data = [["Req ID","User","Component","Qty","Purpose","Status","Requested At","Approved By"]]
    for r in requests_list:
        data.append([
            r.get("Request_ID",""),
            r.get("User_Name",""),
            r.get("Component_Name",""),
            r.get("Qty_Requested",""),
            r.get("Purpose",""),
            r.get("Status",""),
            r.get("Requested_At",""),
            r.get("Approved_By","")
        ])

    table = Table(data)
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#6366f1")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("GRID",(0,0),(-1,-1),1,colors.black),
        ("BACKGROUND",(0,1),(-1,-1),colors.whitesmoke)
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Inventory_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", mimetype="application/pdf")


# ---------------- STOCK DASHBOARD ----------------
@app.route("/stock", methods=["GET","POST"])
@login_required("stock")
def stock_dashboard(user):
    if request.method == "POST":
        comp_id = request.form.get("component_id")
        qty = int(request.form.get("quantity",0))
        action = request.form.get("action")

        try:
            cell = components_ws.find(comp_id, in_column=1)
        except Exception:
            cell = None

        if cell:
            try:
                current = int(components_ws.cell(cell.row,4).value or 0)
            except Exception:
                current = 0

            new_qty = current + qty if action == "add" else max(0,current - qty)
            components_ws.update_cell(cell.row,4,new_qty)
            flash("Stock updated successfully!", "success")
        else:
            flash("Component not found", "danger")

    components = get_components()
    pending_issue = get_approved_requests()
    return render_template("stockincharge_dashboard.html", user=user, components=components, pending_issue=pending_issue)


# ---------------- ISSUE REQUEST ----------------
@app.route("/issue_request", methods=["POST"])
@login_required("stock")
def issue_request(user):
    data = request.get_json()
    request_id = data["request_id"]

    # Use the safe batch update function
    if not update_request_status(request_id, "Issued"):
        return jsonify({"error": "Failed to mark as Issued"}), 500

    # Deduct stock from Components sheet
    items_to_issue = [r for r in get_manager_requests() if r["Request_ID"] == request_id]
    for item in items_to_issue:
        cname = item["Component_Name"]
        qty = int(item["Qty_Requested"])
        try:
            cell = components_ws.find(cname, in_column=2)  # Search by Name (Column B)
            current = int(components_ws.cell(cell.row, 4).value or 0)
            components_ws.update_cell(cell.row, 4, max(0, current - qty))
        except:
            pass  # Skip if component not found

    return jsonify({"success": True, "message": "Issued successfully!"})


# ---------------- RUN APP ----------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
