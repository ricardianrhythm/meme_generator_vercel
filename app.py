# 1. Import Statements
from flask import Flask, request, jsonify, render_template
import firebase_admin
from firebase_admin import credentials, firestore
import json
import requests
import openai
from tenacity import retry, stop_after_attempt, wait_exponential
import os
import logging

# 2. Configuration and Setup
app = Flask(__name__)
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

# 3. Helper Functions

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

def collect_user_ip_and_location():
    try:
        # Make a request to the ipify API to get the public IP address
        response = requests.get('https://api.ipify.org?format=json')
        response.raise_for_status()
        ip_address = response.json().get('ip')
        
        # Log the IP address
        print(f"User IP Address: {ip_address}")  # This will log to Vercel logs
        
        # Use ipapi to get location data
        location_response = requests.get(f'https://ipapi.co/{ip_address}/json/')
        location_response.raise_for_status()
        location_data = location_response.json()

        return {
            'ip': ip_address,
            'city': location_data.get('city', 'Unknown City'),
            'region': location_data.get('region', 'Unknown Region'),  # Collect region/state
            'country': location_data.get('country_name', 'Unknown Country')  # Collect country
        }
    except Exception as e:
        print(f"Error fetching IP or location data: {str(e)}")  # Log any errors
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
        st.error(f"Error fetching meme list: {e}")
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
                    'meme_url': meme_url,
                    'explanation': meme_dict.get('explanation', ''),
                    'timestamp': firestore.SERVER_TIMESTAMP
                })
                return meme_url, meme_id, doc_ref.id, None
            except Exception as e:
                error_msg = f"Error storing meme in Firebase: {str(e)}"
                st.error(error_msg)
                return None, None, None, error_msg
        else:
            error_msg = f"Failed to generate meme. {result.get('error_message', '')}"
            st.error(error_msg)
            return None, None, None, error_msg

    except Exception as e:
        error_msg = f"Error in generate_meme: {str(e)}"
        st.error(error_msg)
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
        if city:
            filtered_memes = [meme for meme in memes if meme.get('city') == city]
            if filtered_memes:
                return filtered_memes

        if region:
            filtered_memes = [meme for meme in memes if meme.get('region') == region]
            if filtered_memes:
                return filtered_memes

        if country:
            filtered_memes = [meme for meme in memes if meme.get('country') == country]
            if filtered_memes:
                return filtered_memes

        return memes

    except Exception as e:
        logger.error(f"Error fetching memes from Firebase: {str(e)}")
        return []

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

def create_meme(location, thought, excluded_memes=[]):
    if not thought.strip():
        return "Please enter your thought.", None, get_memes_from_firebase()

    used_thought = thought.strip()
    used_label = location.strip()

    if not used_label:
        return "Please enter a location.", None, get_memes_from_firebase()
    
    # Generate the meme and store in Firebase
    meme_url, meme_id, doc_id, error = generate_meme(used_thought, used_label, excluded_memes=excluded_memes)
    if error:
        return error, None, get_memes_from_firebase()

    # Store meme details with IP address, city, state/region, and country
    user_data = collect_user_ip_and_location()
    ip_address = user_data['ip']
    city = user_data['city']
    region = user_data['region']  # Collect region/state information
    country = user_data['country']  # Collect country information

    logger.debug(f"Saving meme with user data: {user_data}")

    try:
        db.collection('memes').add({
            'thought': used_thought,
            'location': used_label,
            'city': city,
            'region': region,  # Store region/state information
            'country': country,  # Store country information
            'meme_url': meme_url,
            'ip_address': ip_address,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
        logger.debug(f"Meme saved successfully: {meme_url}")
    except Exception as e:
        logger.error(f"Error storing meme in Firebase: {str(e)}")

    meme_html = f"""
    <div style='text-align: center;'>
        <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
        <p style='font-size: 1.2em; font-weight: bold;'>{used_thought}</p>
        <p style='font-size: 1em;'>Location: {used_label}</p>
    </div>
    """
    return "Meme generated successfully.", meme_html, get_memes_from_firebase()

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
def create_meme_route():
    try:
        data = request.json
        location = data.get('location')
        thought = data.get('thought')
        # Initialize excluded_memes as an empty list if not provided in the request data
        excluded_memes = data.get('excluded_memes', [])  
        
        logger.debug(f"Generating meme with location: {location}, thought: {thought}, excluded memes: {excluded_memes}")
        
        # Call the create_meme function
        status, meme_html, _ = create_meme(location, thought, excluded_memes=excluded_memes)
        
        return jsonify({
            'status': status,
            'meme_html': meme_html,
            'meme_id': _  # Return the generated meme ID so it can be added to the exclusion list
        })
    except Exception as e:
        logger.error(f"Error in create_meme_route: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get_previous_memes')
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
        
        return jsonify(meme_gallery)
    except Exception as e:
        logger.error(f"Error in get_previous_memes: {str(e)}")
        return jsonify({'error': str(e)}), 500


# 5. App Execution
if __name__ == '__main__':
    app.run(debug=True)