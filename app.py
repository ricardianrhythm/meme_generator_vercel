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

def collect_user_ip():
    try:
        # Try Streamlit's experimental function
        user_ip = st.experimental_get_query_params().get('streamlit_ip', ['unknown'])[0]
        if user_ip != 'unknown':
            return user_ip

        # Check common proxy headers
        headers_to_check = [
            'HTTP_X_FORWARDED_FOR',
            'HTTP_X_REAL_IP',
            'REMOTE_ADDR'
        ]
        for header in headers_to_check:
            ip = st._get_browser_address_bar_data().get(header)
            if ip:
                return ip.split(',')[0].strip()

        # If all else fails, use the server's IP (which will be consistent for your app)
        response = requests.get('https://api.ipify.org?format=json')
        return response.json()['ip']
    except:
        return "Unknown IP"

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

def get_memes_from_firebase():
    try:
        memes = db.collection('memes').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(20).get()
        return [[meme.to_dict()['meme_url'], f"{meme.to_dict()['thought']} (Location: {meme.to_dict().get('location', '')})"] for meme in memes]
    except Exception as e:
        st.error(f"Error fetching memes from Firebase: {str(e)}")
        return []

def get_locations_from_firebase():
    try:
        locations = db.collection('locations').order_by('label').get()
        location_labels = [location.to_dict().get('label', 'Unknown Location') for location in locations]
        return location_labels + ["Other (specify below)"]
    except Exception as e:
        st.error(f"Error fetching locations from Firebase: {str(e)}")
        return ["Other (specify below)"]

def create_meme(location, thought):
    if not thought.strip():
        return "Please enter your thought.", None, get_memes_from_firebase()

    used_thought = thought.strip()
    used_label = location.strip()

    if location == "Other (specify below)":
        return "Please enter a custom location.", None, get_memes_from_firebase()


def create_meme(location, thought):
    if not thought.strip():
        return "Please enter your thought.", None, get_memes_from_firebase()

    used_thought = thought.strip()
    used_label = location.strip()

    if not used_label:
        return "Please enter a location.", None, get_memes_from_firebase()

    # Add new location to Firebase if it's not already in the list
    existing_locations = get_locations_from_firebase()
    if used_label not in existing_locations:
        try:
            db.collection('locations').add({
                'label': used_label,
                'ip_address': ""
            })
            st.success(f"Added new location: {used_label}")
        except Exception as e:
            st.error(f"Error adding new location to Firebase: {str(e)}")

    # Collect IP address
    ip_address = collect_user_ip()

    # Generate the meme
    meme_url, meme_id, doc_id, error = generate_meme(used_thought, used_label)
    if error:
        return error, None, get_memes_from_firebase()

    # Update location with IP address
    try:
        location_query = db.collection('locations').where('label', '==', used_label).limit(1).get()
        if location_query:
            location_doc_id = location_query[0].id
            db.collection('locations').document(location_doc_id).update({
                'ip_address': ip_address
            })
    except Exception as e:
        st.error(f"Error updating location with IP address: {str(e)}")

    # Store meme details with IP address
    try:
        db.collection('memes').add({
            'thought': used_thought,
            'location': used_label,
            'meme_url': meme_url,
            'ip_address': ip_address,
            'timestamp': firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        st.error(f"Error storing meme in Firebase: {str(e)}")

    meme_html = f"""
    <div style='text-align: center;'>
        <img src='{meme_url}' alt='Meme' style='max-width: 100%; height: auto;'/>
        <p style='font-size: 1.2em; font-weight: bold;'>{used_thought}</p>
        <p style='font-size: 1em;'>Location: {used_label}</p>
    </div>
    """
    return "Meme generated successfully.", meme_html, get_memes_from_firebase()

def main():
    try:
        logger.debug("Starting main function")
        
        st.title("Big Red Button Meme Generator")
        
        logger.debug("Fetching locations from Firebase")
        location_labels = get_locations_from_firebase()
        logger.debug(f"Fetched locations: {location_labels}")
        
        # Ensure "Other (specify below)" is always the last option
        if "Other (specify below)" in location_labels:
            location_labels.remove("Other (specify below)")
        location_labels.append("Other (specify below)")
        
        # Use a unique key for the selectbox
        selected_location = st.selectbox(
            "Select Location", 
            location_labels,
            key='location_selectbox'
        )
        
        logger.debug(f"Selected location: {selected_location}")

        # Only show the custom location input if "Other (specify below)" is selected
        if selected_location == "Other (specify below)":
            custom_location = st.text_input("Enter custom location")
            logger.debug(f"Custom location entered: {custom_location}")
        else:
            custom_location = ""

        thought = st.text_input("Enter your thought")
        logger.debug(f"Thought entered: {thought}")
        
        # Initialize session state variables
        if 'meme_generated' not in st.session_state:
            st.session_state.meme_generated = False
        if 'excluded_memes' not in st.session_state:
            st.session_state.excluded_memes = []
        if 'current_meme_id' not in st.session_state:
            st.session_state.current_meme_id = None

        if st.button("Generate Meme"):
            logger.debug("Generate Meme button clicked")
            # Use custom_location if "Other (specify below)" is selected, otherwise use selected_location
            location = custom_location if selected_location == "Other (specify below)" else selected_location
            logger.debug(f"Location for meme generation: {location}")
            
            # Use create_meme function to handle new location saving and meme generation
            status, meme_html, meme_gallery = create_meme(location, thought)
            
            if "successfully" in status:
                st.session_state.meme_generated = True
                st.session_state.current_meme_html = meme_html
                st.session_state.current_status = status
                st.session_state.current_location = location
            else:
                st.write(status)

        if st.session_state.meme_generated:
            st.write(st.session_state.current_status)
            if st.session_state.current_meme_html:
                st.markdown(st.session_state.current_meme_html, unsafe_allow_html=True)
            
            if st.button("Try again, different meme"):
                logger.debug("Try again button clicked")
                st.session_state.excluded_memes.append(st.session_state.current_meme_id)
                status, meme_html, meme_gallery = create_meme(st.session_state.current_location, thought)
                
                if "successfully" in status:
                    st.session_state.current_meme_html = meme_html
                    st.session_state.current_status = "New meme generated successfully."
                    st.rerun()
                else:
                    st.write(status)

        logger.debug("Fetching previous memes")
        st.subheader("Previous Memes")
        meme_gallery = get_memes_from_firebase()
        for meme_url, caption in meme_gallery:
            st.image(meme_url, caption=caption, use_column_width=True)
        
        logger.debug("Main function completed successfully")

    except Exception as e:
        logger.error(f"An error occurred in the main function: {str(e)}")
        st.error(f"An error occurred: {str(e)}")

if __name__ == "__main__":
    main()
    
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
