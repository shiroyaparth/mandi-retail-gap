import requests
from dotenv import load_dotenv
import os
import json

load_dotenv()
DATA_GOV_API_KEY = os.getenv('DATA_GOV_API_KEY').strip()

api_url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"

params = {
    'api-key': DATA_GOV_API_KEY,
    'format': 'json',
    'limit': 10,
    'filters[state.keyword]': 'Gujarat',
    'filters[commodity.keyword]': 'Onion'
}

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
}

response = requests.get(api_url, params=params, headers=headers)

print(f"URL: {response.url}")
print(f"Status code: {response.status_code}")

if response.status_code == 200:
    data = response.json()
    print(json.dumps(data, indent=2))
    os.makedirs('ingestion', exist_ok=True)
    with open('ingestion/raw_sample_onion.json', 'w') as f:
        json.dump(data, f, indent=2)
else:
    print("Raw error body:", response.text)