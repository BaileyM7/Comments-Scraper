import os
import requests
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from datetime import datetime, timedelta
from openai import OpenAI
from cleanup_text import clean_text

# === CONFIG ===
with open("keys/openai.txt") as f:
    OPENAI_API_KEY = f.read().strip()

with open("keys/regulation.txt") as f:
    API_KEY = f.read().strip()

BASE_URL = "https://api.regulations.gov/v4/comments"
MAX_RESULTS = 50  # Adjust for more

# === OpenAI Client ===
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === Utility to determine if text is from an organization ===
def is_from_organization(text):
    prompt = (
        "Classify the following comment as written by either an individual or an organization. "
        "Use the number 1 if it was written by an organization, and 2 if it was written by an individual. "
        "An organization includes companies, associations, coalitions, nonprofits, universities, or any group "
        "that speaks on behalf of members. Use the content of the message—not just the name or signature—for your decision."
    )

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:3000]}
        ]
    )

    msg = response.choices[0].message.content.strip()
    return "1" in msg

# === Utility to summarize text ===
def summarize_text(text, agency_name, comment_title):
    prompt = (
        f"Create a 300-word news story with a headline based on the following letter to the federal agency named '{agency_name}'.\n\n"
        "For documents from government, college, or public policy groups (including coalitions and alliances): "
        "Write a news story with stand-alone paragraphs, incorporating direct quotes from the letter's author. "
        "Avoid using a dateline or acronyms.\n\n"
        "For documents from business entities: Write a news story with stand-alone paragraphs, incorporating direct "
        "quotes from the letter's author. Include the city/state of the company filing the comment in the text. Avoid "
        f"using a dateline or acronyms.\n\nAlso describe the organization(s) involved based on this comment title: {comment_title}"
    )
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:8000]}
        ]
    )
    return response.choices[0].message.content

# === Utility to download and extract text from PDF ===
def extract_pdf_text(url):
    response = requests.get(url)
    with open("temp.pdf", "wb") as f:
        f.write(response.content)

    text = ""
    try:
        with pdfplumber.open("temp.pdf") as pdf:
            text = "\n".join([page.extract_text() for page in pdf.pages if page.extract_text()])
    except Exception as e:
        print("pdfplumber error:", e)

    if text.strip():
        os.remove("temp.pdf")
        return text

    print("Falling back to OCR...")
    images = convert_from_path("temp.pdf")
    ocr_text = "\n".join([pytesseract.image_to_string(img) for img in images])
    os.remove("temp.pdf")
    return ocr_text

# === Retrieve full comment info (with fallback for attachments) ===
def fetch_comment_detail(comment_id):
    detail_url = f"{BASE_URL}/{comment_id}?include=attachments&api_key={API_KEY}"
    response = requests.get(detail_url)
    if response.status_code == 200:
        return response.json()
    else:
        print("Failed to retrieve detail for comment.", response.status_code)
        return None

# === Main Process ===
def fetch_comments_with_attachments():
    yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    today_date_md = datetime.today().strftime("%B %d")
    today_date_mdy = datetime.today().strftime("%B %d, %Y")
    url = f"{BASE_URL}?filter[postedDate]={yesterday}&include=attachments&page[size]={MAX_RESULTS}&api_key={API_KEY}"
    response = requests.get(url)

    print("API Status Code:", response.status_code)

    data = response.json()
    comments = data.get("data", [])
    included = data.get("included", [])
    attachments = {item["id"]: item for item in included if item["type"] == "attachments"}

    total_checked = 0
    valid_outputs = 0

    for comment in comments:
        total_checked += 1
        comment_title = comment.get("attributes", {}).get("title", "<No Title>")
        agency = comment.get("attributes", {}).get("agency", "a federal agency")
        comment_id = comment.get("id")

        detail_data = fetch_comment_detail(comment_id)
        if not detail_data:
            continue

        comment_data = detail_data.get("data", {})
        included_data = detail_data.get("included", [])
        detail_attachments = {item["id"]: item for item in included_data if item["type"] == "attachments"}

        comment_attachments = comment_data.get("relationships", {}).get("attachments", {}).get("data", [])
        if not comment_attachments:
            continue

        for att in comment_attachments:
            att_id = att.get("id")
            att_info = detail_attachments.get(att_id)
            if att_info:
                file_formats = att_info.get("attributes", {}).get("fileFormats", [])
                file_url = None
                for fmt in file_formats:
                    if "fileUrl" in fmt:
                        file_url = fmt["fileUrl"]
                        break

                if not file_url:
                    continue

                text = extract_pdf_text(file_url)
                if not text.strip():
                    continue

                org_keywords = ["llc", "inc", "organization", "institute", "university", "center", "society", "association", "coalition", "agency", "company", "group", "corporation"]
                title_lower = comment_title.lower()
                title_has_org = any(word in title_lower for word in org_keywords)

                if title_has_org:
                    is_org = True
                else:
                    is_org = is_from_organization(text)

                if is_org:
                    summary = summarize_text(text, agency, comment_title)
                    lines = summary.strip().split("\n", 1)
                    headline = lines[0].strip()
                    body_main = lines[1].strip() if len(lines) > 1 else ""
                    body = f"WASHINGTON, {today_date_md} -- {body_main}\n\n* * *\n\nRead full text of the public communication here: {file_url}\n\nView Regulations.gov posting on {today_date_mdy} and docket information here: https://www.regulations.gov/comment/{comment_id}."
                    filename = f"{comment_id}.txt"

                    headline = clean_text(headline)
                    body = clean_text(body)
                    filename = clean_text(filename)

                    print("Headline:", headline)
                    print("Body:", body)
                    print("Filename would be:", filename)

                    valid_outputs += 1
                break

    print(f"\nTotal comments checked: {total_checked}")
    print(f"Valid organization summaries generated: {valid_outputs}")

if __name__ == "__main__":
    fetch_comments_with_attachments()
