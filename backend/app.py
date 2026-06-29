# backend/app.py
import os
from flask import Flask, jsonify
from flask_cors import CORS
import requests
from dotenv import load_dotenv

# --- NEW: Tell Python to find the .env file one level UP ---
# 1. Get the directory of the current app.py file (lhd-screening/backend/)
current_dir = os.path.dirname(os.path.abspath(__file__))
# 2. Go up one level to the root directory (lhd-screening/)
root_dir = os.path.dirname(current_dir)
# 3. Combine the root path with the filename '.env'
env_path = os.path.join(root_dir, '.env')

# Load the .env file from the root folder
load_dotenv(dotenv_path=env_path)

app = Flask(__name__)
CORS(app)  # Prevents browser CORS errors during local testing

@app.route('/api/external-data', methods=['GET'])
def get_secure_data():
    # Grab the API key from the environment variable
    api_key = os.getenv("DEMO_NWM_V2_API_KEY")
    
    # Check if the key was loaded correctly (helps with debugging)
    if not api_key:
        return jsonify({"error": "API key missing. Check your .env pathing!"}), 500
    
    # Grab the external API URL from the environment variable  
    external_url = os.getenv("DEMO_NWM_V2_URL")
    
    # Header implementation
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json"
    }
    
    try:
        response = requests.get(external_url, headers=headers)
        response.raise_for_status() 
        return jsonify(response.json())
        
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Failed to fetch data", "details": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)