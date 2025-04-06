from flask import Flask, request, jsonify
import stripe, openai, requests
from bs4 import BeautifulSoup
import os

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.route('/', methods=['GET'])
def home():
    return jsonify({"message": "RankScore API is live. Use /analyze or /webhook."})

# Add /analyze and /webhook from your working code here...

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
