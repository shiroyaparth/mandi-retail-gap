import re
import requests

try:
    r = requests.get('https://fcainfoweb.nic.in', timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
    print("Status:", r.status_code)
    menu_url = "https://fcainfoweb.nic.in/Reports/Report_Menu_web.aspx"
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    res = session.get(menu_url, timeout=10)
    
    # Extract all input tags
    inputs = re.findall(r'<input[^>]+name=[\'"]([^\'"]+)[\'"][^>]*>', res.text, re.I)
    selects = re.findall(r'<select[^>]+name=[\'"]([^\'"]+)[\'"][^>]*>', res.text, re.I)
    print("Inputs:", inputs)
    print("Selects:", selects)
    
    # Check button values
    buttons = re.findall(r'<input[^>]+type=[\'"]submit[\'"][^>]*value=[\'"]([^\'"]+)[\'"][^>]*>', res.text, re.I)
    print("Submit buttons:", buttons)




except Exception as exc:
    print("FCA request failed:", exc)
