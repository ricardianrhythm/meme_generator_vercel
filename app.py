# 1. Import Statements
from flask import Flask, request, jsonify, render_template, session, make_response
import firebase_admin
from firebase_admin import credentials, firestore
import json
import requests
import openai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type  # Updated import
import os
import logging
import threading
from cachetools import TTLCache

# 2. Configuration and Setup
app = Flask(__name__)

# Enable session management
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your_secret_key')  # Replace 'your_secret_key' with an actual key for production

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Firebase Initialization
def initialize_firebase():
    if not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(os.environ.get('FIREBASE_CREDENTIALS')))
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = initialize_firebase()

# Set up API keys
IMGFLIP_USERNAME = os.environ.get('IMGFLIP_USERNAME')
IMGFLIP_PASSWORD = os.environ.get('IMGFLIP_PASSWORD')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

# Initialize the IP location cache and lock
ip_location_cache = TTLCache(maxsize=1000, ttl=3600)  # Cache up to 1000 IPs for 1 hour
cache_lock = threading.Lock()  # Use a lock if needed

# 3. Helper Functions

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(min=1, max=30),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    reraise=True
)
def fetch_location_data(ip_address):
    """
    Fetches the location data using an IP address with exponential backoff.
    """
    # Check if IP address is in cache
    with cache_lock:
        if ip_address in ip_location_cache:
            logger.debug(f"Retrieved location data for IP {ip_address} from cache.")
            return ip_location_cache[ip_address]
    
    try:
        # Use ipapi to get location data
        location_response = requests.get(f'https://ipapi.co/{ip_address}/json/')
        location_response.raise_for_status()
        location_data = location_response.json()
        
        user_location = {
            'ip': ip_address,
            'city': location_data.get('city', 'Unknown City'),
            'region': location_data.get('region', 'Unknown Region'),
            'country': location_data.get('country_name', 'Unknown Country')
        }
        
        # Store the location data in the cache
        with cache_lock:
            ip_location_cache[ip_address] = user_location
            logger.debug(f"Stored location data for IP {ip_address} in cache.")
        
        return user_location

    except requests.exceptions.HTTPError as http_err:
        logger.error(f"HTTP error occurred: {http_err}")
        raise
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Request error occurred: {req_err}")
        raise

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


def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        # 'X-Forwarded-For' may contain multiple IPs, take the first one
        ip = request.headers.getlist("X-Forwarded-For")[0].split(',')[0]
    else:
        ip = request.remote_addr
    return ip

def collect_user_ip_and_location():
    # Check if location data is already stored in the session
    if 'user_location' in session:
        logger.debug("Using cached location from session.")
        return session['user_location']
    
    # If not in session, check if location data is stored in cookies
    location_data = request.cookies.get('user_location')
    if location_data:
        user_location = json.loads(location_data)
        # Save to session to avoid future cookie checks
        session['user_location'] = user_location
        logger.debug("Using cached location from cookies.")
        return user_location

    try:
        # Get the client's IP address
        ip_address = get_client_ip()
        
        # Log the IP address
        logger.debug(f"User IP Address: {ip_address}")

        # Fetch location data with backoff strategy
        user_location = fetch_location_data(ip_address)

        # Save location data in session and cookie
        session['user_location'] = user_location
        resp = make_response(jsonify({"message": "Location data saved"}))
        resp.set_cookie('user_location', json.dumps(user_location), max_age=3600)
        
        return user_location  # Return the user location, not the response

    except Exception as e:
        logger.error(f"Error fetching IP or location data: {str(e)}")
        return {
            'ip': 'Unknown IP',
            'city': 'Unknown City',
            'region': 'Unknown Region',
            'country': 'Unknown Country'
        }


def upsert_location(location_label, city, region, country):
    try:
        # Check if the location document already exists
        location_query = db.collection('locations').where('label', '==', location_label).limit(1).get()
        
        if location_query:
            # Document exists, update it
            location_ref = location_query[0].reference
            location_ref.update({
                'city': city,
                'region': region,
                'country': country,
                'updated_at': firestore.SERVER_TIMESTAMP
            })
            logger.debug(f"Location '{location_label}' updated successfully.")
        else:
            # Document does not exist, create it
            db.collection('locations').add({
                'label': location_label,
                'city': city,
                'region': region,
                'country': country,
                'created_at': firestore.SERVER_TIMESTAMP
            })
            logger.debug(f"Location '{location_label}' created successfully.")
    except Exception as e:
        logger.error(f"Error upserting location: {str(e)}")

def get_meme_list():
    try:
        response = requests.get("https://api.imgflip.com/get_memes")
        response.raise_for_status()
        data = response.json()
        memes = data['data']['memes']
        return [{'name': meme['name'], 'id': meme['id'], 'box_count': meme['box_count']} for meme in memes[:100]]
    except requests.RequestException as e:
        logger.error(f"Error fetching meme list: {e}")
        return []

def generate_meme(thought, location_label, meme_id=None, previous_doc_id=None, excluded_memes=None):
    try:
        # Collect user IP and location data
        user_data = collect_user_ip_and_location()
        city = user_data['city']
        region = user_data['region']
        country = user_data['country']
        
        # Upsert location document in Firestore
        upsert_location(location_label, city, region, country)
        
        # Fetch available memes and exclude previously generated ones
        meme_list = get_meme_list()
        if excluded_memes:
            meme_list = [meme for meme in meme_list if meme['id'] not in excluded_memes]
        if not meme_list:
            return None, None, None, "Error: No more memes available"
            
        if meme_id:
            selected_meme = next((meme for meme in meme_list if meme['id'] == meme_id), None)
            if not selected_meme:
                return None, None, None, f"Meme with ID {meme_id} not found in meme list"
            box_count = selected_meme['box_count']
        else:
            meme_list_str = "\n".join(
                [f"{meme['name']} (ID: {meme['id']}, box_count: {meme['box_count']})" for meme in meme_list]
            )

            messages = [
                {"role": "system", "content": "You are an expert in meme creation. Your task is to select the most appropriate meme template based on a given thought, and generate witty and humorous text for the meme. Ensure that the meme is coherent and funny."},
                {"role": "user", "content": f"The person is at the following location: {location_label}. This is their thought: {thought}\n\nHere is a list of available memes and their respective IDs and box counts:\n{meme_list_str}\n\nBased on this thought, which meme template would be the best fit?\nPlease provide:\nmeme: <name of meme>\nmeme_id: <id of meme>\nexplanation: <reason for the choice>"}
            ]

            data = {
                "model": "gpt-3.5-turbo",
                "temperature": .9,
                "messages": messages
            }

            response = call_openai_api(data)
            if response is None:
                return None, None, None, "Failed to get response from OpenAI API"

            meme_info = response['choices'][0]['message']['content']
            meme_dict = {line.split(": ")[0].strip(): line.split(": ")[1].strip() for line in meme_info.split("\n") if ": " in line}

            meme_id = meme_dict.get('meme_id')
            if not meme_id:
                return None, None, None, "Failed to retrieve meme_id from OpenAI response."

            selected_meme = next((meme for meme in meme_list if meme['id'] == meme_id), None)
            if not selected_meme:
                return None, None, None, f"Meme with ID {meme_id} not found in meme list"

            box_count = selected_meme['box_count']

        # Prompt for text boxes
        text_box_prompt = f"Great choice! Now, the selected meme requires {box_count} text boxes (from text0 to text{box_count - 1}). Please provide the text for each text box, ensuring that the combined texts create a coherent and humorous meme that relates to the thought and location:\n"
        for i in range(box_count):
            text_box_prompt += f"text{i}: <text for text box {i}>\n"

        messages.append({"role": "assistant", "content": meme_info})
        messages.append({"role": "user", "content": text_box_prompt})

        data['messages'] = messages

        response = call_openai_api(data)
        if response is None:
            return None, None, None, "Failed to get response from OpenAI API"

        text_boxes_info = response['choices'][0]['message']['content']
        text_boxes = {line.split(": ")[0].strip(): line.split(": ")[1].strip() for line in text_boxes_info.split("\n") if ": " in line}

        # Prepare parameters for Imgflip API
        url = "https://api.imgflip.com/caption_image"
        params = {
            "template_id": meme_id,
            "username": IMGFLIP_USERNAME,
            "password": IMGFLIP_PASSWORD,
        }

        if box_count > 2:
            for i in range(box_count):
                text_key = f"text{i}"
                text_value = text_boxes.get(text_key, '')
                params[f'boxes[{i}][text]'] = text_value
        else:
            params['text0'] = text_boxes.get('text0', '')
            params['text1'] = text_boxes.get('text1', '')

        response = requests.post(url, data=params)
        result = response.json()

        if result['success']:
            meme_url = result['data']['url']
            
            # Find the location document reference
            location_query = db.collection('locations').where('label', '==', location_label).limit(1).get()
            location_id = location_query[0].id if location_query else None

            # Save meme to Firebase
            try:
                write_time, doc_ref = db.collection('memes').add({
                    'thought': thought,
                    'location': location_label,
                    'location_id': location_id,
                    'city': city,  # Adding city information
                    'region': region,  # Adding region information
                    'country': country,  # Adding country information
                    'meme_url': meme_url,
                    'explanation': meme_dict.get('explanation', ''),
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                return meme_url, meme_id, doc_ref.id, None
            except Exception as e:
                error_msg = f"Error storing meme in Firebase: {str(e)}"
                logger.error(error_msg)
                return None, None, None, error_msg
        else:
            error_msg = f"Failed to generate meme. {result.get('error_message', '')}"
            logger.error(error_msg)
            return None, None, None, error_msg

    except Exception as e:
        error_msg = f"Error in generate_meme: {str(e)}"
        logger.error(error_msg)
        return None, None, None, error_msg

def regenerate_meme(thought, location, excluded_memes):
    meme_url, meme_id, doc_id, error = generate_meme(thought, location, excluded_memes=excluded_memes)
    if error:
        return error, None, get_memes_from_firebase()

    meme_html = f"""
    <div style='text-align: center;'>
        <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
        <p style='font-size: 1.2em; font-weight: bold;'>{thought}</p>
        <p style='font-size: 1em;'>Location: {location}</p>
    </div>
    """
    return "Meme regenerated successfully.", meme_html, get_memes_from_firebase(), meme_id

def get_memes_from_firebase(city=None, region=None, country=None):
    try:
        # Fetch all memes first
        all_memes = db.collection('memes').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(100).get()
        
        # Return a list of dictionaries for easier processing
        memes = [{'meme_url': meme.to_dict()['meme_url'], 
                  'thought': meme.to_dict()['thought'], 
                  'location': meme.to_dict().get('location', ''),
                  'city': meme.to_dict().get('city', ''),
                  'region': meme.to_dict().get('region', ''),
                  'country': meme.to_dict().get('country', '')} 
                 for meme in all_memes]
        
        # Apply filtering in Python if needed
        filtered_memes = []
        if city:
            filtered_memes = [meme for meme in memes if meme.get('city') == city]
        
        if not filtered_memes and region:
            filtered_memes = [meme for meme in memes if meme.get('region') == region]
        
        if not filtered_memes and country:
            filtered_memes = [meme for meme in memes if meme.get('country') == country]
        
        # Fallback to all memes if no specific memes found for filters
        if not filtered_memes:
            filtered_memes = memes

        return filtered_memes

    except Exception as e:
        logger.error(f"Error fetching memes from Firebase: {str(e)}")
        return []

def get_previous_memes():
    try:
        logger.debug("Fetching previous memes")
        
        # Collect user data for filtering
        user_data = collect_user_ip_and_location()
        city = user_data['city']
        region = user_data['region']  # Extract region/state information
        country = user_data['country']  # Extract country information
        
        # Fetch memes with fallback logic
        meme_gallery = get_memes_from_firebase(city, region, country)
        
        return jsonify({'memes': meme_gallery, 'level': 'city/region/country based on found data'})
    except Exception as e:
        logger.error(f"Error in get_previous_memes: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_locations_from_firebase(city, region, country):
    try:
        locations = db.collection('locations').get()
        location_labels = [
            location.to_dict().get('label', 'Unknown Location') for location in locations
            if location.to_dict().get('city') == city or
               location.to_dict().get('region') == region or
               location.to_dict().get('country') == country
        ]
        logger.debug(f"Fetched locations for city: {city}, region: {region}, country: {country}: {location_labels}")
        return location_labels
    except Exception as e:
        logger.error(f"Error fetching locations from Firebase: {str(e)}")
        return ["Other (specify below)"]

# 4. Route Definitions
@app.route('/')
def index():
    logger.debug("Rendering main page")
    
    user_data = collect_user_ip_and_location()
    user_city = user_data.get('city')
    user_region = user_data.get('region')
    user_country = user_data.get('country')

    # Fetch locations from Firebase and filter them based on user's location
    locations = get_locations_from_firebase(user_city, user_region, user_country)
    
    if "Other (specify below)" in locations:
        locations.remove("Other (specify below)")
    locations.append("Other (specify below)")

    logger.debug(f"Locations passed to template: {locations}")
    
    return render_template('index.html', locations=locations)

@app.route('/generate_meme', methods=['POST'])
def generate_meme_route():
    try:
        data = request.json
        location = data.get('location', '').strip()
        thought = data.get('thought', '').strip()
        excluded_memes = data.get('excluded_memes', [])

        if not thought or not location:
            return jsonify({'status': 'Please enter both a location and a thought.', 'meme_html': None})

        meme_url, meme_id, doc_id, error = generate_meme(thought, location, excluded_memes=excluded_memes)
        if error:
            return jsonify({'status': error, 'meme_html': None})

        meme_html = f"""
        <div style='text-align: center;'>
            <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
            <p style='font-size: 1.2em; font-weight: bold;'>{thought}</p>
            <p style='font-size: 1em;'>Location: {location}</p>
        </div>
        """

        return jsonify({
            'status': 'Meme generated successfully.',
            'meme_html': meme_html,
            'meme_id': meme_id
        })
    except Exception as e:
        logger.error(f"Error in generate_meme_route: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_previous_memes')
def get_previous_memes_route():
    try:
        logger.debug("Fetching previous memes")
        
        # Collect user data for filtering
        user_data = collect_user_ip_and_location()
        city = user_data['city']
        region = user_data['region']
        country = user_data['country']
        logger.debug(f"User location data: {user_data}")
        
        # Fetch memes with fallback logic
        meme_gallery = get_memes_from_firebase(city=city)
        level = 'city'
        if not meme_gallery:
            meme_gallery = get_memes_from_firebase(region=region)
            level = 'region'
        if not meme_gallery:
            meme_gallery = get_memes_from_firebase(country=country)
            level = 'country'
        if not meme_gallery:
            meme_gallery = get_memes_from_firebase()
            level = 'global'
        
        return jsonify({'memes': meme_gallery, 'level': level})
    except Exception as e:
        logger.error(f"Error in get_previous_memes: {str(e)}")
        return jsonify({'error': str(e)}), 500

# 5. App Execution
if __name__ == '__main__':
    app.run(debug=True)