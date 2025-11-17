from flask import Flask, render_template, session, request, redirect, url_for, flash, jsonify
from google.oauth2 import service_account
from werkzeug.security import generate_password_hash, check_password_hash
from googleapiclient.discovery import build
from dotenv import load_dotenv
import os
import json
from datetime import datetime
import re
import time
from googleapiclient.errors import HttpError

# -------------------- Load env ----------------
load_dotenv("get.env")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or "dev-secret"

# -------------------- Google Sheets ----------------
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# Ranges
RANGE_NAME = os.getenv("RANGE_NAME")               # Inventory!A:F
USER_RANGE_NAME = os.getenv("USER_RANGE_NAME")     # Employees!A:F
REQUESTS_RANGE_NAME = os.getenv("REQUESTS_RANGE_NAME") # Request!A:L

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
service = build('sheets', 'v4', credentials=creds)

# -----------------Valid roles---------------------
VALID_ROLES = ["user", "manager", "stock engineer"]

# ---------------- Helper Functions ----------------
def normalize_rows(headers, rows):
    normalized = []
    for row in rows:
        row = row + [""] * (len(headers) - len(row))
        normalized.append(row)
    return normalized

def get_sheet_values(spreadsheet_id, range_name, retries=3, delay=2):
    sheet = service.spreadsheets()
    for attempt in range(retries):
        try:
            result = sheet.values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
            values = result.get('values', [])
            return values
        except ConnectionResetError as e:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            else:
                raise e
        except HttpError as e:
            raise e
    return []

def write_append(spreadsheet_id, range_name, values):
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values}
    ).execute()

def write_update(spreadsheet_id, range_name, values):
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

# Users sheet helpers
def get_users_data():
    values = get_sheet_values(SPREADSHEET_ID, USER_RANGE_NAME)
    headers = values[0] if values else []
    rows = values[1:] if len(values) > 1 else []
    rows = normalize_rows(headers, rows)
    return headers, rows

def find_user(identifier):
    headers, rows = get_users_data()
    users = [dict(zip(headers, row)) for row in rows]
    idval = str(identifier).strip().lower()
    for u in users:
        email = (u.get("Email") or "").strip().lower()
        emp = (u.get("EmployeeID") or "").strip()
        if email == idval or emp == idval:
            return u
    return None

def add_user(emp_id, name, designation, role, email, password):
    hashed_password = generate_password_hash(password)
    values = [[emp_id, name, designation, role, email, hashed_password]]
    write_append(SPREADSHEET_ID, USER_RANGE_NAME, values)

# Inventory sheet helpers
def get_inventory_data():
    values = get_sheet_values(SPREADSHEET_ID, RANGE_NAME)
    headers = values[0] if values else []
    rows = values[1:] if len(values) > 1 else []
    rows = normalize_rows(headers, rows)
    return headers, rows

def find_inventory_row_by_component(component_name):
    headers, rows = get_inventory_data()
    for idx, row in enumerate(rows, start=2):
        comp = row[0] if len(row) > 0 else ""
        if comp.strip().lower() == component_name.strip().lower():
            return idx, row
    return None, None

def update_inventory_quantity(component_name, new_qty):
    found_idx, _ = find_inventory_row_by_component(component_name)
    if not found_idx:
        return False
    qty_col_letter = "C"  # Quantity Available
    range_name = f"Inventory!{qty_col_letter}{found_idx}:{qty_col_letter}{found_idx}"
    write_update(SPREADSHEET_ID, range_name, [[str(new_qty)]])
    return True

# ---------------- Requests ----------------
def get_requests_data():
    values = get_sheet_values(SPREADSHEET_ID, REQUESTS_RANGE_NAME)
    headers = values[0] if values else []
    rows = values[1:] if len(values) > 1 else []
    rows = normalize_rows(headers, rows)
    return headers, rows

def _parse_request_id(reqid):
    m = re.match(r"REQ0*([0-9]+)$", str(reqid).strip().upper())
    if not m:
        return None
    return int(m.group(1))

def get_next_request_id():
    headers, rows = get_requests_data()
    max_num = 0
    for r in rows:
        rid = r[0] if len(r) > 0 else ""
        n = _parse_request_id(rid)
        if n and n > max_num:
            max_num = n
    next_num = max_num + 1
    return f"REQ{next_num:04d}"

def append_request_rows(request_rows):
    write_append(SPREADSHEET_ID, REQUESTS_RANGE_NAME, request_rows)

def update_request_status_by_row(sheet_row_index, status, manager_name="", remarks=""):
    status_col = "K"  # Status
    remarks_col = "L"  # Remarks
    # Compose manager notes
    notes = f"{manager_name} - {remarks}" if remarks else manager_name
    # Update Status
    write_update(SPREADSHEET_ID, f"Request!{status_col}{sheet_row_index}", [[status]])
    # Update Remarks
    write_update(SPREADSHEET_ID, f"Request!{remarks_col}{sheet_row_index}", [[notes]])
    print(f"Updated row {sheet_row_index}: status={status}, remarks={notes}")
    return True

def find_request_rows_by_request_id(request_id):
    headers, rows = get_requests_data()
    matched = []
    request_id = str(request_id).strip().upper()
    # Find which column has the request ID
    try:
        rid_col_index = headers.index("RequestID")  # or "Request ID" based on your sheet
    except ValueError:
        print("RequestID column not found in sheet headers")
        return matched

    for idx, row in enumerate(rows, start=2):  # row 1 = headers
        rid = str(row[rid_col_index]).strip().upper() if len(row) > rid_col_index else ""
        if rid == request_id:
            matched.append((idx, row))
    print("Matched rows for", request_id, ":", matched)
    return matched


# -------------------- Routes --------------------
@app.route("/")
def home():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("Email", "").strip()
        password = request.form.get("Password", "")

        user = find_user(identifier)
        if not user:
            flash("No user found!", "danger")
            return redirect(url_for("login"))

        if not check_password_hash(user.get("Password", ""), password):
            flash("Incorrect password!", "danger")
            return redirect(url_for("login"))

        session["user_email"] = user.get("Email", "")
        session["user_name"] = user.get("Name", "")
        session["user_role"] = (user.get("Role") or "").strip().lower()
        session["user_designation"] = user.get("Designation", "")
        session["user_employee_id"] = user.get("EmployeeID", "")
        session["project_submitted"] = False
        session["project_name"] = ""

        flash("Login successful!", "success")

        role = session["user_role"]
        if role == "manager":
            return redirect(url_for("manager_dashboard"))
        elif role == "stock engineer":
            return redirect(url_for("stock_dashboard"))
        else:
            return redirect(url_for("dashboard"))

    return render_template("login.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        emp_id = request.form["EmployeeID"].strip()
        name = request.form["Name"].strip()
        designation = request.form["Designation"].strip()
        role = request.form["Role"].strip().lower()
        email = request.form["Email"].strip()
        password = request.form["Password"]
        re_password = request.form["RePassword"]

        if password != re_password:
            flash("Passwords do not match!", "danger")
            return redirect(url_for("signup"))

        if find_user(email) or find_user(emp_id):
            flash("User already exists!", "danger")
            return redirect(url_for("signup"))

        if role not in VALID_ROLES:
            flash("Invalid role. Use: user, manager or stock engineer", "danger")
            return redirect(url_for("signup"))

        add_user(emp_id, name, designation, role, email, password)
        flash("Signup successful! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")

@app.route("/dashboard")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))

    role = session.get("user_role", "")
    headers_inv, components = get_inventory_data()
    headers_req, requests_rows = get_requests_data()
    # convert requests to dict list using headers
    requests_dicts = []
    for r in requests_rows:
        d = {}
        for i, h in enumerate(headers_req):
            key = h
            d[key] = r[i] if i < len(r) else ""
        requests_dicts.append(d)

    return render_template(
        "dashboard.html",
        role=role,
        user=session.get("user_name", ""),
        designation=session.get("user_designation", ""),
        employee_id=session.get("user_employee_id", ""),
        project_submitted=session.get("project_submitted", False),
        project_name=session.get("project_name", ""),
        components=components,
        inv_headers=headers_inv,
        requests=requests_dicts,
        req_headers=headers_req
    )

@app.route("/user_dashboard", methods=["POST"])
def user_dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))

    if "projectName" in request.form:
        project_name = request.form.get("projectName","").strip()
        if not project_name:
            flash("Project name required","warning")
            return redirect(url_for("dashboard"))
        session["project_name"] = project_name
        session["project_submitted"] = True
        flash(f"Project '{project_name}' submitted.","success")
        return redirect(url_for("dashboard"))

    names = request.form.getlist("name[]")
    qtys = request.form.getlist("qty[]")
    returnables = request.form.getlist("returnable[]")
    return_dates = request.form.getlist("return_date[]")

    if not names:
        flash("No items in order!","warning")
        return redirect(url_for("dashboard"))

    req_id = get_next_request_id()
    request_rows = []
    for i,name in enumerate(names):
        try:
            qty = int(qtys[i])
        except ValueError:
            flash(f"Invalid quantity for {name}","danger")
            return redirect(url_for("dashboard"))

        returnable = returnables[i]
        return_date = return_dates[i] if returnable.strip().lower()=="yes" else "-"

        if returnable.lower()=="yes" and (not return_date or return_date.strip()==""):
            flash(f"Return date required for returnable item {name}","danger")
            return redirect(url_for("dashboard"))

        _, inv_row = find_inventory_row_by_component(name)
        tray = inv_row[3] if inv_row and len(inv_row)>3 else ""  # Tray Number

        row = [
            req_id,
            session.get("user_employee_id",""),
            session.get("user_name",""),
            session.get("user_designation",""),
            session.get("project_name",""),
            name,
            str(qty),
            returnable,
            return_date,
            tray,
            "Pending"
        ]
        request_rows.append(row)

    append_request_rows(request_rows)
    flash(f"Request {req_id} submitted and pending manager approval.","success")
    return redirect(url_for("dashboard"))

@app.route("/search_tray", methods=["POST"])
def search_tray():
    if request.is_json:
        tray_input_raw = request.json.get("trayNumber", "") or ""
    else:
        tray_input_raw = request.form.get("trayNumber", "") or ""
    tray_input_raw = str(tray_input_raw).strip()
    if not tray_input_raw:
        return jsonify({"components": []})
    tray_norm = tray_input_raw.upper().replace(" ", "")
    if tray_norm.isdigit():
        tray_norm = "T" + tray_norm
    elif not tray_norm.startswith("T"):
        tray_norm = "T" + tray_norm

    headers, components = get_inventory_data()
    tray_index = -1
    for i, h in enumerate(headers):
        if h.strip().lower() in ("tray number", "tray", "tray no", "traynumber"):
            tray_index = i
            break
    if tray_index == -1:
        return jsonify({"components": []})

    matched = []
    for row in components:
        cell = (row[tray_index] or "").strip().upper().replace(" ", "")
        if cell and cell == tray_norm:
            matched.append(row)
    return jsonify({"components": matched})

@app.route("/submit_tray_request", methods=["POST"])
def submit_tray_request():
    if "user_email" not in session:
        return redirect(url_for("login"))
    items = request.form.get("items", "[]")
    try:
        items = json.loads(items)
    except Exception:
        items = []
    if not items:
        flash("No tray items selected!", "warning")
        return redirect(url_for("dashboard"))

    req_id = get_next_request_id()
    request_rows = []
    for it in items:
        row = [
            req_id,
            session.get("user_employee_id", ""),
            session.get("user_name", ""),
            session.get("user_designation", ""),
            session.get("project_name", ""),
            it.get("name", ""),
            str(it.get("qty", "")),
            it.get("returnable", ""),
            it.get("return_date", "-"),
            it.get("tray", ""),
            "Pending",
            ""
        ]
        request_rows.append(row)

    append_request_rows(request_rows)
    flash(f"Tray Request {req_id} submitted!", "success")
    return redirect(url_for("dashboard"))


@app.route("/stock_update", methods=["POST"])
def stock_update():
    if "user_email" not in session:
        return redirect(url_for("login"))
    if session.get("user_role") != "stock engineer":
        flash("Unauthorized","danger")
        return redirect(url_for("dashboard"))

    component = request.form.get("component","").strip()
    description = request.form.get("description","").strip()
    returnable = request.form.get("returnable","").strip()
    qty = request.form.get("quantity","").strip()
    tray = request.form.get("tray","").strip()
    remarks = request.form.get("remarks","").strip()

    if not component:
        flash("Component required","warning")
        return redirect(url_for("dashboard"))

    inv_idx, inv_row = find_inventory_row_by_component(component)
    if inv_row:
        new_description = description or (inv_row[1] if len(inv_row)>1 else "")
        try:
            new_qty = int(qty) if qty!="" else int(inv_row[2] if len(inv_row)>2 and inv_row[2]!="" else 0)
        except ValueError:
            new_qty = int(inv_row[2] if len(inv_row)>2 and inv_row[2]!="" else 0)
        new_tray = tray or (inv_row[3] if len(inv_row)>3 else "")
        new_remarks = remarks or (inv_row[4] if len(inv_row)>4 else "")

        row_num = inv_idx
        write_update(SPREADSHEET_ID, f"Inventory!A{row_num}:E{row_num}", [[component, new_description, str(new_qty), new_tray, new_remarks]])
        flash(f"Component '{component}' updated.","success")
    else:
        write_append(SPREADSHEET_ID, RANGE_NAME, [[component, description, str(qty or "0"), tray, remarks]])
        flash(f"Component '{component}' added.","success")
    return redirect(url_for("dashboard"))
    
@app.route("/manager_dashboard")
def manager_dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))
    if session.get("user_role") != "manager":
        flash("Unauthorized", "danger")
        return redirect(url_for("dashboard"))

    headers, requests_rows = get_requests_data()
    requests_dicts = [dict(zip(headers, r)) for r in requests_rows]
    return render_template(
        "manager_dashboard.html",
        requests=requests_dicts,
        req_headers=headers,
        user_name=session.get("user_name")
    )

@app.route("/manager_action", methods=["POST"])
def manager_action():
    if "user_email" not in session:
        return jsonify({"success": False, "message": "Login required"}), 401
    if session.get("user_role") != "manager":
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    req_id = request.form.get("request_id", "").strip().upper()  # normalize
    action = request.form.get("action", "").strip().lower()
    remarks = request.form.get("remarks", "").strip()
    manager_name = session.get("user_name", "")

    matched = find_request_rows_by_request_id(req_id)
    if not matched:
        return jsonify({"success": False, "message": "Request not found"}), 404

    updated_row = None

    for sheet_row_idx, row in matched:
        # Extract component and quantity safely
        component_name = row[5] if len(row) > 5 else ""
        try:
            qty = int(row[6]) if len(row) > 6 and row[6] != "" else 0
        except ValueError:
            qty = 0

        if action == "approve":
            # Update inventory quantity
            inv_row_idx, inv_row = find_inventory_row_by_component(component_name)
            if inv_row_idx and inv_row:
                try:
                    current_qty = int(inv_row[2]) if len(inv_row) > 2 and inv_row[2] != "" else 0
                except ValueError:
                    current_qty = 0
                new_qty = max(0, current_qty - qty)
                update_inventory_quantity(component_name, new_qty)

            status = "Approved"
        else:
            status = "Denied"

        # Update the request sheet
        update_request_status_by_row(sheet_row_idx, status, manager_name, remarks)

        # Prepare updated row for front-end
        updated_row = {
            "RequestID": row[0],
            "Component": component_name,
            "Quantity": qty,
            "Status": status,
            "Remarks": f"{manager_name} - {remarks}" if remarks else manager_name
        }

    return jsonify({
        "success": True,
        "message": f"Request {req_id} {action.title()}d.",
        "updated_row": updated_row
    })


@app.route("/stock_dashboard")
def stock_dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))
    if session.get("user_role") != "stock engineer":
        flash("Unauthorized", "danger")
        return redirect(url_for("dashboard"))

    headers, components = get_inventory_data()
    return render_template(
        "stock_dashboard.html",
        components=components,
        inv_headers=headers,
        user_name=session.get("user_name")
    )

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
