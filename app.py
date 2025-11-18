# app.py - corrected version
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

# ==================== GOOGLE SHEETS SETUP ====================
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

# ==================== HELPERS ====================

def get_components():
    """Return list of components as dicts (works with Components sheet)."""
    return components_ws.get_all_records()

def get_requests():
    """
    Keep this for legacy use if needed (returns list of dicts).
    Prefer get_user_requests() / get_manager_requests() for requests since
    they parse rows in a stable column-order way.
    """
    return requests_ws.get_all_records()

def get_user(email):
    """Return user dict from Users sheet (case-insensitive email match)."""
    users = users_ws.get_all_records()
    return next((u for u in users if u.get("Email", "").lower() == (email or "").lower()), None)

# --- Requests helpers: read rows by explicit columns (A -> J)
# Expected columns in Requests sheet (A..J)
# A: Request_ID
# B: User_Email
# C: User_Name
# D: Component_ID
# E: Component_Name
# F: Qty_Requested
# G: Purpose
# H: Status
# I: Requested_At
# J: Approved_By

def get_user_requests(user_email):
    """Return list of request dicts for a particular user (reads A2:J)."""
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (request_id, email, user_name, component_id, component_name,
         qty_requested, purpose, status, requested_at, approved_by) = (row + [""] * 10)[:10]

        if (email or "").lower() == (user_email or "").lower():
            out.append({
                "Request_ID": request_id or "",
                "User_Email": email or "",
                "User_Name": user_name or "",
                "Component_ID": component_id or "",
                "Component_Name": component_name or "",
                "Qty_Requested": qty_requested or "",
                "Purpose": purpose or "",
                "Status": status or "",
                "Requested_At": requested_at or "",
                "Approved_By": approved_by or ""
            })
    return out

def get_manager_requests():
    """Return all requests (parsed row-by-row with explicit column mapping)."""
    rows = requests_ws.get("A2:J") or []
    out = []
    for row in rows:
        (request_id, user_email, user_name, component_id, component_name,
         qty_requested, purpose, status, requested_at, approved_by) = (row + [""] * 10)[:10]

        out.append({
            "Request_ID": request_id or "",
            "User_Email": user_email or "",
            "User_Name": user_name or "",
            "Component_ID": component_id or "",
            "Component_Name": component_name or "",
            "Qty_Requested": qty_requested or "",
            "Purpose": purpose or "",
            "Status": status or "",
            "Requested_At": requested_at or "",
            "Approved_By": approved_by or ""
        })
    return out

def get_approved_requests():
    """Return requests with Status == 'Approved' (used by stockincharge view)."""
    rows = requests_ws.get("A2:J") or []
    approved = []
    for row in rows:
        (request_id, user_email, user_name, component_id, component_name,
         qty_requested, purpose, status, requested_at, approved_by) = (row + [""] * 10)[:10]

        if (status or "").strip().lower() == "approved":
            approved.append({
                "Request_ID": request_id or "",
                "User_Email": user_email or "",
                "User_Name": user_name or "",
                "Component_ID": component_id or "",
                "Component_Name": component_name or "",
                "Qty_Requested": qty_requested or "",
                "Purpose": purpose or "",
                "Status": status or "",
                "Requested_At": requested_at or "",
                "Approved_By": approved_by or ""
            })
    return approved

def update_request_status(request_id, new_status, manager_name=""):
    """
    Update all rows with Request_ID == request_id:
    - Status column (H, column index 8)
    - Approved_At column (I, index 9) -> set current timestamp
    - Approved_By column (J, index 10) -> manager_name
    Use update_cell to avoid payload format issues.
    """
    try:
        cells = requests_ws.findall(str(request_id), in_column=1)
        if not cells:
            return False

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        for cell in cells:
            row = cell.row
            # H -> column 8; I -> 9; J -> 10
            # Use update_cell(row, col, value) to avoid complex payloads
            requests_ws.update_cell(row, 8, new_status)      # Status (H)
            requests_ws.update_cell(row, 9, ts)              # Approved_At (I)
            requests_ws.update_cell(row, 10, manager_name)   # Approved_By (J)
        return True
    except Exception as e:
        print("Error updating request status:", e)
        return False

# ==================== AUTH DECORATOR ====================
def login_required(role=None):
    def decorator(f):
        def wrapper(*args, **kwargs):
            if 'email' not in session:
                return redirect(url_for('login'))
            user = get_user(session['email'])
            if not user:
                session.pop('email', None)
                flash("Session expired. Please login again.", "danger")
                return redirect(url_for('login'))
            user_role = (user.get("Role") or "").lower()
            allowed = [role.lower()] if role else []
            if role == "stock":
                allowed.extend(["stock incharge", "stock engineer"])
            if role and user_role not in [a.lower() for a in allowed] and "manager" not in user_role:
                flash("Access denied.", "danger")
                return redirect(url_for('login'))
            # pass user into view as first arg (keeps view signature consistent)
            return f(user=user, *args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator

# ==================== ROUTES ====================

@app.route('/')
def index():
    if 'email' in session:
        user = get_user(session['email'])
        if not user:
            return redirect(url_for('login'))
        role = (user.get("Role") or "").lower()
        if "manager" in role:
            return redirect(url_for('manager_dashboard'))
        elif "stock" in role:
            return redirect(url_for('stock_dashboard'))
        else:
            return redirect(url_for('user_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
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
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        repassword = request.form.get('RePassword', '')
        if password != repassword:
            flash("Passwords do not match!", "danger")
            return render_template('signup.html')
        if get_user(email):
            flash("Email already registered!", "danger")
            return render_template('signup.html')
        # Append row in Users sheet: adjust order to match your Users header
        users_ws.append_row([
            request.form.get('EmployeeID',''),
            request.form.get('Name',''),
            request.form.get('Designation',''),
            request.form.get('Role',''),
            email,
            password
        ])
        flash("Account created successfully! Please login.", "success")
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/logout')
def logout():
    session.pop('email', None)
    flash("Logged out successfully", "info")
    return redirect(url_for('login'))

# ==================== USER DASHBOARD ====================
@app.route('/user')
@login_required()
def user_dashboard(user):
    components = get_components()
    my_requests = get_user_requests(user.get("Email") or session.get('email'))
    # pass requests as list-of-dicts with the keys your templates expect
    return render_template('user_dashboard.html', user=user, components=components, requests=my_requests)

@app.route('/api/components')
def api_components():
    components = get_components()
    # frontend expects fields: name, tray, stock
    return jsonify([{
        "name": c.get("Name",""),
        "tray": c.get("Location","N/A"),
        "stock": int(c.get("Current_Stock", 0) or 0)
    } for c in components])

@app.route('/submit_request', methods=['POST'])
@login_required()
def submit_request(user):
    data = request.json or {}
    project = (data.get("project") or "No Project Name").strip()
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items selected"}), 400
    # create request id
    request_id = f"REQ{len(get_manager_requests()) + 1:04d}"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    for item in items:
        # append row with the expected A..J columns (we put "" for Component_ID if not used)
        requests_ws.append_row([
            request_id,
            user.get("Email") or session.get("email"),
            user.get("Name"),
            "",                     # Component_ID (optional)
            item.get("name", ""),   # Component_Name
            item.get("qty", ""),    # Qty_Requested
            project,                # Purpose
            "Pending",              # Status
            ts,                     # Requested_At
            ""                      # Approved_By
        ])
    return jsonify({"success": True, "request_id": request_id})

# ==================== MANAGER DASHBOARD ====================
@app.route('/manager')
@login_required("manager")
def manager_dashboard(user):
    # use get_manager_requests() so columns are correct
    requests = get_manager_requests()
    components = get_components()
    low_stock = [
        c for c in components
        if int(c.get("Current_Stock", 0) or 0) <= int(c.get("Min_Stock", 0) or 0)
    ]
    return render_template('manager_dashboard.html', user=user, requests=requests, components=components, low_stock=low_stock)

@app.route('/approve/<req_id>')
@login_required("manager")
def approve_request(user, req_id):
    ok = update_request_status(req_id, "Approved", user.get("Name",""))
    if ok:
        flash(f"Request {req_id} approved!", "success")
    else:
        flash("Failed to update Google Sheet", "danger")
    return redirect(url_for('manager_dashboard'))

@app.route('/reject/<req_id>')
@login_required("manager")
def reject_request(user, req_id):
    ok = update_request_status(req_id, "Rejected", user.get("Name",""))
    if ok:
        flash(f"Request {req_id} rejected!", "warning")
    else:
        flash("Failed to update Google Sheet", "danger")
    return redirect(url_for('manager_dashboard'))

@app.route('/download_report')
@login_required("manager")
def download_report(user):
    requests_list = get_manager_requests()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()
    story.append(Paragraph("<b>Smart Inventory System – All Requests Report</b>", styles['Title']))
    story.append(Paragraph(f"Generated by: {user.get('Name','')} • {datetime.now().strftime('%d %B %Y, %H:%M')}", styles['Normal']))
    story.append(Spacer(1, 20))

    data = [['Req ID', 'User', 'Component', 'Qty', 'Purpose', 'Status', 'Requested At', 'Approved By']]
    for r in requests_list:
        data.append([
            r.get('Request_ID',''),
            r.get('User_Name',''),
            r.get('Component_Name',''),
            r.get('Qty_Requested',''),
            r.get('Purpose',''),
            r.get('Status',''),
            r.get('Requested_At',''),
            r.get('Approved_By','')
        ])

    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#6366f1')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('BACKGROUND', (0,1), (-1,-1), colors.whitesmoke),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"Inventory_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", mimetype='application/pdf')

# ==================== STOCK INCHARGE DASHBOARD ====================
@app.route('/stock', methods=['GET', 'POST'])
@login_required("stock")
def stock_dashboard(user):
    # if POST, update stock (form from stock management tab)
    if request.method == 'POST':
        comp_id = request.form.get('component_id')
        qty = int(request.form.get('quantity', 0))
        action = request.form.get('action')
        # find by ID in Components sheet (assuming first column is ID)
        try:
            cell = components_ws.find(comp_id, in_column=1)
        except Exception:
            cell = None
        if cell:
            try:
                current = int(components_ws.cell(cell.row, 4).value or 0)  # assuming 4th col is Current_Stock
            except Exception:
                current = 0
            new_qty = current + qty if action == "add" else max(0, current - qty)
            components_ws.update_cell(cell.row, 4, new_qty)
            flash("Stock updated successfully!", "success")
        else:
            flash("Component not found", "danger")

    components = get_components()
    # pending_issue are manager-approved requests (not yet marked issued)
    pending_issue = get_approved_requests()
    return render_template('stockincharge_dashboard.html', user=user, components=components, pending_issue=pending_issue)

# ==================== RUN ====================
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
