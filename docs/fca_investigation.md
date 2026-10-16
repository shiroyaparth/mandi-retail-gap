# Investigation of fcainfoweb.nic.in (Department of Consumer Affairs)

**Date of Investigation:** 16 October 2026  
**Investigated from:** Local workstation (India IP)  
**Status:** **CAPTCHA-gated WebForms; No automated keyless API available**

---

## 1. Technical Findings

1. **DNS & Connectivity:**
   - Host: `fcainfoweb.nic.in` -> Resolved to `164.100.117.103` (NIC datacenter).
   - Port 443 TCP connected with valid SSL.
   - Platform: ASP.NET WebForms (`.aspx`), running on Microsoft IIS with ViewState and EventValidation tokens.

2. **Endpoints Probed:**
   - `https://fcainfoweb.nic.in/` (HTTP 200)
   - `https://fcainfoweb.nic.in/Reports/Report_Menu_web.aspx` (HTTP 200, 13,109 bytes)
   - `https://fcainfoweb.nic.in/ListofCentersRevamp.aspx` (HTTP 200, 241,413 bytes)
   - `https://fcainfoweb.nic.in/ListofCommodity.aspx` (HTTP 200, 55,352 bytes)

3. **Access Controls & Anti-Automation:**
   - On `Report_Menu_web.aspx`, generating the Retail or Wholesale price report requires submitting a POST request containing:
     - `__VIEWSTATE`, `__EVENTVALIDATION`, `__VIEWSTATEGENERATOR`
     - Radio selection `ctl00$MainContent$Rbl_Rpt_type`
     - **Image CAPTCHA:** `<img src="../CaptchaImage.aspx" id="ctl00_MainContent_captchalogin" />`
     - **Mandatory input:** `<input name="ctl00$MainContent$Captcha" type="text" maxlength="6" ... />`
     - Validation: `ctl00_MainContent_Captcha_Validator.errormessage = "Captcha is required.";`

4. **Compliance & Hard Rules:**
   - Automated CAPTCHA solving, bypass, or evasion is **strictly prohibited** by project rules and site terms.
   - There is no documented public unauthenticated REST API or open direct download URL for daily commodity CSVs.

---

## 2. Solution: Manual Official Import (`data_type='official'`)

To allow official government retail prices from DCA / fcainfoweb into KrishiPulse without violating site rules:
1. **Manual Export:**
   A user can open `https://fcainfoweb.nic.in/Reports/Report_Menu_web.aspx` in their browser, solve the visual CAPTCHA, view the daily retail price table, and save/export it as CSV or Excel into `data_inbox/`.
2. **Dedicated Importer:**
   Run `python ingestion/load_retail_official.py --file <path_to_report>`:
   - Maps commodities (Potato, Onion, Tomato).
   - Maps Gujarat reporting centers (Ahmedabad, Rajkot, Surat, Vadodara, etc.).
   - Inserts records into `retail_price` marked with `data_type = 'official'` and `confidence_note = 'Official Dept. of Consumer Affairs (fcainfoweb.nic.in)'`.
3. **Labels & Separation:**
   All dashboards, downstream anomaly scoring, and summaries explicitly distinguish `official` vs `scraped` vs `modeled` retail prices. Modeled estimates are never presented as observed data.
