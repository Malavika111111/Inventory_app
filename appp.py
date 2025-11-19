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
    return next((u for u in users if u.get("Email", "").lower() == (email or "").lower()), None)

# ---------------- UNIFIED & SAFE REQUEST PARSERS ----------------
def get_all_requests():
    try:
        return requests_ws.get_all_records()
    except Exception as e:
        print("Error fetching requests:", e)
        return []

def get_user_requests(user_email):
    rows = get_all_requests()
    return [r for r in rows if (r.get("User_Email", "") or "").lower() == user_email.lower()]

def get_manager_requests():
    return get_all_requests()  # Now unified and safe

def get_approved_requests():
    rows = get_all_requests()
    approved = []
    for r in rows:
        if str(r.get("Status", "")).strip().lower() == "approved":
            approved.append({
                "Request_ID": r.get("Request_ID", ""),
                "User_Name": r.get("User_Name", ""),
                "Purpose": r.get("Purpose", ""),
                "Component_Name": r.get("Component_Name", ""),
                "Qty_Requested": r.get("Qty_Requested", ""),
                "Requested_At": r.get("Requested_At", "")
            })
    return approved

# ---------------- STATUS UPDATE (Supports Issued & Returned) ----------------
def update_request_status(request_id, new_status, manager_name="", return_ts=False):
    try:
        cells = requests_ws.findall(str(request_id), in_column=1)  # Column A = Request_ID
        if not cells:
            return False

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        for cell in cells:
            row = cell.row
            requests_ws.update_cell(row, 8, new_status)  # Status

            if new_status == "Approved":
                requests_ws.update_cell(row, 9, timestamp)  # Requested_At → reuse as Approved_At
                if manager_name:
                    requests_ws.update_cell(row, 10, manager_name)

            if new_status == "Issued":
                requests_ws.update_cell(row, 9, timestamp)  # Issued At

            if new_status == "Returned":
                requests_ws.update_cell(row, 11, timestamp)  # Returned_At (Column K)

        return True
    except Exception as e:
        print("Update status error:", e)
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
                flash("Access denied: Insufficient permissions", "danger")
                return redirect(url_for('login'))

            return fn(user=user, *args, **kwargs)
        inner.__name__ = fn.__name__
        return inner
    return wrapper

# ---------------- ROUTES ----------------
@app.route("/")
def index():
    if "email" not in session:
        return redirect(url_for("login"))
    user = get_user(session["email"])
    if not user:
        return redirect(url_for("login"))

    role = (user.get("Role") or "").lower()
    if "manager" in role:
        return redirect(url_for("manager_dashboard"))
    if "stock" in role:
        return redirect(url_for("stock_dashboard"))
    return redirect(url_for("user_dashboard"))

# ---------------- AUTH ----------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        user = get_user(email)
        if user and user.get("Password") == password:
            session['email'] = email
            flash(f"Welcome back, {user.get('Name','User') }!", "success")
            return redirect(url_for('index'))
        flash("Invalid email or password", "danger")
    return render_template("login.html")

@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        repassword = request.form.get('RePassword', '')
        if password != repassword:
            flash("Passwords do not match!", "danger")
            return render_template("signup.html")
        if get_user(email):
            flash("Email already registered!", "danger")
            return render_template("signup.html")

        users_ws.append_row([
            request.form.get("EmployeeID", ""),
            request.form.get("Name", ""),
            request.form.get("Designation", ""),
            request.form.get("Role", "user"),
            email,
            password
        ])
        flash("Account created! Please login.", "success")
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
    my_requests = get_user_requests(user.get("Email"))
    return render_template("user_dashboard.html", user=user, components=components, requests=my_requests)

@app.route("/api/components")
def api_components():
    components = get_components()
    return jsonify([{
        "name": c.get("Name", ""),
        "tray": c.get("Location", "N/A"),
        "stock": int(c.get("Current_Stock", 0) or 0)
    } for c in components])

# ---------------- SUBMIT REQUEST (FIXED) ----------------
@app.route("/submit_request", methods=["POST"])
@login_required()
def submit_request(user):
    data = request.get_json(force=True)
    project = (data.get("project") or "Untitled Project").strip()
    items = data.get("items", [])

    if not items:
        return jsonify({"error": "No items selected"}), 400

    # Prevent duplicate pending request for same project
    existing = get_user_requests(user.get("Email"))
    if any(r["Purpose"].strip().lower() == project.lower() and r["Status"].lower() == "pending" for r in existing):
        return jsonify({"error": "You already have a pending request for this project!"}), 400

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    request_id = f"REQ{len(get_requests()) + 1:04d}"

    comp_dict = {c["Name"].strip().lower(): c for c in get_components()}

    for item in items:
        name = item.get("name", "").strip()
        qty = item.get("qty", 0)
        comp = comp_dict.get(name.lower(), {})
        requests_ws.append_row([
            request_id,
            user.get("Email"),
            user.get("Name"),
            comp.get("Component_ID", ""),
            comp.get("Name", name),
            qty,
            project,
            "Pending",
            ts,
            "",  # Approved_By
            ""   # Returned_At (Column K)
        ])

    return jsonify({"success": True, "request_id": request_id})

# ---------------- RETURN ITEM ----------------
@app.route("/return_item/<req_id>")
@login_required()
def return_item(user, req_id):   # <-- Added 'user' here
    user_email = session['email']
    user_requests = get_user_requests(user_email)

    # Allow return only if item is Issued and belongs to this user
    if any(r["Request_ID"] == req_id and r["Status"] == "Issued" for r in user_requests):
        if update_request_status(req_id, "Returned"):
            flash(f"Items from request {req_id} returned successfully!", "success")
        else:
            flash("Failed to mark as returned", "danger")
    else:
        flash("Invalid request or item already returned", "warning")

    return redirect(url_for("user_dashboard"))

# ---------------- MANAGER DASHBOARD ----------------
@app.route("/manager")
@login_required("manager")
def manager_dashboard(user):
    requests = get_manager_requests()
    components = get_components()
    low_stock = [c for c in components if int(c.get("Current_Stock", 0) or 0) <= int(c.get("Min_Stock", 0) or 0)]
    return render_template("manager_dashboard.html", user=user, requests=requests, components=components, low_stock=low_stock)

@app.route("/approve/<req_id>")
@login_required("manager")
def approve_request(user, req_id):
    success = update_request_status(req_id, "Approved", user.get("Name", ""))
    flash(f"Request {req_id} approved!" if success else "Failed to approve", "success" if success else "danger")
    return redirect(url_for("manager_dashboard"))

@app.route("/reject/<req_id>")
@login_required("manager")
def reject_request(user, req_id):
    success = update_request_status(req_id, "Rejected", user.get("Name", ""))
    flash(f"Request {req_id} rejected!" if success else "Failed to reject", "warning" if success else "danger")
    return redirect(url_for("manager_dashboard"))

# ---------------- DOWNLOAD REPORT ----------------
@app.route("/download_report")
@login_required("manager")
def download_report(user):
    requests_list = get_all_requests()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()

    story.append(Paragraph("Smart Inventory System – All Requests Report", styles["Title"]))
    story.append(Paragraph(f"Generated by: {user.get('Name','Manager')} • {datetime.now().strftime('%d %B %Y, %H:%M')}", styles["Normal"]))
    story.append(Spacer(1, 20))

    data = [["Req ID", "User", "Component", "Qty", "Purpose", "Status", "Requested", "Approved By", "Returned"]]
    for r in requests_list:
        data.append([
            r.get("Request_ID", ""),
            r.get("User_Name", ""),
            r.get("Component_Name", ""),
            r.get("Qty_Requested", ""),
            r.get("Purpose", ""),
            r.get("Status", "Pending"),
            r.get("Requested_At", "")[:16],
            r.get("Approved_By", ""),
            r.get("Returned_At", "")[:16] if r.get("Returned_At") else "-"
        ])

    table = Table(data, colWidths=[60, 70, 100, 40, 100, 70, 90, 80, 80])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#6366f1")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 1), (-1, -1), colors.whitesmoke),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (3, 1), (3, -1), "CENTER"),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True,
                     download_name=f"Inventory_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
                     mimetype="application/pdf")

# ---------------- STOCK DASHBOARD & ISSUE ----------------
@app.route("/stock", methods=["GET", "POST"])
@login_required("stock")
def stock_dashboard(user):
    if request.method == "POST":
        comp_id = request.form.get("component_id")
        qty = int(request.form.get("quantity", 0))
        action = request.form.get("action")

        try:
            cell = components_ws.find(str(comp_id), in_column=1)
            if cell:
                current = int(components_ws.cell(cell.row, 4).value or 0)
                new_qty = current + qty if action == "add" else max(0, current - qty)
                components_ws.update_cell(cell.row, 4, new_qty)
                flash("Stock updated!", "success")
            else:
                flash("Component not found", "danger")
        except Exception as e:
            flash("Error updating stock", "danger")

    components = get_components()
    pending_issue = get_approved_requests()
    return render_template("stockincharge_dashboard.html", user=user, components=components, pending_issue=pending_issue)

#------------ISSUE REQUEST-----------
@app.route("/issue_request", methods=["POST"])
def issue_request():
    data = request.get_json()
    request_id = data["request_id"]

    all_requests = requests_ws.get_all_records()
    items_to_issue = [r for r in all_requests if r["Request_ID"] == request_id]

    if not items_to_issue:
        return {"error":"Request not found"},404

    # Deduct stock
    components = components_ws.get_all_records()
    for item in items_to_issue:
        cname = item["Component_Name"]
        qty = int(item["Qty_Requested"])
        for idx, comp in enumerate(components, start=2):
            if comp["Name"] == cname:
                new_qty = int(comp["Current_Stock"]) - qty
                if new_qty < 0: new_qty = 0
                components_ws.update_cell(idx,4,new_qty)
                break

    # Mark issued
    for idx, row in enumerate(all_requests, start=2):
        if row["Request_ID"] == request_id:
            requests_ws.update_cell(idx,8,"Issued")

    return {"success":True,"message":"Stock deducted & request marked as issued!"}

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
