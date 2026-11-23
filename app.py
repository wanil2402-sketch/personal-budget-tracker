
from flask import Flask, render_template, request, session, redirect, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from mysql.connector import IntegrityError
from db import get_db_connection
from dotenv import load_dotenv
import os
import secrets
import smtplib
from email.message import EmailMessage
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import calendar
from datetime import date, datetime, timedelta

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")


def generate_otp():
    return f"{secrets.randbelow(1000000):06d}"


def send_otp_email(to_email, otp):
    send_email(
        to_email,
        "Your Login Verification Code",
        f"Your verification code is {otp}. It expires in 5 minutes. "
        "Do not share this code with anyone."
    )


def generate_reset_token(email):
    serializer = URLSafeTimedSerializer(app.secret_key)
    return serializer.dumps(email, salt="password-reset")


def verify_reset_token(token):
    serializer = URLSafeTimedSerializer(app.secret_key)
    try:
        return serializer.loads(
            token,
            salt="password-reset",
            max_age=900
        )
    except (BadSignature, SignatureExpired):
        return None


def send_email(to_email, subject, body):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = app.config["MAIL_USERNAME"]
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
        server.starttls()
        server.login(
            app.config["MAIL_USERNAME"],
            app.config["MAIL_PASSWORD"]
        )
        server.send_message(message)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form["full_name"].strip()
        email = request.form["email"].strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if not full_name or not email:
            return "Name and email are required."

        if len(password) < 8:
            return "Password must contain at least 8 characters."

        if password != confirm_password:
            return "Passwords do not match. Please try again."

        password_hash = generate_password_hash(
            password, method="pbkdf2:sha256"
        )

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            query = """
                INSERT INTO Users (full_name, email, password_hash)
                VALUES (%s, %s, %s)
            """
            cursor.execute(query, (full_name, email, password_hash))
            connection.commit()

            session["user_id"] = cursor.lastrowid
            session["full_name"] = full_name
            return redirect(url_for("financial_setup"))

        except IntegrityError:
            return "This email is already registered."

        except Exception as e:
            print("REGISTRATION ERROR:", repr(e), flush=True)
            if connection:
                connection.rollback()
            return "Something went wrong. Please try again."

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("register.html")



@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute(
                "SELECT user_id, email FROM Users WHERE email = %s",
                (email,)
            )
            user = cursor.fetchone()

            if user:
                token = generate_reset_token(user["email"])
                reset_link = url_for(
                    "reset_password",
                    token=token,
                    _external=True
                )

                send_email(
                    user["email"],
                    "Password Reset - Personal Budget Tracker",
                    f"Click this link to reset your password. "
                    f"This link expires in 15 minutes:\n\n{reset_link}"
                )

            flash("If an account exists with that email, a reset link has been sent.")

        except Exception:
            app.logger.exception("Password reset request failed")
            flash("Unable to process your request right now. Please try again later.")

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    email = verify_reset_token(token)

    if not email:
        flash("This password reset link is invalid or expired.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if len(password) < 8:
            flash("Password must contain at least 8 characters.")
            return render_template("reset_password.html")

        if password != confirm_password:
            flash("Passwords do not match.")
            return render_template("reset_password.html")

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor()

            password_hash = generate_password_hash(
                password, method="pbkdf2:sha256"
            )

            cursor.execute(
                "UPDATE Users SET password_hash = %s WHERE email = %s",
                (password_hash, email)
            )
            connection.commit()

            if cursor.rowcount == 0:
                flash("Unable to reset password. Please request a new link.")
                return redirect(url_for("forgot_password"))

            flash("Your password has been updated. Please log in.")
            return redirect(url_for("login"))

        except Exception:
            if connection:
                connection.rollback()
            app.logger.exception("Password reset failed")
            flash("Unable to update your password. Please try again.")

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("reset_password.html")


def send_login_alert(email, full_name):
    try:
        send_email(
            email,
            "New Login Alert - Personal Budget Tracker",
            f"Hello {full_name},\\n\\n"
            "A successful login to your Personal Budget Tracker account "
            "was detected. If this was not you, please reset your password."
        )
    except Exception:
        app.logger.exception("Login notification email could not be sent.")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        ip_address = request.remote_addr or "unknown"

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT locked_until
                FROM Login_Attempts
                WHERE email = %s AND ip_address = %s
                AND locked_until > NOW()
            """, (email, ip_address))

            if cursor.fetchone():
                return "Too many failed attempts. Please try again after 15 minutes."

            cursor.execute("""
                SELECT user_id, full_name, password_hash
                FROM Users
                WHERE email = %s
            """, (email,))

            user = cursor.fetchone()

            if user and user["password_hash"] and check_password_hash(
                user["password_hash"], password
            ):
                cursor.execute(
                    "DELETE FROM Login_Attempts WHERE email = %s AND ip_address = %s",
                    (email, ip_address)
                )

                cursor.execute("""
                    SELECT enabled
                    FROM Two_Factor_Settings
                    WHERE user_id = %s
                """, (user["user_id"],))

                two_factor = cursor.fetchone()
                connection.commit()

                if two_factor and two_factor["enabled"]:
                    otp = generate_otp()
                    otp_hash = generate_password_hash(otp, method="pbkdf2:sha256")

                    cursor.execute("""
                        INSERT INTO Login_OTPs
                            (user_id, otp_hash, expires_at, attempts)
                        VALUES (%s, %s, DATE_ADD(NOW(), INTERVAL 5 MINUTE), 0)
                        ON DUPLICATE KEY UPDATE
                            otp_hash = VALUES(otp_hash),
                            expires_at = VALUES(expires_at),
                            attempts = 0
                    """, (user["user_id"], otp_hash))

                    connection.commit()

                    session.clear()
                    session["pending_2fa_user_id"] = user["user_id"]
                    session["pending_2fa_email"] = email
                    session["pending_2fa_name"] = user["full_name"]

                    try:
                        send_otp_email(email, otp)
                    except Exception:
                        cursor.execute(
                            "DELETE FROM Login_OTPs WHERE user_id = %s",
                            (user["user_id"],)
                        )
                        connection.commit()
                        session.clear()
                        app.logger.exception("OTP email could not be sent.")
                        flash("Unable to send verification code. Please try again.")
                        return redirect(url_for("login"))

                    return redirect(url_for("verify_otp"))

                session.clear()
                session["user_id"] = user["user_id"]
                session["full_name"] = user["full_name"]
                send_login_alert(email, user["full_name"])
                return redirect(url_for("dashboard"))

            cursor.execute("""
                INSERT INTO Login_Attempts
                    (email, ip_address, failed_attempts, locked_until)
                VALUES (%s, %s, 1, NULL)
                ON DUPLICATE KEY UPDATE
                    failed_attempts = CASE
                        WHEN locked_until IS NOT NULL AND locked_until <= NOW()
                        THEN 1
                        ELSE failed_attempts + 1
                    END,
                    locked_until = CASE
                        WHEN locked_until IS NOT NULL AND locked_until <= NOW()
                        THEN NULL
                        WHEN failed_attempts >= 3
                        THEN DATE_ADD(NOW(), INTERVAL 15 MINUTE)
                        ELSE locked_until
                    END
            """, (email, ip_address))

            connection.commit()
            return "Invalid email or password."

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("login.html")


@app.route("/verify-otp", methods=["GET", "POST"])
def verify_otp():
    if "pending_2fa_user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        entered_otp = request.form.get("otp", "").strip()
        user_id = session["pending_2fa_user_id"]

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT otp_hash, attempts
                FROM Login_OTPs
                WHERE user_id = %s AND expires_at > NOW()
            """, (user_id,))

            otp_record = cursor.fetchone()

            if not otp_record:
                cursor.execute(
                    "DELETE FROM Login_OTPs WHERE user_id = %s",
                    (user_id,)
                )
                connection.commit()
                session.clear()
                flash("Verification code expired. Please log in again.")
                return redirect(url_for("login"))

            if not check_password_hash(otp_record["otp_hash"], entered_otp):
                attempts = otp_record["attempts"] + 1

                if attempts >= 3:
                    cursor.execute(
                        "DELETE FROM Login_OTPs WHERE user_id = %s",
                        (user_id,)
                    )
                    connection.commit()
                    session.clear()
                    flash("Too many incorrect codes. Please log in again.")
                    return redirect(url_for("login"))

                cursor.execute("""
                    UPDATE Login_OTPs
                    SET attempts = %s
                    WHERE user_id = %s
                """, (attempts, user_id))
                connection.commit()
                flash("Incorrect verification code. Please try again.")
                return redirect(url_for("verify_otp"))

            cursor.execute(
                "DELETE FROM Login_OTPs WHERE user_id = %s",
                (user_id,)
            )
            connection.commit()

            email = session["pending_2fa_email"]
            full_name = session["pending_2fa_name"]

            session.clear()
            session["user_id"] = user_id
            session["full_name"] = full_name

            send_login_alert(email, full_name)
            return redirect(url_for("dashboard"))

        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("verify_otp.html")



@app.route("/security-settings")
def security_settings():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute(
            "SELECT enabled FROM Two_Factor_Settings WHERE user_id = %s",
            (session["user_id"],)
        )
        setting = cursor.fetchone()
        enabled = bool(setting and setting["enabled"])

        return render_template("security_settings.html", enabled=enabled)
    finally:
        cursor.close()
        connection.close()


@app.route("/toggle-2fa", methods=["POST"])
def toggle_2fa():
    if "user_id" not in session:
        return redirect(url_for("login"))

    action = request.form.get("action")

    if action not in ("enable", "disable"):
        flash("Invalid security setting.")
        return redirect(url_for("security_settings"))

    enabled = action == "enable"
    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT INTO Two_Factor_Settings (user_id, enabled)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE enabled = VALUES(enabled)
        """, (session["user_id"], enabled))

        if not enabled:
            cursor.execute(
                "DELETE FROM Login_OTPs WHERE user_id = %s",
                (session["user_id"],)
            )

        connection.commit()
        flash("Two-factor authentication " + ("enabled." if enabled else "disabled."))
    except Exception:
        connection.rollback()
        app.logger.exception("Unable to update two-factor settings.")
        flash("Unable to update your security settings.")
    finally:
        cursor.close()
        connection.close()

    return redirect(url_for("security_settings"))


@app.route("/financial-setup", methods=["GET", "POST"])
def financial_setup():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT category_id, category_name
            FROM Categories
            WHERE category_type = 'Expense'
            ORDER BY category_name
        """)
        categories = cursor.fetchall()

        if request.method == "POST":
            try:
                expected_income = float(
                    request.form.get("expected_income", 0)
                )
                if expected_income <= 0:
                    return "Please enter a valid monthly income."

                cursor.execute("""
                    INSERT INTO User_Financial_Settings
                    (user_id, expected_monthly_income)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE
                    expected_monthly_income = VALUES(expected_monthly_income)
                """, (session["user_id"], expected_income))

                for category in categories:
                    field_name = f"budget_{category['category_id']}"
                    amount_text = request.form.get(field_name, "").strip()

                    if amount_text:
                        amount = float(amount_text)
                        if amount < 0:
                            return "Budget amounts cannot be negative."

                        if amount > 0:
                            cursor.execute("""
                                INSERT INTO Personal_Budgets
                                (user_id, category_id, budget_amount)
                                VALUES (%s, %s, %s)
                                ON DUPLICATE KEY UPDATE
                                budget_amount = VALUES(budget_amount)
                            """, (
                                session["user_id"],
                                category["category_id"],
                                amount
                            ))

                connection.commit()
                return redirect(url_for("dashboard"))

            except ValueError:
                connection.rollback()
                return "Please enter valid numbers for income and budgets."

        return render_template(
            "financial_setup.html",
            full_name=session["full_name"],
            categories=categories
        )

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT c.category_type,
                   COALESCE(SUM(t.amount), 0) AS total
            FROM Transactions t
            JOIN Categories c
                ON t.category_id = c.category_id
            WHERE t.user_id = %s
            GROUP BY c.category_type
        """, (session["user_id"],))

        rows = cursor.fetchall()

        totals = {
            row["category_type"]: float(row["total"])
            for row in rows
        }

        income = totals.get("Income", 0)
        expenses = totals.get("Expense", 0)
        balance = income - expenses

        cursor.execute(
            "SELECT expected_monthly_income FROM User_Financial_Settings WHERE user_id = %s",
            (session["user_id"],)
        )
        settings = cursor.fetchone()
        expected_income = float(settings["expected_monthly_income"]) if settings else 0

        cursor.execute(
            "SELECT COALESCE(SUM(budget_amount), 0) AS total_budget FROM Personal_Budgets WHERE user_id = %s",
            (session["user_id"],)
        )
        budget_row = cursor.fetchone()
        expected_expenses = float(budget_row["total_budget"])

        cursor.execute(
            "SELECT created_at FROM Users WHERE user_id = %s",
            (session["user_id"],)
        )
        user_record = cursor.fetchone()
        registration_date = user_record["created_at"].date()
        today = date.today()

        def dashboard_anniversary(year, month):
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(registration_date.day, last_day))

        cycle_start = dashboard_anniversary(today.year, today.month)

        if today < cycle_start:
            if today.month == 1:
                cycle_start = dashboard_anniversary(today.year - 1, 12)
            else:
                cycle_start = dashboard_anniversary(today.year, today.month - 1)

        if cycle_start.month == 12:
            next_cycle_start = dashboard_anniversary(cycle_start.year + 1, 1)
        else:
            next_cycle_start = dashboard_anniversary(
                cycle_start.year, cycle_start.month + 1
            )

        cursor.execute("""
            SELECT c.category_name,
                   COALESCE(SUM(t.amount), 0) AS total
            FROM Transactions t
            JOIN Categories c
                ON t.category_id = c.category_id
            WHERE t.user_id = %s
              AND c.category_type = 'Expense'
            GROUP BY c.category_id, c.category_name
            ORDER BY total DESC
        """, (session["user_id"],))
        expense_categories = cursor.fetchall()

        cursor.execute("""
            SELECT c.category_name,
                   pb.budget_amount,
                   COALESCE(SUM(t.amount), 0) AS actual
            FROM Personal_Budgets pb
            JOIN Categories c
                ON pb.category_id = c.category_id
            LEFT JOIN Transactions t
                ON t.user_id = pb.user_id
                AND t.category_id = pb.category_id
                AND t.transaction_date >= %s
                AND t.transaction_date < %s
            WHERE pb.user_id = %s
              AND c.category_type = 'Expense'
            GROUP BY pb.budget_id, c.category_id,
                     c.category_name, pb.budget_amount
            ORDER BY c.category_name
        """, (cycle_start, next_cycle_start, session["user_id"]))
        budget_categories = cursor.fetchall()

        print("CHART DEBUG:", session["user_id"], expense_categories)
        print("BUDGET CHART DEBUG:", budget_categories)

        return render_template(
            "dashboard.html",
            full_name=session["full_name"],
            income=income,
            expenses=expenses,
            balance=balance,
            expected_income=expected_income,
            expected_expenses=expected_expenses,
            expense_categories=expense_categories,
            budget_categories=budget_categories
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()



@app.route("/add-transaction", methods=["GET", "POST"])
def add_transaction():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        if request.method == "POST":
            category_id = request.form["category_id"]
            method_id = request.form["method_id"]
            amount = float(request.form["amount"])
            transaction_date = date.fromisoformat(
                request.form["transaction_date"]
            )
            description = request.form["description"].strip()
            user_id = session["user_id"]

            if amount <= 0:
                return "Amount must be greater than zero."

            cursor.execute(
                "SELECT created_at FROM Users WHERE user_id = %s",
                (user_id,)
            )
            user = cursor.fetchone()

            if not user:
                return "User account not found."

            registration_date = user["created_at"].date()

            def anniversary(year, month):
                last_day = calendar.monthrange(year, month)[1]
                return date(year, month, min(registration_date.day, last_day))

            cycle_start = anniversary(
                transaction_date.year, transaction_date.month
            )

            if transaction_date < cycle_start:
                if transaction_date.month == 1:
                    previous_year = transaction_date.year - 1
                    previous_month = 12
                else:
                    previous_year = transaction_date.year
                    previous_month = transaction_date.month - 1

                cycle_start = anniversary(previous_year, previous_month)

            if cycle_start.month == 12:
                next_cycle_start = anniversary(cycle_start.year + 1, 1)
            else:
                next_cycle_start = anniversary(
                    cycle_start.year, cycle_start.month + 1
                )

            cursor.execute("""
                INSERT INTO Transactions
                (user_id, category_id, method_id, amount,
                 transaction_date, description)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                user_id, category_id, method_id, amount,
                transaction_date, description
            ))

            cursor.execute("""
                SELECT category_name, category_type
                FROM Categories
                WHERE category_id = %s
            """, (category_id,))
            category = cursor.fetchone()

            if category and category["category_type"] == "Expense":
                cursor.execute("""
                    SELECT budget_amount
                    FROM Personal_Budgets
                    WHERE user_id = %s AND category_id = %s
                """, (user_id, category_id))
                budget = cursor.fetchone()

                if budget:
                    cursor.execute("""
                        SELECT COALESCE(SUM(amount), 0) AS total_spent
                        FROM Transactions
                        WHERE user_id = %s
                          AND category_id = %s
                          AND transaction_date >= %s
                          AND transaction_date < %s
                    """, (
                        user_id, category_id, cycle_start, next_cycle_start
                    ))

                    total_spent = float(cursor.fetchone()["total_spent"])
                    budget_amount = float(budget["budget_amount"])

                    if total_spent > budget_amount:
                        excess = total_spent - budget_amount
                        flash(
                            f'Budget alert: {category["category_name"]} '
                            f'spending is ₹{total_spent:.2f}, '
                            f'which is ₹{excess:.2f} over your '
                            f'₹{budget_amount:.2f} budget.',
                            "warning"
                        )

            connection.commit()
            return redirect(url_for("dashboard"))

        cursor.execute("""
            SELECT category_id, category_name, category_type
            FROM Categories
            ORDER BY category_type, category_name
        """)
        categories = cursor.fetchall()

        cursor.execute("""
            SELECT method_id, method_name
            FROM Payment_Methods
            ORDER BY method_name
        """)
        methods = cursor.fetchall()

        return render_template(
            "add_transaction.html",
            categories=categories,
            methods=methods
        )

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/transactions")
def transactions():
    if "user_id" not in session:
        return redirect(url_for("login"))

    search = request.args.get("search", "").strip()
    transaction_type = request.args.get("type", "").strip()
    category_id = request.args.get("category", "").strip()
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT category_id, category_name
            FROM Categories
            ORDER BY category_name
        """)
        categories = cursor.fetchall()

        conditions = ["t.user_id = %s"]
        params = [session["user_id"]]

        if search:
            conditions.append("""
                (c.category_name LIKE %s
                OR p.method_name LIKE %s
                OR t.description LIKE %s)
            """)
            search_value = "%" + search + "%"
            params.extend([search_value, search_value, search_value])

        if transaction_type in ("Income", "Expense"):
            conditions.append("c.category_type = %s")
            params.append(transaction_type)

        if category_id.isdigit():
            conditions.append("t.category_id = %s")
            params.append(int(category_id))

        if from_date:
            conditions.append("t.transaction_date >= %s")
            params.append(from_date)

        if to_date:
            conditions.append("t.transaction_date <= %s")
            params.append(to_date)

        query = """
            SELECT
                t.transaction_id,
                t.transaction_date,
                c.category_name,
                c.category_type,
                t.amount,
                p.method_name,
                t.description
            FROM Transactions t
            JOIN Categories c ON t.category_id = c.category_id
            JOIN Payment_Methods p ON t.method_id = p.method_id
            WHERE """ + " AND ".join(conditions) + """
            ORDER BY t.transaction_date DESC
        """

        cursor.execute(query, tuple(params))
        transactions = cursor.fetchall()

        return render_template(
            "transactions.html",
            transactions=transactions,
            categories=categories,
            search=search,
            selected_type=transaction_type,
            selected_category=category_id,
            from_date=from_date,
            to_date=to_date
        )

    finally:
        cursor.close()
        connection.close()


@app.route("/edit-transaction/<int:transaction_id>", methods=["GET", "POST"])
def edit_transaction(transaction_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT *
            FROM Transactions
            WHERE transaction_id = %s AND user_id = %s
        """, (transaction_id, session["user_id"]))

        transaction = cursor.fetchone()

        if not transaction:
            return "Transaction not found."

        if request.method == "POST":
            category_id = int(request.form["category_id"])
            method_id = int(request.form["method_id"])
            amount = float(request.form["amount"])
            transaction_date = date.fromisoformat(
                request.form["transaction_date"]
            )
            description = request.form.get("description", "").strip()

            if amount <= 0:
                return "Amount must be greater than zero."

            cursor.execute("""
                SELECT category_id
                FROM Categories
                WHERE category_id = %s
            """, (category_id,))
            if not cursor.fetchone():
                return "Invalid category."

            cursor.execute("""
                SELECT method_id
                FROM Payment_Methods
                WHERE method_id = %s
            """, (method_id,))
            if not cursor.fetchone():
                return "Invalid payment method."

            cursor.execute("""
                UPDATE Transactions
                SET category_id = %s,
                    method_id = %s,
                    amount = %s,
                    transaction_date = %s,
                    description = %s
                WHERE transaction_id = %s AND user_id = %s
            """, (
                category_id, method_id, amount,
                transaction_date, description,
                transaction_id, session["user_id"]
            ))

            connection.commit()
            flash("Transaction updated successfully!", "success")
            return redirect(url_for("transactions"))

        cursor.execute("""
            SELECT category_id, category_name, category_type
            FROM Categories
            ORDER BY category_type, category_name
        """)
        categories = cursor.fetchall()

        cursor.execute("""
            SELECT method_id, method_name
            FROM Payment_Methods
            ORDER BY method_name
        """)
        methods = cursor.fetchall()

        return render_template(
            "edit_transaction.html",
            transaction=transaction,
            categories=categories,
            methods=methods
        )

    finally:
        cursor.close()
        connection.close()


@app.route("/delete-transaction/<int:transaction_id>", methods=["POST"])
def delete_transaction(transaction_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            DELETE FROM Transactions
            WHERE transaction_id = %s AND user_id = %s
        """, (transaction_id, session["user_id"]))

        connection.commit()

        if cursor.rowcount:
            flash("Transaction deleted successfully!", "success")
        else:
            flash("Transaction not found.", "warning")

        return redirect(url_for("transactions"))

    finally:
        cursor.close()
        connection.close()


@app.route("/budget", methods=["GET", "POST"])
def budget():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        if request.method == "POST":
            category_id = int(request.form["category_id"])
            amount = float(request.form["budget_amount"])

            if amount <= 0:
                return "Please enter a valid budget amount."

            cursor.execute("""
                INSERT INTO Personal_Budgets
                (user_id, category_id, budget_amount)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                budget_amount = VALUES(budget_amount)
            """, (session["user_id"], category_id, amount))

            connection.commit()
            return redirect(url_for("dashboard"))

        cursor.execute("""
            SELECT category_id, category_name
            FROM Categories
            WHERE category_type = 'Expense'
            ORDER BY category_name
        """)
        categories = cursor.fetchall()

        return render_template("budget.html", categories=categories)

    finally:
        cursor.close()
        connection.close()

@app.route("/budget-summary")
def budget_summary():
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        user_id = session["user_id"]

        cursor.execute(
            "SELECT created_at FROM Users WHERE user_id = %s",
            (user_id,)
        )
        user = cursor.fetchone()

        if not user:
            return "User account not found."

        registration_date = user["created_at"].date()
        today = date.today()

        def anniversary(year, month):
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, min(registration_date.day, last_day))

        cycle_start = anniversary(today.year, today.month)

        if today < cycle_start:
            if today.month == 1:
                cycle_start = anniversary(today.year - 1, 12)
            else:
                cycle_start = anniversary(today.year, today.month - 1)

        if cycle_start.month == 12:
            next_cycle_start = anniversary(cycle_start.year + 1, 1)
        else:
            next_cycle_start = anniversary(
                cycle_start.year, cycle_start.month + 1
            )

        cursor.execute("""
            SELECT
                c.category_name,
                pb.budget_amount,
                COALESCE(SUM(t.amount), 0) AS actual_expenses
            FROM Personal_Budgets pb
            JOIN Categories c
                ON pb.category_id = c.category_id
            LEFT JOIN Transactions t
                ON t.category_id = pb.category_id
                AND t.user_id = pb.user_id
                AND t.transaction_date >= %s
                AND t.transaction_date < %s
            WHERE pb.user_id = %s
                AND c.category_type = 'Expense'
            GROUP BY pb.budget_id, c.category_name, pb.budget_amount
            ORDER BY c.category_name
        """, (cycle_start, next_cycle_start, user_id))

        budgets = cursor.fetchall()

        for item in budgets:
            item["remaining"] = (
                float(item["budget_amount"]) -
                float(item["actual_expenses"])
            )
            item["status"] = (
                "Exceeded" if item["remaining"] < 0 else "Within Budget"
            )

        cycle_end = next_cycle_start - date.resolution

        return render_template(
            "budget_summary.html",
            budgets=budgets,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            next_cycle_start=next_cycle_start
        )

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/monthly-report")
def monthly_report():
    if "user_id" not in session:
        return redirect(url_for("login"))

    month = request.args.get("month")
    year = request.args.get("year")

    report = None
    categories = []
    cycle_start = None
    cycle_end = None
    previous_cycle_start = None
    previous_cycle_end = None

    if month and year:
        try:
            month = int(month)
            year = int(year)

            if month < 1 or month > 12 or year < 2020:
                return "Invalid month or year."

        except ValueError:
            return "Please enter a valid month and year."

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        try:
            cursor.execute(
                "SELECT created_at FROM Users WHERE user_id = %s",
                (session["user_id"],)
            )
            user = cursor.fetchone()

            if not user:
                return "User account not found."

            registration_date = user["created_at"].date()

            def anniversary(y, m):
                last_day = calendar.monthrange(y, m)[1]
                return date(y, m, min(registration_date.day, last_day))

            cycle_start = anniversary(year, month)

            if month == 1:
                previous_cycle_start = anniversary(year - 1, 12)
            else:
                previous_cycle_start = anniversary(year, month - 1)

            previous_cycle_end = cycle_start - date.resolution

            if month == 12:
                next_cycle_start = anniversary(year + 1, 1)
            else:
                next_cycle_start = anniversary(year, month + 1)

            cycle_end = next_cycle_start - date.resolution

            cursor.execute("""
                SELECT
                    COALESCE(SUM(CASE
                        WHEN c.category_type = 'Income'
                        THEN t.amount ELSE 0
                    END), 0) AS total_income,
                    COALESCE(SUM(CASE
                        WHEN c.category_type = 'Expense'
                        THEN t.amount ELSE 0
                    END), 0) AS total_expenses
                FROM Transactions t
                JOIN Categories c ON t.category_id = c.category_id
                WHERE t.user_id = %s
                  AND t.transaction_date >= %s
                  AND t.transaction_date < %s
            """, (session["user_id"], cycle_start, next_cycle_start))

            report = cursor.fetchone()
            report["balance"] = (
                report["total_income"] - report["total_expenses"]
            )

            cursor.execute("""
                SELECT
                    c.category_name,
                    pb.budget_amount AS expected_budget,
                    COALESCE(SUM(t.amount), 0) AS total_spent
                FROM Personal_Budgets pb
                JOIN Categories c
                    ON pb.category_id = c.category_id
                LEFT JOIN Transactions t
                    ON t.user_id = pb.user_id
                    AND t.category_id = pb.category_id
                    AND t.transaction_date >= %s
                    AND t.transaction_date < %s
                WHERE pb.user_id = %s
                GROUP BY c.category_id, c.category_name,
                         pb.budget_id, pb.budget_amount
                ORDER BY c.category_name
            """, (cycle_start, next_cycle_start, session["user_id"]))

            categories = cursor.fetchall()

            cursor.execute(
                "SELECT expected_monthly_income FROM User_Financial_Settings WHERE user_id = %s",
                (session["user_id"],)
            )
            settings = cursor.fetchone()

            expected_income = float(settings["expected_monthly_income"]) if settings else 0
            expected_expenses = sum(float(item["expected_budget"]) for item in categories)
            expected_savings = expected_income - expected_expenses
            actual_savings = float(report["balance"])
            savings_difference = actual_savings - expected_savings

        finally:
            cursor.close()
            connection.close()

    return render_template(
        "monthly_report.html",
        report=report,
        categories=categories,
        expected_income=expected_income if month and year else 0,
        expected_expenses=expected_expenses if month and year else 0,
        expected_savings=expected_savings if month and year else 0,
        actual_savings=actual_savings if month and year else 0,
        savings_difference=savings_difference if month and year else 0,
        month=month,
        year=year,
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        previous_cycle_start=previous_cycle_start,
        previous_cycle_end=previous_cycle_end
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False)

