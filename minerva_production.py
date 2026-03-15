import os
import logging
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv
import requests
import psycopg2
from psycopg2 import extras
from flask import Flask, request, jsonify
from flask_cors import CORS

# Load environment variables
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Email notification settings (optional — if set, quote requests get emailed to Rodney)
SMTP_HOST = os.getenv("SMTP_HOST", "")          # e.g. "smtp-mail.outlook.com" for Outlook, "smtp.gmail.com" for Gmail
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")           # e.g. "freedompainting207@outlook.com"
SMTP_PASS = os.getenv("SMTP_PASS", "")           # App password or account password
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "freedompainting207@outlook.com")  # Where notifications go

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- WEB SERVER & CORS ---
app = Flask(__name__)
# CORS is critical. It allows the HTML widget on ANY website to talk to this backend.
CORS(app)

# --- DATABASE LOGIC (POSTGRESQL) ---
MAX_HISTORY = 20

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db_connection()
        conn.autocommit = True
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS web_users (
                user_id TEXT PRIMARY KEY,
                tier TEXT DEFAULT 'free',
                messages_today INTEGER DEFAULT 0,
                last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                email TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS web_history (
                id SERIAL PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS quote_requests (
                id SERIAL PRIMARY KEY,
                name TEXT,
                phone TEXT,
                email TEXT,
                service TEXT,
                message TEXT,
                source TEXT DEFAULT 'form',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'new'
            )
        ''')

        # Safely attempt to add the email column if the table already existed before this update
        try:
            c.execute("ALTER TABLE web_users ADD COLUMN email TEXT")
        except:
            pass  # Column already exists, safe to ignore

        c.close()
        conn.close()
        logger.info("Freedom Painting database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

def get_user_tier(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT tier FROM web_users WHERE user_id = %s', (user_id,))
        res = c.fetchone()
        c.close()
        conn.close()
        return res[0] if res else 'free'
    except:
        return 'free'

def get_message_count(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT messages_today, last_reset FROM web_users WHERE user_id = %s', (user_id,))
        res = c.fetchone()
        c.close()
        conn.close()
        if not res: return 0
        if datetime.now() - res[1] > timedelta(hours=24): return 0
        return res[0]
    except:
        return 0

def increment_count(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT messages_today, last_reset FROM web_users WHERE user_id = %s', (user_id,))
        res = c.fetchone()
        now = datetime.now()
        if not res:
            c.execute('INSERT INTO web_users (user_id, messages_today, last_reset) VALUES (%s, 1, %s)', (user_id, now))
        elif now - res[1] > timedelta(hours=24):
            c.execute('UPDATE web_users SET messages_today = 1, last_reset = %s WHERE user_id = %s', (now, user_id))
        else:
            c.execute('UPDATE web_users SET messages_today = messages_today + 1 WHERE user_id = %s', (user_id,))
        conn.commit()
        c.close()
        conn.close()
    except Exception as e:
        logger.error(f"Incr error: {e}")

def get_user_email(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT email FROM web_users WHERE user_id = %s', (user_id,))
        res = c.fetchone()
        c.close()
        conn.close()
        return res[0] if res else None
    except:
        return None

def set_user_email(user_id, email):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE web_users SET email = %s WHERE user_id = %s', (email, user_id))
        conn.commit()
        c.close()
        conn.close()
    except Exception as e:
        logger.error(f"Email save error: {e}")

def save_chat(user_id, role, content):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('INSERT INTO web_history (user_id, role, content) VALUES (%s, %s, %s)', (user_id, role, content))
        c.execute('''
            DELETE FROM web_history WHERE id IN (
                SELECT id FROM web_history WHERE user_id = %s ORDER BY timestamp DESC OFFSET %s
            )
        ''', (user_id, MAX_HISTORY))
        conn.commit()
        c.close()
        conn.close()
    except Exception as e:
        logger.error(f"Save error: {e}")

def get_history(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor(cursor_factory=extras.DictCursor)
        c.execute('SELECT role, content FROM web_history WHERE user_id = %s ORDER BY timestamp ASC', (user_id,))
        rows = c.fetchall()
        c.close()
        conn.close()
        return [{"role": r['role'], "content": r['content']} for r in rows]
    except:
        return []

def save_quote_request(name, phone, email, service, message, source='form'):
    """Save a quote request to the database and return True on success."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO quote_requests (name, phone, email, service, message, source)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (name, phone, email, service, message, source))
        conn.commit()
        c.close()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Quote save error: {e}")
        return False

def send_email_notification(subject, body_text):
    """Send an email notification to Rodney. Fails silently if SMTP is not configured."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        logger.info("SMTP not configured — skipping email notification.")
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = NOTIFY_EMAIL
        msg['Subject'] = subject

        msg.attach(MIMEText(body_text, 'plain'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())

        logger.info(f"Email notification sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email send error: {e}")
        return False


# --- FREEDOM PAINTING SYSTEM PROMPT ---
SYSTEM_PROMPT = """You are the automated assistant for Freedom Painting, a professional residential painting company based in Hodgdon, Maine, serving the Houlton and Aroostook County area. The owner is Rodney McEwen.

Your tone is friendly, professional, and down-to-earth. You sound like a helpful person at a local Maine business — warm but efficient. You are NOT robotic. You are NOT overly formal.

BUSINESS KNOWLEDGE:
- Company: Freedom Painting
- Owner: Rodney McEwen
- Phone: (207) 502-9970
- Email: freedompainting207@outlook.com
- Location: Calais Rd, Hodgdon, ME 04730
- Service Area: Houlton, Hodgdon, Presque Isle, Aroostook County, and surrounding areas in Maine
- Facebook: 100% recommendation rate across 58+ reviews

SERVICES OFFERED:
- Exterior Painting (siding, trim, shutters, full repaints — everything from historic Victorians to modern homes)
- Interior Painting (walls, ceilings, trim, doors, accent walls)
- Cabinet Refinishing (kitchen and bathroom cabinets — sand, prime, and paint with durable finishes)
- Deck & Fence Staining (power wash, prep, stain, and seal)
- Wall Repair & Prep (crack repair, patching, skim coating, water damage repair)
- Color Consultation (help choosing colors and palettes)
- We use high-quality materials including Sherwin-Williams and Benjamin Moore products

PRICING:
- We do NOT quote set prices. Every project is unique and quoted individually based on size, condition, and scope.
- We offer FREE estimates and consultations. No obligation.
- If someone asks about pricing, tell them we provide free quotes and encourage them to describe their project so Rodney can follow up with an accurate estimate.

BOOKING & QUOTES:
- To get a quote: The customer should describe their project (what needs painting, how many rooms/square footage, current condition, timeline). You collect this info along with their name, phone number, and email, then tell them Rodney will follow up within 24 hours.
- To schedule a consultation: Collect their name, phone, email, preferred date/time window, and project description. Tell them Rodney will confirm the appointment.
- Always try to collect: name, phone number, email, and project details.

STRICT RULES:
1. NEVER invent prices, timelines, or policies not described above. If you don't know something specific, say Rodney will be able to answer that directly.
2. Keep responses conversational and concise — 2-4 sentences max unless collecting project details.
3. If someone shares project details, acknowledge them and gently ask for their contact info so Rodney can follow up.
4. Always be helpful about scheduling and what to expect from the process.
5. If someone seems ready to book, push them toward providing their contact info or calling (207) 502-9970.
6. Do not use excessive emojis. One occasionally is fine.
7. If asked about anything unrelated to painting or home improvement, politely redirect to how Freedom Painting can help them.
8. Mention the 100% recommendation rate and 58+ five-star reviews when relevant — it builds trust.
9. If someone asks about commercial work, say we specialize in residential but they're welcome to call Rodney to discuss."""

# --- API ENDPOINTS ---

@app.route('/', methods=['GET'])
def health():
    return "Minerva Web API is live and routing traffic.", 200

@app.route('/chat', methods=['POST'])
def chat():
    # 1. Parse the incoming JSON from the website frontend
    data = request.get_json()
    if not data or 'user_id' not in data or 'message' not in data:
        return jsonify({"reply": "Invalid request payload."}), 400

    uid = str(data['user_id'])
    txt = data['message'].strip()

    if not txt:
        return jsonify({"reply": "Feel free to ask me anything about our painting services!"}), 400

    # 2. Check Database Limits and Email Status
    tier = get_user_tier(uid)
    count = get_message_count(uid)
    email = get_user_email(uid)

    # THE LEAD CATCHER TRAP
    if count >= 3 and not email:
        # Check if the user's message contains an email address
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", txt)
        if email_match:
            extracted_email = email_match.group(0)
            set_user_email(uid, extracted_email)
            success_reply = f"Got it, thanks! I've saved your contact info ({extracted_email}). Rodney will be able to follow up with you directly. Now, how can I help with your painting project?"
            save_chat(uid, "user", txt)
            save_chat(uid, "assistant", success_reply)

            # Notify Rodney about the new lead
            send_email_notification(
                subject="New Website Lead — Freedom Painting",
                body_text=f"A new lead was captured from the website chatbot.\n\nEmail: {extracted_email}\nChat User ID: {uid}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\nCheck the conversation history in your database for full context."
            )

            return jsonify({"reply": success_reply})
        else:
            # Block them from proceeding until they provide an email
            return jsonify({"reply": "Before we continue, could you share your email address? That way Rodney can follow up with you directly about your project."})

    if tier == 'free' and count >= 15:
        return jsonify({"reply": "You've been really thorough — love the detail! You've hit our chat limit for today, but you can always call Rodney directly at (207) 502-9970 or fill out the quote form on our website. We'll get back to you within 24 hours."})

    # 3. Save User Message & Get Context
    save_chat(uid, "user", txt)
    hist = get_history(uid)

    # Check if the user just provided contact/project info that looks like a quote request
    # This lets the AI naturally collect info and we capture it in the conversation history
    payload = [{"role": "system", "content": SYSTEM_PROMPT}] + hist

    # 4. Synchronous request to Groq
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(GROQ_URL, headers=headers, json={
            "model": "llama-3.1-8b-instant",
            "messages": payload,
            "temperature": 0.7
        }, timeout=15)

        resp_data = resp.json()
        if "choices" in resp_data and len(resp_data["choices"]) > 0:
            bot_res = resp_data["choices"][0]["message"]["content"]

            # 5. Save AI Reply & Update Count
            save_chat(uid, "assistant", bot_res)
            increment_count(uid)

            # 6. Send the text back to the HTML website
            return jsonify({"reply": bot_res})
        else:
            logger.error(f"Groq API Error: {resp_data}")
            return jsonify({"reply": "I'm having a little trouble right now. You can always call us directly at (207) 502-9970!"})
    except Exception as e:
        logger.error(f"Network Logic Error: {e}")
        return jsonify({"reply": "A network hiccup — sorry about that. Give us a call at (207) 502-9970 and we'll take care of you."})


@app.route('/contact', methods=['POST'])
def contact():
    """Handle quote request form submissions from the website."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "No data received."}), 400

    name = data.get('name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    service = data.get('service', '').strip()
    message = data.get('message', '').strip()

    if not name or not phone or not email:
        return jsonify({"success": False, "error": "Name, phone, and email are required."}), 400

    # Save to database
    saved = save_quote_request(name, phone, email, service, message, source='website_form')

    if not saved:
        return jsonify({"success": False, "error": "Failed to save request."}), 500

    # Send email notification to Rodney
    service_label = {
        'exterior': 'Exterior Painting',
        'interior': 'Interior Painting',
        'cabinets': 'Cabinet Refinishing',
        'deck': 'Deck / Fence Staining',
        'repair': 'Wall Repair / Prep',
        'other': 'Other'
    }.get(service, service or 'Not specified')

    email_body = f"""New Quote Request from Freedom Painting Website
{'='*50}

Name:     {name}
Phone:    {phone}
Email:    {email}
Service:  {service_label}

Project Details:
{message or 'No details provided.'}

{'='*50}
Submitted: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}
Source: Website Contact Form

Reply to this customer within 24 hours!
"""

    send_email_notification(
        subject=f"New Quote Request: {name} — {service_label}",
        body_text=email_body
    )

    logger.info(f"Quote request saved from {name} ({email})")
    return jsonify({"success": True, "message": "Quote request received."})


if __name__ == "__main__":
    if not GROQ_API_KEY or not DATABASE_URL:
        logger.error("Critical environment variables missing. Set GROQ_API_KEY and DATABASE_URL.")
    else:
        init_db()
        port = int(os.environ.get("PORT", 8080))
        # This starts the Flask web server to listen for website traffic
        app.run(host='0.0.0.0', port=port)
