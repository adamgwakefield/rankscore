from flask import Flask, request, jsonify
import stripe
import openai
import requests
from bs4 import BeautifulSoup
import os

app = Flask(__name__)

# === Config ===
openai.api_key = ""
stripe.api_key = ""
STRIPE_WEBHOOK_SECRET = ""

# === Health Check Route ===
@app.route('/', methods=['GET'])
def home():
    return jsonify({"message": "RankScore API is live. Use /analyze or /webhook."})

# === RankScore Logic Placeholders ===
def analyze_metadata(url):
    return {"meta_score": 90}

def analyze_headers(soup):
    return {"header_score": 80}

def analyze_structured_data(soup):
    return {"structured_score": 70}

def analyze_faq(soup):
    return {"faq_score": 100}

def analyze_mobile_friendly(soup):
    return {"mobile_score": 90}

def analyze_accessibility(soup):
    return {"accessibility_score": 85}

def analyze_page_speed(url):
    return {"speed_score": 88}

def calculate_rankscore(*args):
    score = sum(
        value for score_dict in args for value in score_dict.values()
    ) / len(args)
    return round(score, 2)

# === Analyze Endpoint ===
@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.get_json()
    url = data.get("url")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')

        metadata = analyze_metadata(url)
        headers = analyze_headers(soup)
        structured_data = analyze_structured_data(soup)
        faq_present = analyze_faq(soup)
        mobile_friendly = analyze_mobile_friendly(soup)
        accessibility = analyze_accessibility(soup)
        speed_metrics = analyze_page_speed(url)

        final_score = calculate_rankscore(
            metadata, headers, structured_data, faq_present,
            mobile_friendly, accessibility, speed_metrics
        )

        result = {
            "url": url,
            "score": final_score,
            "components": {
                **metadata,
                **headers,
                **structured_data,
                **faq_present,
                **mobile_friendly,
                **accessibility,
                **speed_metrics
            }
        }

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Failed to analyze: {str(e)}"}), 500

# === Stripe Webhook ===
@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return jsonify({"error": str(e)}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        customer_email = session.get('customer_email')
        metadata = session.get('metadata', {})
        url_to_analyze = metadata.get('url')

        print(f"[STRIPE] ✅ Payment received from {customer_email} for {url_to_analyze}")
        # TODO: Optional - Trigger analysis here

    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
