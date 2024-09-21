from flask import Flask, request, jsonify, render_template
import firebase_admin
from firebase_admin import credentials, firestore
import json
import requests
import openai
from tenacity import retry, stop_after_attempt, wait_exponential
import os

app = Flask(__name__)

# Initialize Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(json.loads(os.environ.get('FIREBASE_CREDENTIALS')))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Set up API keys
IMGFLIP_USERNAME = os.environ.get('IMGFLIP_USERNAME')
IMGFLIP_PASSWORD = os.environ.get('IMGFLIP_PASSWORD')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def call_openai_api(data):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {openai.api_key}'
    }
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
        response.raise_for_status()
        return response.json()
    except Exception as err:
        print(f"An error occurred: {err}")
        return None

def collect_user_ip():
    return request.remote_addr

# ... (keep other utility functions like get_meme_list, generate_meme, etc.)

@app.route('/')
def index():
    locations = get_locations_from_firebase()
    return render_template('index.html', locations=locations)

@app.route('/generate_meme', methods=['POST'])
def create_meme_route():
    data = request.json
    location = data.get('location')
    thought = data.get('thought')
    
    status, meme_html, _ = create_meme(location, thought)
    
    return jsonify({
        'status': status,
        'meme_html': meme_html
    })

@app.route('/get_previous_memes')
def get_previous_memes():
    meme_gallery = get_memes_from_firebase()
    return jsonify(meme_gallery)

if __name__ == '__main__':
    app.run(debug=True)
