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

def update_component_stock(component_name, qty_to_add):
    """
    Updates stock by matching component name (case-insensitive).
    Adds qty_to_add back to Current_Stock.
    Returns True if updated, False if component not found.
    """
    components = components_ws.get_all_records()
    for i, row in enumerate(components, start=2):  # sheet rows start at 2 for data
        # defensive checks for keys existing
        name = row.get("Name", "")
        if name and name.strip().lower() == str(component_name).strip().lower():
            try:
                current_qty = int(row.get("Current_Stock", 0) or 0)
            except Exception:
                current_qty = 0
            try:
                add = int(qty_to_add)
            except Exception:
                add = 0
            new_qty = current_qty + add
            # Update Current Stock (column 4 expected to be Current_Stock)
            components_ws.update_cell(i, 4, new_qty)
            return True
    return False

def get_borrowed_items(user_email):
    """
    Return list of borrowed items for the user.
    Consider statuses: Approved, Issued, Partially Returned (these show as "borrowed")
    """
    rows = requests_ws.get_all_records()
    borrowed = []
    for r in rows:
        status = (r.get("Status") or "").strip()
        if (r.get("User_Email") or "").lower() == user_email.lower() and status in ["Approved", "Issued", "Partially Returned"]:
            borrowed.append({
                "Request_ID": r.get("Request_ID"),
                "Component_Name": r.get("Component_Name"),
                "Qty_Requested": int(r.get("Qty_Requested") or 0),
                "Purpose": r.get("Purpose"),
                "Requested_At": r.get("Requested_At"),
                "Status": status
            })
    return borrowed

# ---------------- REQUEST PARSERS ----------------

def get_user_requests(user_email):
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (req_id, email, uname, cid, cname, qty, purpose, status, req_at, appr_by) = (row + [""] * 10)[:10]
        if (email or "").lower() == user_email.lower():
            out.append({
                "Request_ID": req_id,
                "User_Email": email,
                "User_Name": uname,
                "Component_ID": cid,
                "Component_Name": cname,
                "Qty_Requested": qty,
                "Purpose": purpose,
                "Status": status,
                "Requested_At": req_at,
                "Approved_By": appr_by
            })
    return out


def get_manager_requests():
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (req_id, uemail, uname, cid, cname, qty, purpose, status, req_at, appr_by) = (row + [""] * 10)[:10]
        out.append({
            "Request_ID": req_id,
            "User_Email": uemail,
            "User_Name": uname,
            "Component_ID": cid,
            "Component_Name": cname,
            "Qty_Requested": qty,
            "Purpose": purpose,
            "Status": status,
            "Requested_At": req_at,
            "Approved_By": appr_by
        })
    return out


def get_approved_requests():
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (req_id, email, uname, cid, cname, qty, purpose, status, req_at, appr_by) = (row + [""] * 10)[:10]
        if (status or "").strip().lower() == "approved":
            out.append({
                "Request_ID": req_id,
                "User_Name": uname,
                "Purpose": purpose,
                "Component_Name": cname,
                "Qty_Requested": qty,
                "Requested_At": req_at
            })
    return out


def update_request_status(request_id, new_status, manager_name=""):
    try:
        cells = requests_ws.findall(str(request_id), in_column=1)
        if not cells:
            return False
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        for c in cells:
            row = c.row
            requests_ws.update_cell(row, 8, new_status)   # H = Status
            requests_ws.update_cell(row, 9, ts)          # I = Timestamp/Requested_At (used as last action time)
            requests_ws.update_cell(row, 10, manager_name) # J = Approved_By / metadata
        return True
    except Exception as e:
        print("Error updating:", e)
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
    my_requests = get_user_requests(user.get("Email"))
    borrowed_items = get_borrowed_items(user.get("Email"))
    return render_template("user_dashboard.html", user=user, components=components, requests=my_requests, borrowed=borrowed_items)

@app.route("/api/components")
def api_components():
    components = get_components()
    return jsonify([{
        "name": c.get("Name",""),
        "tray": c.get("Location","N/A"),
        "stock": int(c.get("Current_Stock",0) or 0)
    } for c in components])


# ---------------- SUBMIT REQUEST ----------------
@app.route("/submit_request", methods=["POST"])
@login_required()
def submit_request(user):
    data = request.get_json(force=True)  # force ensures JSON is parsed
    project = (data.get("project") or "No Project Name").strip()
    items = data.get("items", [])

    if not items:
        return jsonify({"error": "No items selected"}), 400

    # Check if a pending request for this project exists
    existing_requests = get_user_requests(user.get("Email"))
    for r in existing_requests:
        if (r.get("Purpose") or "").strip().lower() == project.lower() and (r.get("Status") or "").strip().lower() == "pending":
            return jsonify({"error": "Request for this project already submitted"}), 400

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    request_id = f"REQ{len(get_manager_requests()) + 1:04d}"

    for item in items:
        requests_ws.append_row([
            request_id,
            user.get("Email") or session.get("email"),
            user.get("Name"),
            "",  # Component ID if you have
            item.get("name",""),
            item.get("qty",""),
            project,
            "Pending",
            ts,
            ""  # Approved by
        ])

    return jsonify({"success": True, "request_id": request_id})


#---------------RETURN ITEM------------
@app.route("/return_item", methods=["POST"])
@login_required()
def return_item(user):
    data = request.get_json()
    request_id = data.get("request_id")
    component_name = data.get("component_name")
    try:
        return_qty = int(data.get("return_qty", 0))
    except Exception:
        return_qty = 0
    remarks = data.get("remarks", "")

    if not request_id or not component_name or return_qty <= 0:
        return {"error": "Invalid data"}, 400

    rows = requests_ws.get_all_records()
    for idx, row in enumerate(rows, start=2):
        if (row.get("Request_ID") == request_id) and (row.get("Component_Name") or "").strip().lower() == component_name.strip().lower():

            # ensure borrowed qty is int
            try:
                borrowed_qty = int(row.get("Qty_Requested") or 0)
            except Exception:
                borrowed_qty = 0

            ts = datetime.now().strftime("%Y-%m-%d %H:%M")

            # ===== CASE A: FULL RETURN =====
            if return_qty == borrowed_qty:
                # Update status -> Returned
                requests_ws.update_cell(idx, 8, "Returned")  # H = Status
                # Update timestamp (I)
                requests_ws.update_cell(idx, 9, ts)
                # Optionally annotate Approved_By (J) with return info (non-destructive)
                try:
                    prev = requests_ws.cell(idx, 10).value or ""
                    note = f"{prev} | Returned by {user.get('Name','')}"
                    requests_ws.update_cell(idx, 10, note)
                except Exception:
                    pass

                # Add stock back by component name
                update_component_stock(component_name, return_qty)

                return {"success": True, "message": f"Full return processed. {return_qty} items returned."}

            # ===== CASE B: PARTIAL RETURN =====
            elif return_qty < borrowed_qty:
                remaining_qty = borrowed_qty - return_qty

                # Update remaining qty (F) and status (H)
                requests_ws.update_cell(idx, 6, remaining_qty)  # F = Qty_Requested
                requests_ws.update_cell(idx, 8, "Partially Returned")  # H = Status
                requests_ws.update_cell(idx, 9, ts)

                # annotate Approved_By for audit (optional)
                try:
                    prev = requests_ws.cell(idx, 10).value or ""
                    note = f"{prev} | Partial return by {user.get('Name','')} ({return_qty})"
                    requests_ws.update_cell(idx, 10, note)
                except Exception:
                    pass

                # Add returned qty back to stock
                update_component_stock(component_name, return_qty)

                return {"success": True, "message": f"Partial return processed. Returned {return_qty}, Remaining {remaining_qty}."}

            else:
                return {"error": "Return quantity exceeds borrowed quantity!"}, 400

    return {"error": "Item not found"}, 404


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
        try:
            qty = int(item["Qty_Requested"])
        except Exception:
            qty = 0
        for idx, comp in enumerate(components, start=2):
            if comp.get("Name") and comp["Name"].strip().lower() == (cname or "").strip().lower():
                new_qty = int(comp.get("Current_Stock", 0) or 0) - qty
                if new_qty < 0: new_qty = 0
                components_ws.update_cell(idx,4,new_qty)
                break

    # Mark issued and update timestamp
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    for idx, row in enumerate(all_requests, start=2):
        if row["Request_ID"] == request_id:
            requests_ws.update_cell(idx,8,"Issued")
            requests_ws.update_cell(idx,9,ts)

    return {"success":True,"message":"Stock deducted & request marked as issued!"}


# ---------------- RUN APP ----------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
