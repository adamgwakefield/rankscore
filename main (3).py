# Standard library imports
import sqlite3
import os
import time
import string
from email.message import EmailMessage
import secrets
from datetime import datetime
import smtplib
import ssl
from contextlib import contextmanager
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

# Third-party imports
import streamlit as st
import validators
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from bs4 import BeautifulSoup
from fpdf import FPDF
import gspread
from google.oauth2.service_account import Credentials
import stripe

# -------------------- CONFIGURATION --------------------
# Load sensitive data from environment variables
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY", "")
PRICE_ID = "price_1R0skJFAFoWFp1B2AVfYvzAV"
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "yourbusiness@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "your-app-password")

# Configure Stripe
stripe.api_key = STRIPE_API_KEY

# -------------------- DATABASE UTILS --------------------
@contextmanager
def get_db_connection(db_name):
    conn = sqlite3.connect(db_name)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Initialize SQLite database for storing access codes."""
    with get_db_connection("rankscore_codes.db") as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS access_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                code TEXT UNIQUE,
                used BOOLEAN DEFAULT 0
            )
        ''')
        conn.commit()

def init_tracking_database():
    """Initialize SQLite database for tracking AEO progress."""
    with get_db_connection("aeo_tracking.db") as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY,
                url TEXT,
                scan_date TIMESTAMP,
                total_score INTEGER,
                content_structure_score INTEGER,
                technical_score INTEGER,
                metadata_score INTEGER,
                accessibility_score INTEGER,
                speed_score INTEGER,
                structured_data_present BOOLEAN,
                faq_present BOOLEAN,
                mobile_friendly BOOLEAN
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY,
                url TEXT,
                creation_date TIMESTAMP,
                type TEXT,
                description TEXT,
                priority INTEGER,
                points_potential INTEGER,
                status TEXT,
                implementation_date TIMESTAMP,
                notes TEXT
            )
        ''')
        conn.commit()

def generate_unique_code():
    """Generate a secure, random 8-character access code."""
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

def save_access_code(email):
    """Generate and save a unique access code for a new user."""
    code = generate_unique_code()
    with get_db_connection("rankscore_codes.db") as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO access_codes (email, code) VALUES (?, ?)", (email, code))
            conn.commit()
        except sqlite3.IntegrityError:
            return save_access_code(email)  # Retry if conflict
    return code

def validate_access_code(user_code):
    """Check if the entered access code is valid and not used."""
    with get_db_connection("rankscore_codes.db") as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM access_codes WHERE code = ? AND used = 0", (user_code,))
        return c.fetchone()

def mark_code_as_used(user_code):
    """Mark the access code as used after login."""
    with get_db_connection("rankscore_codes.db") as conn:
        c = conn.cursor()
        c.execute("UPDATE access_codes SET used = 1 WHERE code = ?", (user_code))
        conn.commit()

def save_scan_results(url, score_data, metadata, structured_data, faq_present, mobile_friendly, speed_metrics):
    """Save scan results to database."""
    with get_db_connection("aeo_tracking.db") as conn:
        c = conn.cursor()
        c.execute('''
            SELECT COUNT(*) FROM scans WHERE url = ? AND scan_date >= datetime('now', '-1 minute')
        ''', (url,))
        if c.fetchone()[0] > 0:
            return
        c.execute('''
            INSERT INTO scans (url, scan_date, total_score, content_structure_score, technical_score,
                              metadata_score, accessibility_score, speed_score, structured_data_present,
                              faq_present, mobile_friendly)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (url, datetime.now(), score_data["total_score"], score_data["subscores"]["content_structure"],
              score_data["subscores"]["technical"], score_data["subscores"]["metadata"],
              score_data["subscores"]["accessibility"], speed_metrics["performance_score"],
              bool(structured_data), bool(faq_present), bool(mobile_friendly)))
        conn.commit()

def save_recommendations(url, recommendations):
    """Save new recommendations to database."""
    with get_db_connection("aeo_tracking.db") as conn:
        c = conn.cursor()
        for rec in recommendations:
            c.execute('''
                SELECT COUNT(*) FROM recommendations WHERE url = ? AND type = ? AND status = 'pending'
            ''', (url, rec["type"]))
            if c.fetchone()[0] == 0:
                c.execute('''
                    INSERT INTO recommendations (url, creation_date, type, description, priority,
                                               points_potential, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (url, datetime.now(), rec["type"], rec["message"], rec["priority"],
                      rec["points_available"], "pending"))
        conn.commit()

def update_recommendation_status(rec_id, status, notes=None):
    """Update recommendation status and implementation date."""
    with get_db_connection("aeo_tracking.db") as conn:
        c = conn.cursor()
        c.execute('''
            UPDATE recommendations SET status = ?, implementation_date = ?, notes = ?
            WHERE id = ?
        ''', (status, datetime.now() if status == "implemented" else None, notes, rec_id))
        conn.commit()

def get_historical_data(url):
    """Retrieve historical scan data for a URL."""
    with get_db_connection("aeo_tracking.db") as conn:
        df_scans = pd.read_sql_query('''
            SELECT scan_date, total_score, content_structure_score, technical_score,
                   metadata_score, accessibility_score, speed_score
            FROM scans WHERE url = ? ORDER BY scan_date
        ''', conn, params=(url,))
        df_recs = pd.read_sql_query('''
            SELECT creation_date, type, description, priority, points_potential, status,
                   implementation_date
            FROM recommendations WHERE url = ? ORDER BY priority DESC, points_potential DESC
        ''', conn, params=(url,))
    return df_scans, df_recs

def get_progress_summary(url):
    """Generate a summary of progress for reporting."""
    df_scans, df_recs = get_historical_data(url)
    if df_scans.empty:
        return None
    first_scan = df_scans.iloc[0]
    latest_scan = df_scans.iloc[-1]
    implemented_recs = df_recs[df_recs["status"] == "implemented"]
    return {
        "initial_score": first_scan["total_score"],
        "current_score": latest_scan["total_score"],
        "total_improvement": latest_scan["total_score"] - first_scan["total_score"],
        "scan_count": len(df_scans),
        "implemented_changes": len(implemented_recs),
        "pending_changes": len(df_recs[df_recs["status"] == "pending"]),
        "implementation_impact": implemented_recs["points_potential"].sum(),
        "trending_data": df_scans,
        "recommendations": df_recs
    }

# -------------------- EMAIL AND GOOGLE SHEETS --------------------
def send_access_code(email, access_code):
    """Send the RankScore Pro access code to the user's email."""
    subject = "Your RankScore Pro Access Code"
    body = f"\n🎉 Thank you for purchasing RankScore Pro!\n\nYour unique access code: **{access_code}\n\nEnter this code in the RankScore Pro app to unlock full features.\n\nIf you have issues, contact support@rankscore.ai.\n"
    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = email
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        st.error(f"Failed to send email: {e}")

def save_to_google_sheets(email, url):
    """Save lead data to Google Sheets."""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file("credentials.json", scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open("RankScore Leads").sheet1
        sheet.append_row([email, url, str(datetime.now())])
        return True
    except FileNotFoundError:
        st.error("credentials.json file not found.")
        return False
    except Exception as e:
        st.error(f"Error saving to Google Sheets: {e}")
        return False

# -------------------- AEO ANALYSIS FUNCTIONS --------------------
def validate_url(url):
    """Validate the provided URL."""
    try:
        return validators.url(url)
    except Exception:
        return False

def analyze_lite_aeo(url):
    """Provide a basic AEO score for the Lite version."""
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        title_present = bool(soup.title)
        return 65 if title_present else 50  # Base score with title bonus
    except Exception:
        return 0

def analyze_metadata(url):
    """Perform metadata analysis."""
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.string if soup.title else "Missing"
        description = soup.find("meta", attrs={"name": "description"})
        description = description["content"] if description else "Missing"
        recommendation = "Consider adding a meta description to improve visibility and SEO." if description == "Missing" else "Meta description is present."
        return {"title": title, "description": description, "recommendation": recommendation}
    except Exception as e:
        return {"title": "Error", "description": f"Error fetching metadata: {e}", "recommendation": "N/A"}

def analyze_headers(soup):
    """Analyze header structure."""
    h1_tags = soup.find_all("h1")
    h2_tags = soup.find_all("h2")
    return {"h1_present": len(h1_tags) > 0, "h2_present": len(h2_tags) > 0, "h1_count": len(h1_tags), "h2_count": len(h2_tags)}

def analyze_structured_data(soup):
    """Check for structured data."""
    scripts = soup.find_all("script", type="application/ld+json")
    return len(scripts) > 0

def analyze_faq(soup):
    """Check for FAQ schema."""
    faq_schema = soup.find("script", type="application/ld+json", string=lambda text: text and "FAQPage" in text)
    return faq_schema is not None

def analyze_mobile_friendly(soup):
    """Check for mobile-friendliness."""
    viewport_meta = soup.find("meta", attrs={"name": "viewport"})
    return viewport_meta is not None

def analyze_accessibility(soup):
    """Check image accessibility."""
    images = soup.find_all("img")
    images_with_alt = [img for img in images if img.has_attr("alt")]
    return len(images_with_alt) == len(images)

def analyze_page_speed(url):
    """Analyze page speed metrics."""
    try:
        metrics = {
            "total_time": 0, "time_to_first_byte": 0, "resource_count": 0,
            "total_size": 0, "resource_types": {}, "performance_score": 0
        }
        start_time = time.time()
        response = requests.get(url, stream=True)
        ttfb = time.time() - start_time
        metrics["time_to_first_byte"] = round(ttfb * 1000, 2)

        soup = BeautifulSoup(response.text, "html.parser")
        resources = [
            (script["src"], "script") for script in soup.find_all("script", src=True) +
            (link["href"], "css") for link in soup.find_all("link", rel="stylesheet") +
            (img["src"], "image") for img in soup.find_all("img", src=True)
        ]
        metrics["resource_count"] = len(resources)

        def fetch_resource(resource):
            resource_url, resource_type = resource
            if not resource_url.startswith(("http://", "https://")):
                base_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                resource_url = base_url + resource_url
            try:
                r = requests.head(resource_url, timeout=5)
                return resource_type, int(r.headers.get("content-length", 0))
            except Exception:
                return resource_type, 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(fetch_resource, resources))
        for resource_type, size in results:
            metrics["total_size"] += size
            metrics["resource_types"][resource_type] = metrics["resource_types"].get(resource_type, 0) + 1

        metrics["total_time"] = round((time.time() - start_time) * 1000, 2)
        score = 100
        if metrics["time_to_first_byte"] > 200: score -= 20
        if metrics["total_time"] > 3000: score -= 20
        if metrics["resource_count"] > 50: score -= 20
        if metrics["total_size"] > 5000000: score -= 20
        metrics["performance_score"] = max(0, score)

        return metrics
    except Exception as e:
        return {"error": str(e), "total_time": 0, "time_to_first_byte": 0, "resource_count": 0, "total_size": 0, "resource_types": {}, "performance_score": 0}

def calculate_rankscore(metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics):
    """Calculate RankScore with weights optimized for AEO."""
    structured_data_score = 25 if structured_data else 0
    faq_score = 20 if faq_present else 0
    header_score = 15 if headers["h1_present"] else 0
    title_score = 10 if metadata["title"] != "Missing" else 0
    speed_score = 10 if speed_metrics["performance_score"] >= 80 else 0
    description_score = 8 if metadata["description"] != "Missing" else 0
    mobile_score = 7 if mobile_friendly else 0
    accessibility_score = 5 if accessibility else 0

    total_score = (structured_data_score + faq_score + header_score +
                  title_score + speed_score + description_score +
                  mobile_score + accessibility_score)

    return {
        "total_score": total_score,
        "subscores": {
            "content_structure": structured_data_score + faq_score + header_score,
            "technical": speed_score + mobile_score,
            "metadata": title_score + description_score,
            "accessibility": accessibility_score
        },
        "component_scores": {
            "structured_data": structured_data_score, "faq": faq_score,
            "headers": header_score, "title": title_score,
            "speed": speed_score, "description": description_score,
            "mobile": mobile_score, "accessibility": accessibility_score
        }
    }

def get_impact_description(metric_type):
    """Return the impact description for different optimization metrics."""
    impacts = {
        "title": {"what": "Title tags are crucial for search engines...", "why": "A well-optimized title...", "impact": "High"},
        "description": {"what": "Meta descriptions provide...", "why": "Clear descriptions...", "impact": "Medium-High"},
        "h1": {"what": "H1 headers define...", "why": "Answer engines use...", "impact": "High"},
        "structured_data": {"what": "Structured data provides...", "why": "It helps answer...", "impact": "High"},
        "faq": {"what": "FAQ sections address...", "why": "This format matches...", "impact": "Medium-High"},
        "mobile": {"what": "Mobile-friendly pages...", "why": "Voice searches often...", "impact": "Medium"},
        "accessibility": {"what": "Accessible content...", "why": "Clear content structure...", "impact": "Medium"},
        "speed": {"what": "Page speed affects...", "why": "Faster pages are...", "impact": "High"}
    }
    return impacts.get(metric_type, {})

def prioritize_quick_wins(metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics):
    """Prioritize quick wins based on impact and effort."""
    issues = []
    if metadata["title"] == "Missing": issues.append({"type": "title", "priority": 1, "effort": "Low", "fix": "Add a descriptive title tag", "example": "Best Italian Recipes | Easy Guide"})
    if metadata["description"] == "Missing": issues.append({"type": "description", "priority": 2, "effort": "Low", "fix": "Add a meta description", "example": "Discover easy Italian recipes..."})
    if not headers["h1_present"]: issues.append({"type": "h1", "priority": 1, "effort": "Low", "fix": "Add an H1 header", "example": "Welcome to Italian Recipes"})
    if not structured_data: issues.append({"type": "structured_data", "priority": 3, "effort": "Medium", "fix": "Implement structured data", "example": "Add Recipe schema markup..."})
    if not faq_present: issues.append({"type": "faq", "priority": 3, "effort": "Medium", "fix": "Add FAQ schema markup", "example": "Include FAQs with schema..."})
    if not mobile_friendly: issues.append({"type": "mobile", "priority": 2, "effort": "Medium", "fix": "Implement responsive design", "example": '<meta name="viewport" content="width=device-width">'})
    if not accessibility: issues.append({"type": "accessibility", "priority": 2, "effort": "Low", "fix": "Add image alt text", "example": '<img src="pasta.jpg" alt="Fresh pasta">'})
    if speed_metrics["time_to_first_byte"] > 200: issues.append({"type": "speed", "priority": 1, "effort": "Medium", "fix": "Improve server response", "example": "Optimize server config..."})
    if speed_metrics["total_size"] > 5000000: issues.append({"type": "speed", "priority": 2, "effort": "Medium", "fix": "Reduce page size", "example": "Compress images..."})
    if speed_metrics["resource_count"] > 50: issues.append({"type": "speed", "priority": 2, "effort": "Medium", "fix": "Reduce requests", "example": "Combine CSS files..."})
    return sorted(issues, key=lambda x: (x["priority"], 0 if x["effort"] == "Low" else 1))

# -------------------- PDF REPORT GENERATION --------------------
def generate_quick_wins_pdf_report(url, score, top_issues):
    """Generate a PDF report with quick wins to encourage Pro upgrade."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, txt="RankScore Lite Quick Wins", ln=True, align="C")
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 10, txt=f"URL: {url}", ln=True)
    pdf.cell(0, 10, txt=f"AEO Score: {score}/100", ln=True)
    pdf.ln(5)
    pdf.multi_cell(0, 10, txt="Upgrade to RankScore Pro for a full AEO analysis and detailed recommendations!")
    pdf.ln(5)
    pdf.set_font("Arial", style="B", size=10)
    pdf.cell(0, 10, txt="Top 3 Quick Wins", ln=True)
    pdf.ln(5)
    for i, issue in enumerate(top_issues[:3], 1):
        pdf.set_font("Arial", size=10)
        pdf.multi_cell(0, 10, f"{i}. {issue['fix']} (Effort: {issue['effort']}, Example: {issue['example']})")
    pdf.ln(5)
    pdf.set_font("Arial", style="B", size=10)
    pdf.cell(0, 10, txt="Next Step", ln=True)
    pdf.set_font("Arial", size=10)
    pdf.multi_cell(0, 10, txt="[Click here to upgrade to RankScore Pro](https://rankscore.ai/)")
    pdf_file = "lite_quick_wins.pdf"
    try:
        pdf.output(pdf_file)
    except Exception as e:
        st.error(f"Failed to generate PDF: {e}")
        return None
    return pdf_file

def generate_detailed_pdf_report(url, metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics, rankscore):
    """Generate a detailed PDF report for Pro users."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, txt="RankScore Pro Detailed Analysis", ln=True, align="C")
    pdf.set_font("Arial", size=10)
    pdf.cell(0, 10, txt=f"URL: {url}", ln=True)
    pdf.cell(0, 10, txt=f"RankScore: {rankscore}/100", ln=True)
    pdf.ln(5)
    sections = [
        ("Title", metadata["title"], "Add a descriptive title..."),
        ("Description", metadata["description"], "Add a meta description..."),
        ("Headers", f"H1 Present: {headers['h1_present']}, H2 Present: {headers['h2_present']}", "Ensure at least one H1..."),
        ("Structured Data", "Present" if structured_data else "Missing", "Add structured data..."),
        ("FAQs", "Present" if faq_present else "Missing", "Include FAQs..."),
        ("Mobile-Friendliness", "Yes" if mobile_friendly else "No", "Add viewport meta tag..."),
        ("Accessibility", "Yes" if accessibility else "No", "Add alt attributes..."),
        ("Speed Score", f"{speed_metrics['performance_score']}/100", "Optimize for better performance...")
    ]
    for title, value, recommendation in sections:
        pdf.set_font("Arial", style="B", size=10)
        pdf.cell(0, 10, txt=title, ln=True)
        pdf.set_font("Arial", size=10)
        pdf.cell(0, 10, txt=f"Value: {value}", ln=True)
        pdf.multi_cell(0, 10, txt=f"Recommendation: {recommendation}")
        pdf.ln(5)
    pdf_file = "pro_detailed_report.pdf"
    try:
        pdf.output(pdf_file)
    except Exception as e:
        st.error(f"Failed to generate PDF: {e}")
        return None
    return pdf_file

# -------------------- PROGRESS DASHBOARD --------------------
def create_progress_dashboard():
    st.title("AEO Progress Dashboard")
    init_tracking_database()
    with get_db_connection("aeo_tracking.db") as conn:
        urls = pd.read_sql_query("SELECT DISTINCT url FROM scans", conn)
    if urls.empty:
        st.warning("No analysis history found. Run a Pro analysis to begin tracking.")
        return
    selected_url = st.selectbox("Select URL to view progress", urls["url"])
    if not selected_url:
        return
    progress_summary = get_progress_summary(selected_url)
    if not progress_summary:
        st.warning("No data available for selected URL")
        return
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Score Improvement", f"{progress_summary['total_improvement']} pts", f"{progress_summary['current_score']}/100 current")
    with col2:
        st.metric("Implemented Changes", progress_summary["implemented_changes"], f"+{progress_summary['implementation_impact']} potential points")
    with col3:
        st.metric("Pending Optimizations", progress_summary["pending_changes"])
    st.subheader("Score Trending")
    fig = px.line(progress_summary["trending_data"], x="scan_date", y=["total_score", "content_structure_score", "technical_score", "metadata_score", "accessibility_score"], title="Score Progress Over Time")
    st.plotly_chart(fig)
    st.subheader("Optimization Progress")
    recs_df = progress_summary["recommendations"]
    status_counts = recs_df["status"].value_counts()
    fig_status = go.Figure(data=[go.Pie(labels=status_counts.index, values=status_counts.values, hole=.3)])
    fig_status.update_layout(title="Implementation Status")
    st.plotly_chart(fig_status)
    st.subheader("Recommendations Status")
    for _, rec in recs_df.sort_values(["priority", "points_potential"], ascending=[True, False]).iterrows():
        with st.expander(f"{rec['type']}: {rec['description'][:50]}...", expanded=rec["status"]=="pending"):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(f"**Description:** {rec['description']}")
                st.write(f"**Potential Impact:** +{rec['points_potential']} points")
                st.write(f"**Priority:** {rec['priority']}")
            with col2:
                status = st.selectbox("Status", ["pending", "in_progress", "implemented", "deferred"], index=["pending", "in_progress", "implemented", "deferred"].index(rec["status"]), key=f"status_{rec.name}")
                if status != rec["status"]:
                    notes = st.text_area("Implementation Notes", key=f"notes_{rec.name}")
                    if st.button("Update Status", key=f"update_{rec.name}"):
                        update_recommendation_status(rec.name, status, notes)
                        st.success("Status updated!")
                        st.experimental_rerun()
    if st.button("Generate Progress Report"):
        report = generate_progress_report(selected_url, progress_summary)
        st.download_button("Download Progress Report", report, file_name="progress_report.pdf", mime="application/pdf")

def generate_progress_report(url, progress_summary):
    """Generate a PDF progress report."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "AEO Progress Report", ln=True, align="C")
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"URL: {url}", ln=True)
    pdf.cell(0, 10, f"Report Date: {datetime.now().strftime('%Y-%m-%d')}", ln=True)
    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Overall Progress", ln=True)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"Initial Score: {progress_summary['initial_score']}", ln=True)
    pdf.cell(0, 10, f"Current Score: {progress_summary['current_score']}", ln=True)
    pdf.cell(0, 10, f"Total Improvement: {progress_summary['total_improvement']} points", ln=True)
    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Implementation Status", ln=True)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"Implemented Changes: {progress_summary['implemented_changes']}", ln=True)
    pdf.cell(0, 10, f"Pending Optimizations: {progress_summary['pending_changes']}", ln=True)
    pdf.cell(0, 10, f"Impact of Implemented Changes: +{progress_summary['implementation_impact']} points", ln=True)
    pdf.ln(10)
    pdf.set_font("Arial", "B", 14)
    pdf.cell(0, 10, "Recent Implementations", ln=True)
    pdf.set_font("Arial", "", 12)
    implemented = progress_summary["recommendations"][progress_summary["recommendations"]["status"] == "implemented"].sort_values("implementation_date", ascending=False)
    for _, rec in implemented.head().iterrows():
        pdf.multi_cell(0, 10, f"• {rec['description']} (+{rec['points_potential']} points)")
    return pdf.output(dest="S").encode("latin-1")

# -------------------- MAIN APPLICATION --------------------
def main():
    st.title("RankScore - AEO Visibility Analyzer")
    if "lite_displayed" not in st.session_state:
        st.session_state["lite_displayed"] = False
    if "pro_unlocked" not in st.session_state:
        st.session_state["pro_unlocked"] = False
    if "pro_analysis" not in st.session_state:
        st.session_state["pro_analysis"] = {}

    # Lite Version (Lead Magnet)
    st.subheader("🚀 Free AEO Score - RankScore Lite")
    st.write("Get a basic AEO score to evaluate your website's visibility. Enter your details below!")
    email = st.text_input("Enter your email to receive your free AEO report:", key="lite_email", value=st.session_state.get("lite_email", ""))
    url = st.text_input("Enter your website URL:", key="lite_url", value=st.session_state.get("lite_url", ""))
    if st.button("Get Free AEO Score"):
        if email and url and validate_url(url):
            score = analyze_lite_aeo(url)
            st.success(f"✅ Your AEO Score is {score}/100 (Lite Version)")
            st.write("⚠️ Upgrade to RankScore Pro for a detailed analysis, optimization recommendations, and progress tracking!")
            st.markdown("[Upgrade to RankScore Pro 🚀](https://rankscore.ai/)")
            response = requests.get(url, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")
            top_issues = prioritize_quick_wins(analyze_metadata(url), analyze_headers(soup), False, False, False, False, analyze_page_speed(url))
            report_file = generate_quick_wins_pdf_report(url, score, top_issues)
            if report_file:
                with open(report_file, "rb") as file:
                    st.download_button("Download Quick Wins Report", file, file_name=report_file)
            save_to_google_sheets(email, url)  # Capture lead
            st.session_state["lite_email"] = email
            st.session_state["lite_url"] = url
        else:
            st.error("❌ Please enter a valid email and URL.")

    # Pro Access and Payment
    st.sidebar.title("🔐 Unlock RankScore Pro")
    user_code = st.sidebar.text_input("Enter Access Code:", type="password", key="pro_code")
    if user_code and not st.session_state["pro_unlocked"]:
        valid_code = validate_access_code(user_code)
        if valid_code:
            st.sidebar.success("✅ Access Granted to RankScore Pro!")
            st.session_state["pro_unlocked"] = True
            mark_code_as_used(user_code)
        else:
            st.sidebar.error("❌ Invalid or used access code. Check your email or purchase Pro.")

    if st.session_state["pro_unlocked"]:
        st.subheader("🏆 Welcome to RankScore Pro!")
        page = st.radio("Select Feature", ["Full Analysis", "Progress Dashboard"], key="pro_page")
        if page == "Full Analysis":
            url = st.text_input("Enter your website URL for full analysis:", key="pro_url", value=st.session_state.get("pro_url", ""))
            if url and validate_url(url):
                if st.button("Analyze Full AEO"):
                    response = requests.get(url, timeout=10)
                    soup = BeautifulSoup(response.text, "html.parser")
                    metadata = analyze_metadata(url)
                    headers = analyze_headers(soup)
                    structured_data = analyze_structured_data(soup)
                    faq_present = analyze_faq(soup)
                    mobile_friendly = analyze_mobile_friendly(soup)
                    accessibility = analyze_accessibility(soup)
                    speed_metrics = analyze_page_speed(url)
                    score_data = calculate_rankscore(metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics)
                    st.session_state["pro_analysis"] = {
                        "metadata": metadata, "headers": headers, "structured_data": structured_data,
                        "faq_present": faq_present, "mobile_friendly": mobile_friendly, "accessibility": accessibility,
                        "speed_metrics": speed_metrics, "score_data": score_data
                    }
                    st.write(f"**Overall AEO Score:** {score_data['total_score']}/100")
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        st.metric("Content Structure", f"{score_data['subscores']['content_structure']}/60")
                        st.metric("Technical", f"{score_data['subscores']['technical']}/17")
                    with col2:
                        st.metric("Metadata", f"{score_data['subscores']['metadata']}/18")
                        st.metric("Accessibility", f"{score_data['subscores']['accessibility']}/5")
                    with col3:
                        st.metric("Speed", f"{speed_metrics['performance_score']}/100")
                    st.write("**Components:**")
                    st.write(f"- Title: {metadata['title']}")
                    st.write(f"- Description: {metadata['description']}")
                    st.write(f"- Structured Data: {structured_data}")
                    st.write(f"- FAQs: {faq_present}")
                    st.write(f"- Mobile-Friendly: {mobile_friendly}")
                    st.write(f"- Accessibility: {accessibility}")
                    top_issues = prioritize_quick_wins(metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics)
                    st.write("**Top Recommendations:**")
                    for issue in top_issues[:3]:
                        st.write(f"- {issue['fix']} (Impact: {issue['priority']}, Effort: {issue['effort']})")
                    save_scan_results(url, score_data, metadata, structured_data, faq_present, mobile_friendly, speed_metrics)
                    save_recommendations(url, [{"type": i["type"], "message": i["fix"], "priority": i["priority"], "points_available": 10} for i in top_issues[:3]])
                    if st.button("Download Detailed Report"):
                        report_file = generate_detailed_pdf_report(url, metadata, headers, structured_data, faq_present, mobile_friendly, accessibility, speed_metrics, score_data["total_score"])
                        if report_file:
                            with open(report_file, "rb") as file:
                                st.download_button("Download Detailed Report", file, file_name=report_file)
                st.session_state["pro_url"] = url
            else:
                st.warning("Please enter a valid URL.")
        elif page == "Progress Dashboard":
            create_progress_dashboard()

    # Payment Flow
    email = st.session_state.get("lite_email", "")
    if st.button("Buy RankScore Pro ($1,997)"):
        if email:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": PRICE_ID, "quantity": 1}],
                mode="payment",
                success_url="https://rankscore.ai/success",
                cancel_url="https://rankscore.ai/cancel",
                metadata={"email": email}
            )
            st.success("✅ Redirecting to checkout...")
            st.markdown(f"[Click here to complete your purchase]({session.url})")
            new_code = save_access_code(email)
            send_access_code(email, new_code)
            st.success("✅ Purchase successful! Check your email for the access code.")
        else:
            st.error("❌ Please enter your email in the Lite section to proceed with payment.")

if __name__ == "__main__":
    main()