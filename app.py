from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, jsonify
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from io import BytesIO
from dotenv import load_dotenv
import os

# PDF Generation (works perfectly on Windows)
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
    return components_ws.get_all_records()

def get_requests():
    return requests_ws.get_all_records()

def get_user(email):
    users = users_ws.get_all_records()
    return next((u for u in users if u.get("Email", "").lower() == email.lower()), None)

def update_request_status(request_id, new_status):
    try:
        # Find all rows with this Request_ID
        cell_list = requests_ws.findall(str(request_id))
        if not cell_list:
            return False

        updates = []
        for cell in cell_list:
            row = cell.row
            # Update Status (Column G)
            updates.append({
                "range": f"G{row}",
                "values": [[new_status]]
            })
            # Optional: Update timestamp (Column H)
            from datetime import datetime
            updates.append({
                "range": f"H{row}",
                "values": [[datetime.now().strftime("%Y-%m-%d %H:%M")]]
            })

        # Batch update — FAST & SAFE
        requests_ws.batch_update({"valueInputOption": "RAW", "data": updates})
        return True

    except Exception as e:
        print(f"Google Sheets Error: {e}")
        return False
    
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
            user_role = user.get("Role", "").lower()
            allowed = [role.lower()] if role else []
            if role == "stock":
                allowed.extend(["stock incharge", "stock engineer"])
            if role and user_role not in [a.lower() for a in allowed] and "manager" not in user_role:
                flash("Access denied.", "danger")
                return redirect(url_for('login'))
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
        role = user["Role"].lower()
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
            flash(f"Welcome back, {user['Name']}!", "success")
            return redirect(url_for('index'))
        flash("Invalid email or password", "danger")
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['Email'].strip().lower()
        password = request.form['Password']
        repassword = request.form.get('RePassword', '')

        # Check password match
        if password != repassword:
            flash("Passwords do not match!", "danger")
            return render_template('signup.html')

        # Check if email exists
        if get_user(email):
            flashback("Email already registered!", "danger")
            return render_template('signup.html')

        # Add new user
        users_ws.append_row([
            request.form['EmployeeID'],
            request.form['Name'],
            request.form['Designation'],
            request.form['Role'],
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
    my_requests = [r for r in get_requests() if r.get("User_Email", "").lower() == session['email'].lower()]
    return render_template('user_dashboard.html', user=user, components=components, requests=my_requests)

@app.route('/api/components')
def api_components():
    components = get_components()
    return jsonify([{
        "name": c["Name"],
        "tray": c.get("Location", "N/A"),
        "stock": int(c.get("Current_Stock", 0) or 0)
    } for c in components])

@app.route('/submit_request', methods=['POST'])
@login_required()
def submit_request(user):
    data = request.json
    project = data.get("project", "No Project Name").strip()
    items = data.get("items", [])

    if not items:
        return jsonify({"error": "No items selected"}), 400

    request_id = f"REQ{len(get_requests()) + 1:04d}"

    for item in items:
        requests_ws.append_row([
            request_id,
            session['email'],
            user["Name"],
            "",
            item["name"],
            item["qty"],
            project,
            "Pending",
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            "",
            ""
        ])

    return jsonify({"success": True, "request_id": request_id})

# ==================== MANAGER DASHBOARD ====================

@app.route('/manager')
@login_required("manager")
def manager_dashboard(user):
    requests = get_requests()
    components = get_components()

    low_stock = [
        c for c in components
        if int(c.get("Current_Stock", 0) or 0) <= int(c.get("Min_Stock", 0) or 0)
    ]

    return render_template(
        'manager_dashboard.html',
        user=user,
        requests=requests,
        components=components,
        low_stock=low_stock
    )


@app.route('/approve/<req_id>')
@login_required("manager")
def approve_request(user, req_id):
    if update_request_status(req_id, "Approved"):
        flash(f"Request {req_id} Approved Successfully!", "success")
    else:
        flash("Failed to update Google Sheets", "danger")

    return redirect(url_for('manager_dashboard'))


@app.route('/reject/<req_id>')
@login_required("manager")
def reject_request(user, req_id):
    cells = requests_ws.findall(req_id, in_column=1)

    for cell in cells:
        requests_ws.update(f'G{cell.row}', [[ "Rejected" ]])

    flash(f"Request {req_id} rejected", "warning")
    return redirect(url_for('manager_dashboard'))


@app.route('/download_report')
@login_required("manager")
def download_report(user):
    requests = get_requests()
    buffer = BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=letter)
    story = []
    styles = getSampleStyleSheet()

    story.append(Paragraph("<b>Smart Inventory System – All Requests Report</b>", styles['Title']))
    story.append(Paragraph(
        f"Generated by: {user['Name']} • {datetime.now().strftime('%d %B %Y, %H:%M')}",
        styles['Normal']
    ))
    story.append(Spacer(1, 20))

    data = [['Req ID', 'User', 'Component', 'Qty', 'Purpose', 'Status', 'Date']]

    for r in requests:
        data.append([
            r.get('Request_ID', ''),
            r.get('User_Name', ''),
            r.get('Component_Name', ''),
            r.get('Qty_Requested', ''),
            r.get('Purpose', ''),
            r.get('Status', ''),
            r.get('Requested_At', '')[:10]
        ])

    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors

    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6366f1')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
    ]))

    story.append(table)
    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"Inventory_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        mimetype='application/pdf'
    )

# ==================== STOCK INCHARGE ====================
@app.route('/stock', methods=['GET', 'POST'])
@login_required("stock")
def stock_dashboard(user):
    if request.method == 'POST':
        comp_id = request.form['component_id']
        qty = int(request.form['quantity'])
        action = request.form['action']
        cell = components_ws.find(comp_id, in_column=1)
        if cell:
            current = int(components_ws.cell(cell.row, 4).value or 0)
            new_qty = current + qty if action == "add" else max(0, current - qty)
            components_ws.update(f'D{cell.row}', new_qty)
            flash("Stock updated successfully!", "success")

    components = get_components()
    pending_issue = [r for r in get_requests() if r.get("Status") == "Approved"]
    return render_template('stockincharge_dashboard.html', user=user, components=components, pending_issue=pending_issue)

# ==================== RUN ====================
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
